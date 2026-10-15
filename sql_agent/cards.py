"""Read the saved next-card schedule and predict its pairings independently."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable


def load_remaining_card(event: str, *, exclude_fights: list[str] | None = None) -> dict:
    from pipeline.scraper import RateLimitedPlaywrightScraper

    scraper = RateLimitedPlaywrightScraper()
    try:
        card = scraper.crawl_remaining_card(event)
    finally:
        scraper.close()
    excluded = set()
    for query in exclude_fights or []:
        matches = [f for f in card["fights"] if query.strip().casefold() in {
            f["fight_id"], f["fighter_a_name"].casefold(), f["fighter_b_name"].casefold(),
        }]
        if len(matches) != 1:
            raise ValueError(f"Exclusion {query!r} must match one remaining fight ID or exact fighter name")
        excluded.add(matches[0]["fight_id"])
    card["excluded_fights"] = [f for f in card["fights"] if f["fight_id"] in excluded]
    card["fights"] = [f for f in card["fights"] if f["fight_id"] not in excluded]
    return card


def load_next_card(db_path: str | Path, *, today: date | None = None) -> dict:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    today = today or date.today()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"upcoming_events", "upcoming_fights"}.issubset(tables):
            raise ValueError("No upcoming schedule yet. Run scripts/update_ufc_data.ps1 first.")
        connection.execute("BEGIN")
        event = connection.execute(
            "SELECT u.* FROM upcoming_events u WHERE u.event_date >= ? "
            "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.event_url = u.event_url) "
            "ORDER BY u.event_date, u.event_id LIMIT 1", (today.isoformat(),),
        ).fetchone()
        if event is None:
            raise ValueError("No upcoming card is saved, or the saved card has passed. Run the scraper again.")
        fights = [dict(row) for row in connection.execute(
            "SELECT * FROM upcoming_fights WHERE event_id = ? ORDER BY card_order", (event["event_id"],))]
        if not fights or len(fights) != event["source_fight_count"]:
            raise ValueError("Saved upcoming card is incomplete. Run the scraper again.")
    card = {**dict(event), "fights": fights, "caveats": []}
    fetched = datetime.fromisoformat(card["last_scraped_at"])
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - fetched).total_seconds() > 48 * 3600:
        card["caveats"].append("Schedule was fetched more than 48 hours ago; refresh for withdrawals or replacements.")
    if card["event_date"] == today.isoformat():
        card["caveats"].append("This card is today; UFCStats has no start time here. Refresh before predicting to check it has not started.")
    return card


def predict_card(agent, card: dict, *, as_of_date: str | None = None,
                 on_result: Callable[[dict], None] | None = None,
                 on_progress: Callable[[str], None] | None = None,
                 refresh_remaining: Callable[[], dict] | None = None) -> dict:
    cutoff = as_of_date or card["event_date"]
    if date.fromisoformat(cutoff) > date.fromisoformat(card["event_date"]):
        raise ValueError("Card analysis cutoff cannot be after the card date")
    results = []
    skipped = []

    def still_remaining(fight):
        if refresh_remaining is None:
            return True
        fresh = refresh_remaining()
        if fresh["event_id"] != card["event_id"] or fresh["event_date"] != card["event_date"]:
            raise ValueError("Refreshed event identity/date changed; restart card analysis")
        return any(all(f.get(k) == fight.get(k) for k in ("fight_id", "fighter_a_id", "fighter_b_id"))
                   for f in fresh["fights"])

    def skip(fight):
        skipped.append({"fight_id": fight["fight_id"], "fighter_a": fight["fighter_a_name"],
                        "fighter_b": fight["fighter_b_name"], "reason": "Result posted or pairing removed/changed during analysis"})
        if on_progress:
            on_progress(f"Skipping {fight['fighter_a_name']} vs {fight['fighter_b_name']}: no longer an unchanged remaining matchup")

    for position, fight in enumerate(card["fights"], 1):
        if not still_remaining(fight):
            skip(fight)
            continue
        if on_progress:
            on_progress(f"[{position}/{len(card['fights'])}] "
                        f"{fight['fighter_a_name']} vs {fight['fighter_b_name']}")
        rounds = fight["scheduled_rounds"]
        rounds_basis = "source"
        if rounds is None:
            rounds = 5 if fight["card_order"] == 1 or fight["is_title_bout"] else 3
            rounds_basis = "assumed main event/title distance" if rounds == 5 else "assumed standard distance"
        try:
            identity_args = {}
            if card.get("live_remaining"):
                identity_args["fighter_identities"] = [
                    {"fighter_id": fight[f"fighter_{side}_id"], "fighter_name": fight[f"fighter_{side}_name"]}
                    for side in ("a", "b")
                ]
            result = agent.predict(fight["fighter_a_id"], fight["fighter_b_id"], cutoff,
                                   scheduled_rounds=rounds, rounds_basis=rounds_basis,
                                   event_name=card["event_name"],
                                   on_progress=on_progress, **identity_args)
        except Exception as exc:
            # An unavailable fighter/model response must not discard other bouts.
            result = {"fighter_a": fight["fighter_a_name"], "fighter_b": fight["fighter_b_name"],
                      "error": str(exc), "model_usage": getattr(exc, "model_usage", None),
                      "review_history": getattr(exc, "review_history", [])}
        if not still_remaining(fight):
            skip(fight)
            continue
        result.update(fight_id=fight["fight_id"], card_order=fight["card_order"], weight_class=fight["weight_class"])
        results.append(result)
        if on_result:
            on_result(result)
    return {"event_id": card["event_id"], "event_name": card["event_name"],
            "event_date": card["event_date"], "as_of_date": cutoff,
            "schedule_fetched_at": card["last_scraped_at"], "caveats": card["caveats"],
            "completed_fights": card.get("completed_fights", 0),
            "skipped_fights": skipped, "excluded_fights": card.get("excluded_fights", []),
            "predictions": results, "failed_fights": sum("error" in r for r in results)}
