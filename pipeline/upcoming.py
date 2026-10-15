"""Atomic replacement of the next-card schedule; completed history is untouched."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Event, UpcomingEvent, UpcomingFight


def save_upcoming_card(session: Session, card: dict | None, *, today: date | None = None) -> int:
    """Validate the complete response before replacing any cached schedule rows.

    The caller commits/rolls back. A confirmed empty upcoming listing clears the
    old schedule; a failed fetch must never be passed here as None.
    """
    today = today or date.today()
    if card is not None:
        event_id = card["event_id"]
        if not re.fullmatch(r"[a-f0-9]{16}", event_id) or not card["event_url"].endswith("/" + event_id):
            raise ValueError("Upcoming event source identity mismatch")
        day = date.fromisoformat(card["event_date"])
        if day < today or not card["event_name"].strip():
            raise ValueError("Upcoming event needs a non-past date and name")
        if session.scalar(select(Event.event_id).where(Event.event_url == card["event_url"])):
            raise ValueError("Upcoming event already exists in completed history")
        fights = card["fights"]
        if not fights:
            raise ValueError("Upcoming card must have published pairings")
        seen_ids, seen_fighters = set(), set()
        for position, fight in enumerate(fights, 1):
            fid = fight["fight_id"]
            if not re.fullmatch(r"[a-f0-9]{16}", fid) or fid in seen_ids:
                raise ValueError("Invalid or duplicate upcoming fight identity")
            seen_ids.add(fid)
            if fight["card_order"] != position or not fight["source_fight_url"].endswith("/" + fid):
                raise ValueError("Upcoming fight order or source identity mismatch")
            if fight.get("scheduled_rounds") not in (None, 3, 5):
                raise ValueError("Scheduled distance must be 3, 5, or unknown")
            for side in ("a", "b"):
                fighter_id = fight[f"fighter_{side}_id"]
                if not re.fullmatch(r"[a-f0-9]{16}", fighter_id) or fighter_id in seen_fighters:
                    raise ValueError("Invalid or repeated fighter in upcoming card")
                if not fight[f"fighter_{side}_name"].strip():
                    raise ValueError("Upcoming fighter name is missing")
                seen_fighters.add(fighter_id)

    session.execute(delete(UpcomingFight))
    session.execute(delete(UpcomingEvent))
    if card is None:
        return 0
    session.add(UpcomingEvent(
        event_id=card["event_id"], event_name=card["event_name"], event_date=day,
        event_location=card.get("event_location"), event_url=card["event_url"],
        source_fight_count=len(fights), last_scraped_at=datetime.now(timezone.utc),
    ))
    session.flush()
    for fight in fights:
        session.add(UpcomingFight(event_id=card["event_id"], **fight))
    session.flush()
    return len(fights)
