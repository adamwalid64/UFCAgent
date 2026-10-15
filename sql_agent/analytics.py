"""Deterministic, documented measurements; prediction reasoning lives in the prompt."""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from datetime import date
from statistics import mean

from .database import QuerySession


def is_finish(method: str | None) -> bool:
    return (method or "").lower() in {"ko/tko", "submission"}


def duration_minutes(row: dict) -> float | None:
    ending = row.get("bout_ending_time") or ""
    if not re.fullmatch(r"\d+:\d{2}", ending):
        return None
    minutes, seconds = map(int, ending.split(":"))
    rnd = row.get("bout_ending_round")
    if not rnd or rnd < 1 or seconds >= 60:
        return None
    elapsed = minutes * 60 + seconds
    match = re.search(r"\(([\d-]+)\)", row.get("bout_time_format") or "")
    if match:
        lengths = [int(n) * 60 for n in match[1].split("-")]
        if rnd > len(lengths) or elapsed > lengths[rnd - 1]:
            return None
        elapsed += sum(lengths[:rnd - 1])
    elif rnd != 1 or row.get("bout_time_format") != "No Time Limit":
        return None
    return elapsed / 60 if elapsed > 0 else None


def _ratio(rows: list[dict], numerator: str, denominator: str, *, defense=False):
    usable = [r for r in rows if r.get("bout_stats_available") and
              r.get(numerator) is not None and r.get(denominator) is not None]
    den = sum(r[denominator] for r in usable)
    if not den:
        return None
    value = sum(r[numerator] for r in usable) / den
    return 1 - value if defense else value


def _rate(rows: list[dict], column: str, per_minutes: int = 1):
    usable = [(r[column], duration_minutes(r)) for r in rows
              if r.get("bout_stats_available") and r.get(column) is not None]
    usable = [(value, duration) for value, duration in usable if duration is not None]
    minutes = sum(duration for _, duration in usable)
    return sum(value for value, _ in usable) * per_minutes / minutes if minutes else None


def metrics(rows: list[dict]) -> dict:
    outcomes = Counter(r["bout_outcome"] for r in rows)
    wins = outcomes["win"]
    return {
        "fights": len(rows),
        "record": {k: outcomes[k] for k in ("win", "loss", "draw", "no_contest")},
        "unknown_results": outcomes[None],
        "stat_fights": sum(bool(r.get("bout_stats_available")) for r in rows),
        "timed_stat_fights": sum(bool(r.get("bout_stats_available")) and
                                 duration_minutes(r) is not None for r in rows),
        "sig_strikes_landed_per_min": _rate(rows, "bout_significant_strikes_landed"),
        "sig_strikes_absorbed_per_min": _rate(rows, "opponent_bout_significant_strikes_landed"),
        "striking_accuracy": _ratio(rows, "bout_significant_strikes_landed",
                                    "bout_significant_strikes_attempted"),
        "striking_defense": _ratio(rows, "opponent_bout_significant_strikes_landed",
                                   "opponent_bout_significant_strikes_attempted", defense=True),
        "takedowns_per_15_min": _rate(rows, "bout_takedowns_landed", 15),
        "takedown_accuracy": _ratio(rows, "bout_takedowns_landed", "bout_takedowns_attempted"),
        "takedown_defense": _ratio(rows, "opponent_bout_takedowns_landed",
                                   "opponent_bout_takedowns_attempted", defense=True),
        "sub_attempts_per_15_min": _rate(rows, "bout_submission_attempts", 15),
        "opponent_sub_attempts_per_15_min": _rate(rows, "opponent_bout_submission_attempts", 15),
        "finish_rate_among_wins": sum(r["bout_outcome"] == "win" and
                                      is_finish(r["bout_method_of_victory"]) for r in rows) / wins if wins else None,
        "submission_losses": sum(r["bout_outcome"] == "loss" and
                                 r["bout_method_of_victory"] == "Submission" for r in rows),
        "wins_by_method": dict(Counter(r["bout_method_of_victory"] or "Unknown"
                                       for r in rows if r["bout_outcome"] == "win")),
        "losses_by_method": dict(Counter(r["bout_method_of_victory"] or "Unknown"
                                         for r in rows if r["bout_outcome"] == "loss")),
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _quality(record: Counter) -> float | None:
    n = record["win"] + record["loss"] + record["draw"]
    return (record["win"] + 0.5 * record["draw"] + 1) / (n + 2) if n else None


class MatchupTools:
    def __init__(self, session: QuerySession):
        self.session = session

    def _fighter(self, fighter: str, as_of_date: str | None):
        self.session.check_date(as_of_date)
        return self.session.resolve_fighter(fighter)

    def get_fighter_history(self, fighter: str, as_of_date: str | None = None, *,
                            purpose: str = "Inspect fighter history") -> dict:
        identity = self._fighter(fighter, as_of_date)
        rows = self.session.history_rows([identity["fighter_id"]], purpose)
        # History is intentionally complete, including all per-bout and opponent stats.
        return {"fighter": identity, "as_of_date": self.session.as_of_date, "fights": rows}

    def get_career_stats(self, fighter: str, as_of_date: str | None = None, *,
                         recent_n: int = 5, purpose: str = "Measure career and recent form") -> dict:
        if not 3 <= recent_n <= 5:
            raise ValueError("recent_n must be between 3 and 5")
        identity = self._fighter(fighter, as_of_date)
        rows = self.session.history_rows([identity["fighter_id"]], purpose)
        career, recent = metrics(rows), metrics(rows[-recent_n:])
        streak = 0
        for row in reversed(rows):
            if row["bout_outcome"] != "win":
                break
            streak += 1
        cutoff = date.fromisoformat(self.session.as_of_date)
        dob = identity.get("date_of_birth")
        age = None
        if dob:
            born = date.fromisoformat(dob)
            age = cutoff.year - born.year - ((cutoff.month, cutoff.day) < (born.month, born.day))
        trend_keys = ("sig_strikes_landed_per_min", "sig_strikes_absorbed_per_min",
                      "takedowns_per_15_min", "sub_attempts_per_15_min")
        return {
            "fighter": identity, "as_of_date": self.session.as_of_date,
            "career": career, "recent": recent, "recent_n": recent_n,
            "recent_results": [{"fight_id": r["fight_id"], "date": r["event_date"],
                                "opponent": r["opponent_name"], "result": r["bout_outcome"],
                                "method": r["bout_method_of_victory"]} for r in rows[-recent_n:]],
            "recent_minus_career": {key: recent[key] - career[key]
                                    if recent[key] is not None and career[key] is not None else None
                                    for key in trend_keys},
            "win_streak": streak,
            "days_since_last_fight": (cutoff - date.fromisoformat(rows[-1]["event_date"])).days if rows else None,
            "age": age,
            "caveats": ["Records cover the stored UFCStats history only.",
                        "DOB is from a non-versioned profile; age availability at the cutoff is unverified.",
                        "Missing stats/durations are excluded per metric, not filled with zero."] +
                       (["No eligible fights before cutoff (dataset debut)."] if not rows else []) +
                       (["Fewer than three eligible fights; recent form is thin."] if len(rows) < 3 else []),
        }

    def get_common_opponents(self, a: str, b: str, as_of_date: str | None = None, *,
                             purpose: str = "Compare shared opponents") -> dict:
        a_id = self._fighter(a, as_of_date)["fighter_id"]
        b_id = self._fighter(b, as_of_date)["fighter_id"]
        rows = self.session.history_rows([a_id, b_id], purpose)
        by_fighter = {fid: defaultdict(list) for fid in (a_id, b_id)}
        for row in rows:
            by_fighter[row["fighter_id"]][row["opponent_id"]].append({
                "fight_id": row["fight_id"], "date": row["event_date"],
                "result": row["bout_outcome"], "method": row["bout_method_of_victory"],
                "round": row["bout_ending_round"],
            })
        shared = sorted(by_fighter[a_id].keys() & by_fighter[b_id].keys())
        return {"as_of_date": self.session.as_of_date, "common_opponents": [
            {"opponent_id": fid, "opponent_name": self.session.identities[fid]["fighter_name"],
             "a_results": by_fighter[a_id][fid], "b_results": by_fighter[b_id][fid]} for fid in shared]}

    def get_mma_math_paths(self, a: str, b: str, as_of_date: str | None = None, *,
                           max_depth: int = 3, half_life_days: float = 1095.75,
                           finish_multiplier: float = 1.1, example_limit: int = 5,
                           purpose: str = "Measure both directions of MMA math") -> dict:
        if not 1 <= max_depth <= 5 or half_life_days <= 0 or not 1 <= finish_multiplier <= 1.5:
            raise ValueError("max_depth must be 1..5, half_life_days > 0, finish_multiplier 1..1.5")
        if not 1 <= example_limit <= 200:
            raise ValueError("example_limit must be 1..200")
        a_id = self._fighter(a, as_of_date)["fighter_id"]
        b_id = self._fighter(b, as_of_date)["fighter_id"]
        rows = self.session.history_rows(None, purpose)
        edges = defaultdict(list)
        cutoff = date.fromisoformat(self.session.as_of_date)
        for row in rows:
            if row["bout_outcome"] != "win":
                continue
            age = (cutoff - date.fromisoformat(row["event_date"])).days
            edges[row["fighter_id"]].append({
                "winner_id": row["fighter_id"], "loser_id": row["opponent_id"],
                "fight_id": row["fight_id"], "date": row["event_date"],
                "method": row["bout_method_of_victory"],
                "weight": 2 ** (-age / half_life_days) *
                          (finish_multiplier if is_finish(row["bout_method_of_victory"]) else 1),
            })
        deadline = time.monotonic() + self.session.timeout

        def direction(start, target):
            count, total, strongest = 0, 0.0, []

            def visit(current, visited, path, product):
                nonlocal count, total, strongest
                if time.monotonic() > deadline:
                    raise TimeoutError("MMA path search timed out; no partial counts returned")
                if len(path) >= max_depth:
                    return
                for edge in edges[current]:
                    next_id = edge["loser_id"]
                    if next_id in visited:
                        continue
                    next_path, next_product = path + [edge], product * edge["weight"]
                    if next_id == target:
                        score = next_product / len(next_path) ** 2
                        count += 1
                        total += score
                        strongest.append({"fighter_ids": [start] + [e["loser_id"] for e in next_path],
                                          "fights": next_path, "weight": score})
                        strongest = sorted(strongest, key=lambda p: (-p["weight"],
                                            [e["fight_id"] for e in p["fights"]]))[:example_limit]
                    else:
                        visit(next_id, visited | {next_id}, next_path, next_product)

            visit(start, {start}, [], 1.0)
            return {"path_count": count, "weighted_score": total, "strongest_paths": strongest}

        forward, reverse = direction(a_id, b_id), direction(b_id, a_id)
        return {"as_of_date": self.session.as_of_date, "a_to_b": forward, "b_to_a": reverse,
                "conflicting_evidence": bool(forward["path_count"] and reverse["path_count"]),
                "signal_strength": "weak", "max_depth": max_depth,
                "weight_formula": "product(2^(-age_days/half_life_days) * finish_bonus) / edges^2",
                "half_life_days": half_life_days, "finish_multiplier": finish_multiplier,
                "path_definition": "All simple fighter paths; separate bouts count as separate edges."}

    def get_opponent_quality(self, fighter: str, as_of_date: str | None = None, *,
                              lower_percentile: float = 1 / 3, upper_percentile: float = 2 / 3,
                              purpose: str = "Measure opposition at each historical fight") -> dict:
        if not 0 < lower_percentile < upper_percentile < 1:
            raise ValueError("Tier percentiles must satisfy 0 < lower < upper < 1")
        fid = self._fighter(fighter, as_of_date)["fighter_id"]
        rows = self.session.history_rows(None, purpose)
        by_day = defaultdict(list)
        for row in rows:
            by_day[row["event_date"]].append(row)
        records, opposition = defaultdict(Counter), []
        for day, bouts in sorted(by_day.items()):
            relevant = [r for r in bouts if r["fighter_id"] == fid]
            if relevant:
                population = [q for record in records.values() if (q := _quality(record)) is not None]
                lower, upper = percentile(population, lower_percentile), percentile(population, upper_percentile)
                for row in relevant:
                    record = records[row["opponent_id"]]
                    n = record["win"] + record["loss"] + record["draw"]
                    quality = _quality(record)
                    # Equal distributions contain no data-supported top/low distinction.
                    tier = ("unknown" if quality is None else "mid" if lower == upper else
                            "top" if quality > upper else "low" if quality < lower else "mid")
                    opposition.append({
                        "fight_id": row["fight_id"], "date": day, "opponent_id": row["opponent_id"],
                        "opponent_name": row["opponent_name"], "result": row["bout_outcome"],
                        "opponent_prior_record": {k: record[k] for k in ("win", "loss", "draw", "no_contest")},
                        "opponent_prior_win_pct": record["win"] / n if n else None,
                        "quality": quality, "tier": tier, "roster_size": len(population),
                        "tier_cutoffs": {"lower": lower, "upper": upper},
                    })
            # Entire same-day batch is applied afterwards: strictly earlier dates only.
            for row in bouts:
                records[row["fighter_id"]][row["bout_outcome"]] += 1
        known = [r["quality"] for r in opposition if r["quality"] is not None]
        wins = [r for r in opposition if r["result"] == "win" and r["quality"] is not None]
        return {"as_of_date": self.session.as_of_date, "opponents": opposition,
                "average_opponent_quality": mean(known) if known else None,
                "unknown_quality_fights": sum(r["quality"] is None for r in opposition),
                "best_win": max(wins, key=lambda r: (r["quality"], r["date"])) if wins else None,
                "record_by_tier": {tier: dict(Counter(r["result"] for r in opposition if r["tier"] == tier))
                                   for tier in ("top", "mid", "low", "unknown")},
                "quality_formula": "(prior wins + 0.5 * prior draws + 1) / (prior W+L+D + 2)",
                "tier_percentiles": [lower_percentile, upper_percentile],
                "ranking": None, "elo": None,
                "caveats": ["No rankings or Elo in source; record-based proxy only.",
                            "Each tier uses roster records strictly before that fight date.",
                            "No prior decided bouts means unknown quality; tied cutoffs use mid tier."]}

    def get_style_matchup(self, a: str, b: str, as_of_date: str | None = None, *,
                          high_percentile: float = 0.75,
                          purpose: str = "Compare styles against historical roster percentiles") -> dict:
        if not 0.5 < high_percentile < 1:
            raise ValueError("high_percentile must be between 0.5 and 1")
        a_id = self._fighter(a, as_of_date)["fighter_id"]
        b_id = self._fighter(b, as_of_date)["fighter_id"]
        rows = self.session.history_rows(None, purpose)
        histories = defaultdict(list)
        for row in rows:
            histories[row["fighter_id"]].append(row)
        roster = {fid: metrics(history) for fid, history in histories.items()}
        keys = ("sig_strikes_landed_per_min", "striking_defense", "takedowns_per_15_min",
                "takedown_defense", "sub_attempts_per_15_min")
        distributions = {key: [m[key] for m in roster.values() if m[key] is not None] for key in keys}

        def classify(fid):
            stats = roster.get(fid, metrics([]))
            ranks = {}
            for key, values in distributions.items():
                value = stats[key]
                ranks[key] = ((sum(v < value for v in values) + 0.5 * sum(v == value for v in values)) /
                              len(values) if value is not None and values else None)
            tags = []
            for key, tag in (("sig_strikes_landed_per_min", "high-volume striker"),
                             ("takedowns_per_15_min", "wrestler"),
                             ("sub_attempts_per_15_min", "grappler/submission specialist")):
                if ranks[key] is not None and ranks[key] >= high_percentile and stats[key] > 0:
                    tags.append(tag)
            if all(ranks[key] is not None and ranks[key] >= 0.5 and stats[key] > 0 for key in
                   ("sig_strikes_landed_per_min", "takedowns_per_15_min", "sub_attempts_per_15_min")):
                tags.append("well-rounded")
            return {"fighter_id": fid, "tags": tags, "percentiles": ranks, "stats": stats}

        first, second = classify(a_id), classify(b_id)

        def comparisons(offense, defense):
            return {
                "takedowns": {"offense_per_15_min": offense["takedowns_per_15_min"],
                              "offense_accuracy": offense["takedown_accuracy"],
                              "opponent_defense": defense["takedown_defense"]},
                "striking": {"output_per_min": offense["sig_strikes_landed_per_min"],
                             "opponent_absorbed_per_min": defense["sig_strikes_absorbed_per_min"],
                             "opponent_defense": defense["striking_defense"]},
                "submissions": {"attempts_per_15_min": offense["sub_attempts_per_15_min"],
                                "opponent_submission_defense": None,
                                "opponent_submission_losses": defense["submission_losses"],
                                "opponent_attempts_faced_per_15_min": defense["opponent_sub_attempts_per_15_min"]},
            }

        return {"as_of_date": self.session.as_of_date, "a": first, "b": second,
                "a_offense_vs_b": comparisons(first["stats"], second["stats"]),
                "b_offense_vs_a": comparisons(second["stats"], first["stats"]),
                "roster_size": len(roster), "high_percentile": high_percentile,
                "metric_roster_sizes": {k: len(v) for k, v in distributions.items()},
                "caveats": ["Roster includes only fighters with eligible bouts; percentiles use midranks.",
                            "Style tags are statistical proxies, with no minimum sample guarantee.",
                            "Counter-strike timing and direct submission defense are unavailable."]}
