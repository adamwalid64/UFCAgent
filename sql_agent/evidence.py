"""Point-in-time opponent strength and experience against measurable styles."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import date
from statistics import mean

from .analytics import duration_minutes, metrics


STYLE_KEYS = ("sig_strikes_landed_per_min", "striking_defense", "takedowns_per_15_min",
              "takedown_defense", "sub_attempts_per_15_min")
STYLE_TAGS = {"sig_strikes_landed_per_min": "high-volume striker",
              "takedowns_per_15_min": "wrestler", "sub_attempts_per_15_min": "submission threat"}


def _rank(value, values):
    if value is None or not values:
        return None
    return (sum(v < value for v in values) + 0.5 * sum(v == value for v in values)) / len(values)


def _record(rows):
    counts = Counter(r["bout_outcome"] for r in rows)
    return {key: counts[key] for key in ("win", "loss", "draw", "no_contest")}


def _performance(rows):
    stats = metrics(rows)
    landed, absorbed = stats["sig_strikes_landed_per_min"], stats["sig_strikes_absorbed_per_min"]
    return {"fights": len(rows), "record": stats["record"], "stat_fights": stats["stat_fights"],
            "sig_strike_differential_per_min": landed - absorbed if landed is not None and absorbed is not None else None,
            "takedowns_per_15_min": stats["takedowns_per_15_min"], "takedown_defense": stats["takedown_defense"],
            "wins_by_method": stats["wins_by_method"], "losses_by_method": stats["losses_by_method"]}


def build_matchup_evidence(rows: list[dict], a: dict, b: dict, cutoff: str, *, timeout: float = 5.0) -> dict:
    """Replay whole date batches; no historical opponent sees its bout or later form.

    Elo is derived here (1500 start, K=32, 400-point logistic scale), not an
    official ranking. It rewards outcomes against stronger opposition without
    giving an automatic bonus to finishes over decisions.
    """
    deadline = time.monotonic() + timeout
    histories, by_day = defaultdict(list), defaultdict(list)
    ratings, profiles = defaultdict(lambda: 1500.0), {}
    targets = {a["fighter_id"], b["fighter_id"]}
    evidence = {fid: [] for fid in targets}
    for row in rows:
        if row["event_date"] >= cutoff:
            raise ValueError("Evidence includes a bout on or after the cutoff")
        by_day[row["event_date"]].append(row)

    def cohort(day, weight_class):
        current = date.fromisoformat(day)
        active = [fid for fid, history in histories.items() if history and
                  0 <= (current - date.fromisoformat(history[-1]["event_date"])).days <= 1096]
        same_weight = [fid for fid in active if weight_class and histories[fid][-1].get("weight_class") == weight_class]
        return (same_weight, "same weight class, active within three years") if len(same_weight) >= 8 else (
            active, "all weight classes, active within three years (division cohort too small)")

    def style(fid, day, weight_class):
        stats = profiles.get(fid, metrics([]))
        population, basis = cohort(day, weight_class)
        reliable = stats["timed_stat_fights"] >= 3
        ranks = {}
        for key in STYLE_KEYS:
            values = [profiles[p][key] for p in population
                      if profiles[p]["timed_stat_fights"] >= 3 and profiles[p][key] is not None]
            ranks[key] = _rank(stats[key], values) if reliable else None
        tags = [tag for key, tag in STYLE_TAGS.items()
                if ranks[key] is not None and ranks[key] >= 0.75 and stats[key] > 0]
        return {"tags": tags, "prior_stat_fights": stats["timed_stat_fights"],
                "percentiles": ranks, "cohort": basis, "cohort_size": len(population),
                "status": "measured proxy" if reliable and any(v is not None for v in ranks.values()) else "insufficient history"}

    for day, bouts in sorted(by_day.items()):
        if time.monotonic() > deadline:
            raise TimeoutError("Opponent/style evidence replay timed out; incomplete evidence was not used")
        for row in bouts:
            fid, oid = row["fighter_id"], row["opponent_id"]
            if fid not in targets:
                continue
            prior = histories[oid]
            record = _record(prior)
            decided = sum(record[k] for k in ("win", "loss", "draw"))
            population, basis = cohort(day, row.get("weight_class"))
            values = [ratings[p] for p in population if sum(profiles[p]["record"][k] for k in ("win", "loss", "draw")) >= 3]
            rank = _rank(ratings[oid], values) if decided >= 3 else None
            tier = "unknown" if rank is None else "higher" if rank > 2 / 3 else "lower" if rank < 1 / 3 else "middle"
            duration = duration_minutes(row)
            evidence[fid].append({
                "fight_id": row["fight_id"], "date": day, "opponent_id": oid,
                "before_prediction_cutoff": day < cutoff,
                "opponent_name": row["opponent_name"], "weight_class": row.get("weight_class"),
                "result": row["bout_outcome"], "method": row["bout_method_of_victory"],
                "ending_round": row["bout_ending_round"],
                "opponent_prior_record": record, "opponent_prior_elo": round(ratings[oid], 2),
                "opponent_strength_percentile": rank, "opponent_tier": tier,
                "strength_cohort": basis, "strength_cohort_size": len(values),
                "strength_provisional": decided < 3,
                "opponent_style_before_bout": style(oid, day, row.get("weight_class")),
                "performance": _performance([row]),
                "control_seconds": row.get("bout_control_time_seconds") if row.get("bout_stats_available") else None,
                "opponent_control_seconds": row.get("opponent_bout_control_time_seconds") if row.get("bout_stats_available") else None,
                "minutes": duration,
            })
        # Both perspectives use ratings and histories from before this date.
        changes = defaultdict(float)
        for row in bouts:
            score = {"win": 1.0, "loss": 0.0, "draw": 0.5}.get(row["bout_outcome"])
            if score is not None:
                fid, oid = row["fighter_id"], row["opponent_id"]
                expected = 1 / (1 + 10 ** ((ratings[oid] - ratings[fid]) / 400))
                changes[fid] += 32 * (score - expected)
        for fid, change in changes.items():
            ratings[fid] += change
        for row in bouts:
            histories[row["fighter_id"]].append(row)
        for fid in {r["fighter_id"] for r in bouts}:
            profiles[fid] = metrics(histories[fid])

    current_styles = {fid: style(fid, cutoff, histories[fid][-1].get("weight_class") if histories[fid] else None)
                      for fid in targets}

    def fighter_context(identity, opponent_id):
        fid = identity["fighter_id"]
        history, bouts = histories[fid], evidence[fid]
        target_style = current_styles[opponent_id]
        by_style = []
        for tag in STYLE_TAGS.values():
            ids = {r["fight_id"] for r in bouts if tag in r["opponent_style_before_bout"]["tags"]}
            matching = [r for r in history if r["fight_id"] in ids]
            by_style.append({"opponent_style": tag, **_performance(matching), "fight_ids": sorted(ids)})
        similar = []
        for bout in bouts:
            historical = bout["opponent_style_before_bout"]
            common = [k for k in STYLE_KEYS if historical["percentiles"][k] is not None and target_style["percentiles"][k] is not None]
            if len(common) < 3:
                continue
            similarity = 1 - mean(abs(historical["percentiles"][k] - target_style["percentiles"][k]) for k in common)
            if similarity >= 0.75:
                similar.append({"fight_id": bout["fight_id"], "opponent_name": bout["opponent_name"],
                                "date": bout["date"], "result": bout["result"], "method": bout["method"],
                                "opponent_tier": bout["opponent_tier"], "performance": bout["performance"],
                                "similarity": round(similarity, 3), "compared_metrics": len(common)})
        similar.sort(key=lambda r: (-r["similarity"], r["fight_id"]))
        known = [r for r in bouts if not r["strength_provisional"]]
        wins = [r for r in known if r["result"] == "win"]
        losses = [r for r in known if r["result"] == "loss"]
        born = date.fromisoformat(identity["date_of_birth"]) if identity.get("date_of_birth") else None
        day = date.fromisoformat(cutoff)
        return {"fighter": identity, "career": metrics(history), "recent": metrics(history[-5:]),
                "age": day.year - born.year - ((day.month, day.day) < (born.month, born.day)) if born else None,
                "days_since_last_fight": (day - date.fromisoformat(history[-1]["event_date"])).days if history else None,
                "current_derived_elo": round(ratings[fid], 2), "current_style": current_styles[fid],
                "opposition": {
                    "average_opponent_prior_elo": mean(r["opponent_prior_elo"] for r in known) if known else None,
                    "provisional_opponents": len(bouts) - len(known),
                    "strongest_win_ids": [r["fight_id"] for r in sorted(wins, key=lambda r: -r["opponent_prior_elo"])[:3]],
                    "strongest_loss_ids": [r["fight_id"] for r in sorted(losses, key=lambda r: -r["opponent_prior_elo"])[:3]],
                    "performance_by_tier": {tier: _performance([row for row in history if row["fight_id"] in {
                        r["fight_id"] for r in bouts if r["opponent_tier"] == tier}])
                        for tier in ("higher", "middle", "lower", "unknown")},
                }, "history_by_opponent_style": by_style,
                "next_opponent_style": target_style,
                "history_against_next_opponent_tags": [group for group in by_style if group["opponent_style"] in target_style["tags"]],
                "similar_to_next_opponent": similar[:5], "comparable_style_count": len(similar), "bouts": bouts}

    return {"as_of_date": cutoff, "a": fighter_context(a, b["fighter_id"]),
            "b": fighter_context(b, a["fighter_id"]),
            "methodology": {
                "elo": "Derived, uncalibrated opponent-adjusted rating: initial 1500, K=32, logistic scale 400; no finish bonus. Same-day updates apply after all evidence snapshots.",
                "tiers": "Higher/middle/lower thirds of prior Elo among active peers; at least 3 prior decided bouts, else unknown. Division cohort preferred when at least 8 active fighters are available.",
                "styles": "Offensive rate >=75th percentile among active peers with at least 3 timed-stat bouts. Opponent profiles are frozen before each historical bout.",
                "similarity": "1 minus mean absolute percentile difference across at least 3 metrics; >=0.75 selects analogues. Heuristic similarity, not a win probability.",
            }, "limitations": [
                "Records cover stored UFCStats bouts, not total professional MMA careers.",
                "Historical rankings and championship status are not stored; Elo/tier estimates do not establish either.",
                "Styles and relative strength are statistical proxies, sensitive to sample size, era, and weight class.",
                "All current profiles exclude the target date; every historical opponent profile excludes its own bout date and later fights.",
                "Direct submission defense, counter-strike timing, judging scorecards, injuries, and preparation are unavailable.",
                "A split decision is a method label, not proof of a statistically close performance. Absence of submission losses is not proof of submission defense.",
            ]}
