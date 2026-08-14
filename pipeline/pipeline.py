from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Event, Fight


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
) -> dict[str, Any]:
    fighter_a_stats = fighter_a_stats or {}
    fighter_b_stats = fighter_b_stats or {}

    fighter_a_id = build_fighter_id(fighter_a_name)
    fighter_b_id = build_fighter_id(fighter_b_name)
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
        "fighter_b_days_since_last_fight": fighter_b_stats.get("days_since_last_fight"),
        "winner_fighter_id": winner_fighter_id,
        "winner_name": winner_name,
        "method_of_victory": method_of_victory,
        "method_detail": None,
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


def run_pipeline(scraper, full: bool = False, max_events: int | None = None) -> tuple[int, int, list[str]]:
    session = SessionLocal()
    inserted_events = 0
    inserted_fights = 0
    errors: list[str] = []

    try:
        existing_event_ids = set()
        if not full:
            existing_event_ids = {row[0] for row in session.execute(__import__("sqlalchemy").select(Event.event_id)).all()}

        event_summary = scraper.crawl_event_listing(max_events=max_events if not full else None)
        processed_event_count = 0

        for event_url in event_summary.event_urls:
            if max_events is not None and processed_event_count >= max_events and not full:
                break

            try:
                event_payload = scraper.crawl_event_details(event_url)
                event_record = build_event_record(event_payload)
                event_id = event_record["event_id"]

                if not full and event_id in existing_event_ids:
                    logging.info("Skipping already-known event in incremental mode: %s", event_id)
                    break

                event = ensure_event(session, event_payload)
                existing_event_ids.add(event.event_id)
                inserted_events += 1
                processed_event_count += 1

                fight_urls = event_payload.get("fight_urls") or []
                if not fight_urls:
                    session.commit()
                    continue

                for fight_url in fight_urls:
                    try:
                        fight_payload = scraper.crawl_fight_card(fight_url)
                        fight_id = fight_payload.get("fight_id") or fight_url
                        if session.get(Fight, fight_id) is not None:
                            continue

                        fight_record = build_fight_record(
                            event=event,
                            fight_id=fight_id,
                            fighter_a_name=fight_payload.get("fighter_a_name") or "Unknown Fighter A",
                            fighter_b_name=fight_payload.get("fighter_b_name") or "Unknown Fighter B",
                            winner_name=fight_payload.get("winner_name"),
                            method_of_victory=fight_payload.get("method_of_victory"),
                            ending_round=fight_payload.get("ending_round"),
                            ending_time=fight_payload.get("ending_time"),
                            source_fight_url=fight_payload.get("source_fight_url") or fight_url,
                        )
                        upsert_fight(session, fight_record)
                        inserted_fights += 1
                    except Exception as exc:  # pragma: no cover - failed individual fight page
                        errors.append(f"Fight page failed: {fight_url}: {exc}")
                session.commit()
            except Exception as exc:  # pragma: no cover - failed event page
                errors.append(f"Event page failed: {event_url}: {exc}")
                session.rollback()
    finally:
        session.close()

    logging.info("Pipeline summary: events=%s fights=%s errors=%s", inserted_events, inserted_fights, len(errors))
    return inserted_events, inserted_fights, errors
