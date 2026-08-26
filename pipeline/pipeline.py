from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as database
from .models import Event, Fight, FightRound, Fighter


def slugify(value: str | None) -> str:
    if value is None:
        return "unknown"
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "unknown"


def parse_event_date(raw_date: str | date | None) -> date | None:
    if raw_date is None:
        return None
    if isinstance(raw_date, date):
        return raw_date
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%dT%H:%M:%S%z"]:
        try:
            return datetime.strptime(str(raw_date), fmt).date()
        except ValueError:
            continue
    return None


def build_fighter_id(name: str | None) -> str:
    return slugify(name) or "unknown-fighter"


def build_event_record(event_payload: dict[str, Any]) -> dict[str, Any]:
    event_date = parse_event_date(event_payload.get("event_date"))
    event_name = (event_payload.get("event_name") or "Unknown UFC Event").strip()
    event_id = slugify(f"{event_name} {event_date.isoformat() if event_date else 'unknown-date'}")
    return {
        "event_id": event_id,
        "event_name": event_name,
        "event_date": event_date,
        "event_location": event_payload.get("event_location") or "Unknown Location",
        "event_url": event_payload.get("event_url"),
    }


def build_fight_record(
    event: Event,
    fight_id: str,
    fighter_a_name: str,
    fighter_b_name: str,
    winner_name: str | None = None,
    method_of_victory: str | None = None,
    ending_round: int | None = None,
    ending_time: str | None = None,
    fighter_a_stats: dict[str, Any] | None = None,
    fighter_b_stats: dict[str, Any] | None = None,
    source_fight_url: str | None = None,
    head_to_head_fight_count: int = 0,
    fighter_a_id: str | None = None,
    fighter_b_id: str | None = None,
    method_detail: str | None = None,
    result: str | None = None,
    referee: str | None = None,
    time_format: str | None = None,
) -> dict[str, Any]:
    fighter_a_stats = fighter_a_stats or {}
    fighter_b_stats = fighter_b_stats or {}

    fighter_a_id = fighter_a_id or build_fighter_id(fighter_a_name)
    fighter_b_id = fighter_b_id or build_fighter_id(fighter_b_name)
    winner_fighter_id = build_fighter_id(winner_name) if winner_name else None

    return {
        "fight_id": fight_id,
        "event_id": event.event_id,
        "fighter_a_id": fighter_a_id,
        "fighter_a_name": fighter_a_name,
        "fighter_a_age": fighter_a_stats.get("age"),
        "fighter_a_height_inches": fighter_a_stats.get("height_inches"),
        "fighter_a_reach_inches": fighter_a_stats.get("reach_inches"),
        "fighter_a_stance": fighter_a_stats.get("stance"),
        "fighter_a_wins": fighter_a_stats.get("wins"),
        "fighter_a_losses": fighter_a_stats.get("losses"),
        "fighter_a_draws": fighter_a_stats.get("draws"),
        "fighter_a_current_win_streak": fighter_a_stats.get("current_win_streak"),
        "fighter_a_current_loss_streak": fighter_a_stats.get("current_loss_streak"),
        "fighter_a_significant_strikes_landed": fighter_a_stats.get("significant_strikes_landed"),
        "fighter_a_significant_strikes_attempted": fighter_a_stats.get("significant_strikes_attempted"),
        "fighter_a_takedowns_landed": fighter_a_stats.get("takedowns_landed"),
        "fighter_a_takedowns_attempted": fighter_a_stats.get("takedowns_attempted"),
        "fighter_a_control_time_seconds": fighter_a_stats.get("control_time_seconds"),
        "fighter_a_total_strikes_landed": fighter_a_stats.get("total_strikes_landed"),
        "fighter_a_total_strikes_attempted": fighter_a_stats.get("total_strikes_attempted"),
        "fighter_a_knockdowns": fighter_a_stats.get("knockdowns"),
        "fighter_a_submission_attempts": fighter_a_stats.get("submission_attempts"),
        "fighter_a_reversals": fighter_a_stats.get("reversals"),
        "fighter_a_days_since_last_fight": fighter_a_stats.get("days_since_last_fight"),
        "fighter_b_id": fighter_b_id,
        "fighter_b_name": fighter_b_name,
        "fighter_b_age": fighter_b_stats.get("age"),
        "fighter_b_height_inches": fighter_b_stats.get("height_inches"),
        "fighter_b_reach_inches": fighter_b_stats.get("reach_inches"),
        "fighter_b_stance": fighter_b_stats.get("stance"),
        "fighter_b_wins": fighter_b_stats.get("wins"),
        "fighter_b_losses": fighter_b_stats.get("losses"),
        "fighter_b_draws": fighter_b_stats.get("draws"),
        "fighter_b_current_win_streak": fighter_b_stats.get("current_win_streak"),
        "fighter_b_current_loss_streak": fighter_b_stats.get("current_loss_streak"),
        "fighter_b_significant_strikes_landed": fighter_b_stats.get("significant_strikes_landed"),
        "fighter_b_significant_strikes_attempted": fighter_b_stats.get("significant_strikes_attempted"),
        "fighter_b_takedowns_landed": fighter_b_stats.get("takedowns_landed"),
        "fighter_b_takedowns_attempted": fighter_b_stats.get("takedowns_attempted"),
        "fighter_b_control_time_seconds": fighter_b_stats.get("control_time_seconds"),
        "fighter_b_total_strikes_landed": fighter_b_stats.get("total_strikes_landed"),
        "fighter_b_total_strikes_attempted": fighter_b_stats.get("total_strikes_attempted"),
        "fighter_b_knockdowns": fighter_b_stats.get("knockdowns"),
        "fighter_b_submission_attempts": fighter_b_stats.get("submission_attempts"),
        "fighter_b_reversals": fighter_b_stats.get("reversals"),
        "fighter_b_days_since_last_fight": fighter_b_stats.get("days_since_last_fight"),
        "winner_fighter_id": winner_fighter_id,
        "winner_name": winner_name,
        "method_of_victory": method_of_victory,
        "method_detail": method_detail,
        "result": result,
        "referee": referee,
        "time_format": time_format,
        "ending_round": ending_round,
        "ending_time": ending_time,
        "head_to_head_fight_count": head_to_head_fight_count,
        "source_fight_url": source_fight_url,
    }


def ensure_event(session: Session, event_payload: dict[str, Any]) -> Event:
    event_record = build_event_record(event_payload)
    event = session.get(Event, event_record["event_id"])
    if event is None:
        event = Event(**event_record)
        session.add(event)
    else:
        event.event_name = event_record["event_name"]
        event.event_date = event_record["event_date"]
        event.event_location = event_record["event_location"]
        event.event_url = event_record["event_url"]
    session.flush()
    return event


def upsert_fight(session: Session, fight_payload: dict[str, Any]) -> Fight:
    fight = session.get(Fight, fight_payload["fight_id"])
    if fight is None:
        fight = Fight(**fight_payload)
        session.add(fight)
    else:
        for key, value in fight_payload.items():
            setattr(fight, key, value)
    session.flush()
    return fight


def ensure_fighter(session: Session, fighter_id: str, name: str, profile_url: str | None = None) -> Fighter:
    fighter = session.get(Fighter, fighter_id)
    if fighter is None:
        fighter = Fighter(fighter_id=fighter_id, fighter_name=name, profile_url=profile_url)
        session.add(fighter)
    else:
        fighter.fighter_name = name
        fighter.profile_url = profile_url or fighter.profile_url
    session.flush()
    return fighter


def _prior_stats(fighter: Fighter, event_date: date) -> dict[str, Any]:
    return {
        "wins": fighter.wins, "losses": fighter.losses, "draws": fighter.draws,
        "current_win_streak": fighter.current_win_streak, "current_loss_streak": fighter.current_loss_streak,
        "days_since_last_fight": (event_date - fighter.last_fight_date).days if fighter.last_fight_date else None,
    }


def _update_aggregate(fighter: Fighter, stats: dict[str, Any], outcome: str, event_date: date) -> None:
    fighter.fights += 1
    if outcome == "win":
        fighter.wins += 1
        fighter.current_win_streak += 1
        fighter.current_loss_streak = 0
    elif outcome == "loss":
        fighter.losses += 1
        fighter.current_loss_streak += 1
        fighter.current_win_streak = 0
    elif outcome == "draw":
        fighter.draws += 1
        fighter.current_win_streak = 0
        fighter.current_loss_streak = 0
    elif outcome == "no_contest":
        fighter.no_contests += 1
    for field in ("significant_strikes_landed", "significant_strikes_attempted", "takedowns_landed", "takedowns_attempted", "control_time_seconds"):
        setattr(fighter, field, (getattr(fighter, field) or 0) + (stats.get(field) or 0))
    fighter.last_fight_date = event_date


def _upsert_rounds(session: Session, fight_id: str, rounds: list[dict[str, Any]], a_id: str, b_id: str) -> None:
    for round_payload in rounds:
        number = round_payload["round_number"]
        for side, fighter_id, opponent_id in (("fighter_a", a_id, b_id), ("fighter_b", b_id, a_id)):
            values = round_payload.get(side) or {}
            existing = session.scalar(select(FightRound).where(FightRound.fight_id == fight_id, FightRound.round_number == number, FightRound.fighter_id == fighter_id))
            record = {key: values.get(key) for key in ("knockdowns", "significant_strikes_landed", "significant_strikes_attempted", "total_strikes_landed", "total_strikes_attempted", "takedowns_landed", "takedowns_attempted", "submission_attempts", "reversals", "control_time_seconds")}
            record.update({"fight_id": fight_id, "round_number": number, "fighter_id": fighter_id, "opponent_id": opponent_id})
            if existing is None:
                session.add(FightRound(**record))
            else:
                for key, value in record.items():
                    setattr(existing, key, value)


def run_pipeline(scraper, full: bool = False, max_events: int | None = None) -> tuple[int, int, list[str]]:
    session = database.SessionLocal()
    inserted_events = 0
    inserted_fights = 0
    errors: list[str] = []

    try:
        existing_event_ids = {row[0] for row in session.execute(select(Event.event_id)).all()}
        event_summary = scraper.crawl_event_listing(max_events=max_events)
        errors.extend(f"Listing failed: {error.source_url}: {error.message}" for error in getattr(event_summary, "errors", []))

        pending: list[dict[str, Any]] = []
        for event_url in event_summary.event_urls:
            try:
                payload = scraper.crawl_event_details(event_url)
                if not full and build_event_record(payload)["event_id"] in existing_event_ids:
                    break
                pending.append(payload)
            except Exception as exc:
                errors.append(f"Event page failed: {event_url}: {exc}")
        # UFCStats is newest-first; aggregation and pre-fight features require chronological ingestion.
        pending.sort(key=lambda payload: parse_event_date(payload.get("event_date")) or date.min)

        total_pending = len(pending)
        logging.info("%s event(s) queued in chronological order", total_pending)
        for event_index, event_payload in enumerate(pending, 1):
            try:
                logging.info("[%s/%s] %s", event_index, total_pending, event_payload.get("event_name") or event_payload.get("event_url"))
                event = ensure_event(session, event_payload)
                inserted_events += 1
                fight_urls = event_payload.get("fight_urls") or []
                for fight_index, fight_url in enumerate(fight_urls, 1):
                    try:
                        fight_payload = scraper.crawl_fight_card(fight_url)
                        fight_id = fight_payload.get("fight_id") or fight_url
                        if session.get(Fight, fight_id) is not None:
                            continue
                        a_name = fight_payload.get("fighter_a_name") or "Unknown Fighter A"
                        b_name = fight_payload.get("fighter_b_name") or "Unknown Fighter B"
                        logging.info("  [%s/%s] %s vs %s", fight_index, len(fight_urls), a_name, b_name)
                        a_id = fight_payload.get("fighter_a_id") or build_fighter_id(a_name)
                        b_id = fight_payload.get("fighter_b_id") or build_fighter_id(b_name)
                        fighter_a = ensure_fighter(session, a_id, a_name, fight_payload.get("fighter_a_url"))
                        fighter_b = ensure_fighter(session, b_id, b_name, fight_payload.get("fighter_b_url"))
                        a_stats = {**(fight_payload.get("fighter_a_stats") or {}), **_prior_stats(fighter_a, event.event_date)}
                        b_stats = {**(fight_payload.get("fighter_b_stats") or {}), **_prior_stats(fighter_b, event.event_date)}
                        winner = fight_payload.get("winner_name")
                        prior_h2h = session.scalar(select(__import__("sqlalchemy").func.count(Fight.fight_id)).where(
                            ((Fight.fighter_a_id == a_id) & (Fight.fighter_b_id == b_id)) | ((Fight.fighter_a_id == b_id) & (Fight.fighter_b_id == a_id)))) or 0
                        record = build_fight_record(event, fight_id, a_name, b_name, winner, fight_payload.get("method_of_victory"),
                            fight_payload.get("ending_round"), fight_payload.get("ending_time"), a_stats, b_stats,
                            fight_payload.get("source_fight_url") or fight_url, prior_h2h, a_id, b_id,
                            fight_payload.get("method_detail"), fight_payload.get("result"), fight_payload.get("referee"), fight_payload.get("time_format"))
                        upsert_fight(session, record)
                        _upsert_rounds(session, fight_id, fight_payload.get("rounds") or [], a_id, b_id)
                        result = fight_payload.get("result")
                        if result in {"draw", "no_contest"}:
                            outcome_a = outcome_b = result
                        else:
                            outcome_a = "win" if winner == a_name else "loss"
                            outcome_b = "win" if winner == b_name else "loss"
                        _update_aggregate(fighter_a, fight_payload.get("fighter_a_stats") or {}, outcome_a, event.event_date)
                        _update_aggregate(fighter_b, fight_payload.get("fighter_b_stats") or {}, outcome_b, event.event_date)
                        inserted_fights += 1
                    except Exception as exc:
                        message = f"Fight page failed: {fight_url}: {exc}"
                        logging.warning("  %s", message)
                        logging.debug("Fight failure details", exc_info=True)
                        errors.append(message)
                session.commit()
            except Exception as exc:
                message = f"Event page failed: {event_payload.get('event_url')}: {exc}"
                logging.warning("%s", message)
                logging.debug("Event failure details", exc_info=True)
                errors.append(message)
                session.rollback()
    finally:
        session.close()
        close = getattr(scraper, "close", None)
        if callable(close):
            close()

    logging.info("Pipeline summary: events=%s fights=%s errors=%s", inserted_events, inserted_fights, len(errors))
    return inserted_events, inserted_fights, errors
