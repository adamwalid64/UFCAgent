from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from . import db as database
from .derived import rebuild_derived_state
from .models import Event, Fight, FightRound, Fighter


SIGNIFICANT_STRIKE_SPLITS = ("head", "body", "leg", "distance", "clinch", "ground")
DEFAULT_PROFILE_MAX_AGE_DAYS = 90

# These values describe the fighter as they were recorded for this particular
# bout.  A normal UFCStats fight refresh does not include them, so replacing a
# previously hydrated value with ``None`` would silently destroy a historical
# snapshot that cannot necessarily be reconstructed from the fighter's current
# profile.
FIGHT_PHYSICAL_SNAPSHOT_FIELDS = frozenset(
    f"fighter_{side}_{field}"
    for side in ("a", "b")
    for field in ("age", "height_inches", "reach_inches", "stance")
)

ROUND_STAT_FIELDS = (
    "knockdowns",
    "significant_strikes_landed",
    "significant_strikes_attempted",
    "total_strikes_landed",
    "total_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "submission_attempts",
    "reversals",
    "control_time_seconds",
)


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
        "source_fight_count": len(event_payload.get("fight_urls") or []),
        "last_scraped_at": datetime.now(timezone.utc),
    }


def _split_columns(side: str, stats: dict[str, Any]) -> dict[str, int | None]:
    splits = stats.get("significant_strike_splits") or {}
    values: dict[str, int | None] = {}
    for position in SIGNIFICANT_STRIKE_SPLITS:
        payload = splits.get(position) or {}
        prefix = f"fighter_{side}_significant_{position}_strikes"
        values[f"{prefix}_landed"] = payload.get("landed")
        values[f"{prefix}_attempted"] = payload.get("attempted")
    return values


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
    weight_class: str | None = None,
    card_order: int | None = None,
    event_method_code: str | None = None,
    stats_available: bool = False,
) -> dict[str, Any]:
    fighter_a_stats = fighter_a_stats or {}
    fighter_b_stats = fighter_b_stats or {}

    fighter_a_id = fighter_a_id or build_fighter_id(fighter_a_name)
    fighter_b_id = fighter_b_id or build_fighter_id(fighter_b_name)
    if winner_name is None:
        winner_fighter_id = None
    elif winner_name == fighter_a_name:
        winner_fighter_id = fighter_a_id
    elif winner_name == fighter_b_name:
        winner_fighter_id = fighter_b_id
    else:
        raise ValueError(
            f"Winner {winner_name!r} does not match either participant in fight {fight_id!r}."
        )

    normalized_result = result or ("completed" if winner_name else None)
    record = {
        "fight_id": fight_id,
        "event_id": event.event_id,
        "fighter_a_id": fighter_a_id,
        "fighter_a_name": fighter_a_name,
        "weight_class": weight_class,
        "card_order": card_order,
        "event_method_code": event_method_code,
        "stats_available": stats_available,
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
        "result": normalized_result,
        "referee": referee,
        "time_format": time_format,
        "ending_round": ending_round,
        "ending_time": ending_time,
        "head_to_head_fight_count": head_to_head_fight_count,
        "source_event_url": event.event_url,
        "source_fight_url": source_fight_url,
        "last_scraped_at": datetime.now(timezone.utc),
    }
    record.update(_split_columns("a", fighter_a_stats))
    record.update(_split_columns("b", fighter_b_stats))
    return record


def ensure_event(session: Session, event_payload: dict[str, Any]) -> Event:
    event_record = build_event_record(event_payload)
    event = session.get(Event, event_record["event_id"])
    if event is None and event_record.get("event_url"):
        event = session.scalar(select(Event).where(Event.event_url == event_record["event_url"]))
    if event is None:
        event = Event(**event_record)
        session.add(event)
    else:
        for key in (
            "event_name",
            "event_date",
            "event_location",
            "event_url",
            "source_fight_count",
            "last_scraped_at",
        ):
            setattr(event, key, event_record[key])
    session.flush()
    return event


def upsert_fight(session: Session, fight_payload: dict[str, Any]) -> Fight:
    fight = session.get(Fight, fight_payload["fight_id"])
    if fight is None:
        fight = Fight(**fight_payload)
        session.add(fight)
    else:
        for key, value in fight_payload.items():
            if (
                key in FIGHT_PHYSICAL_SNAPSHOT_FIELDS
                and value is None
                and getattr(fight, key) is not None
            ):
                continue
            setattr(fight, key, value)
    session.flush()
    return fight


def _source_id_from_url(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty, whitespace-free string URL")
    source_id = value.rstrip("/").rsplit("/", 1)[-1]
    if not source_id:
        raise ValueError(f"{label} has no source identity: {value!r}")
    return source_id


def _validate_profile_identity(
    fighter_id: str,
    requested_profile_url: str | None,
    profile: dict[str, Any],
) -> None:
    """Reject redirected/mis-associated profiles before mutating fighter data."""

    if not isinstance(profile, dict):
        raise ValueError(f"Fighter {fighter_id!r} returned a non-object profile payload")
    if not requested_profile_url:
        raise ValueError(f"Fighter {fighter_id!r} has no requested profile URL")
    requested_id = _source_id_from_url(requested_profile_url, label="requested profile URL")
    payload_id = profile.get("fighter_id")
    payload_url = profile.get("profile_url")
    if not isinstance(payload_id, str) or not payload_id:
        raise ValueError(f"Fighter {fighter_id!r} profile payload has no fighter_id")
    payload_url_id = _source_id_from_url(payload_url, label="profile payload URL")
    identities = {fighter_id, requested_id, payload_id, payload_url_id}
    if len(identities) != 1:
        raise ValueError(
            "Fighter profile identity mismatch: "
            f"database={fighter_id!r}, requested_url={requested_id!r}, "
            f"payload={payload_id!r}, payload_url={payload_url_id!r}"
        )


def ensure_fighter(
    session: Session,
    fighter_id: str,
    name: str,
    profile_url: str | None = None,
    profile: dict[str, Any] | None = None,
) -> Fighter:
    if profile is not None:
        _validate_profile_identity(fighter_id, profile_url, profile)
    fighter = session.get(Fighter, fighter_id)
    if fighter is None:
        fighter = Fighter(fighter_id=fighter_id, fighter_name=name, profile_url=profile_url)
        session.add(fighter)
    else:
        fighter.fighter_name = name
        fighter.profile_url = profile_url or fighter.profile_url
    if profile is not None:
        fighter.fighter_name = profile.get("fighter_name") or fighter.fighter_name
        for key in ("fighter_nickname", "date_of_birth", "height_inches", "reach_inches", "stance"):
            value = profile.get(key)
            if value in {None, ""} and getattr(fighter, key) not in {None, ""}:
                continue
            setattr(fighter, key, value)
        fighter.profile_url = profile.get("profile_url") or fighter.profile_url
        fighter.profile_scraped_at = datetime.now(timezone.utc)
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


def _round_payload_is_complete(
    rounds: list[dict[str, Any]], ending_round: Any
) -> bool:
    """Return whether a payload can authoritatively replace the round-key set.

    A fetch that yielded no tables, omitted one fighter, or stopped before the
    recorded ending round may still be useful for updating the natural keys it
    did return.  It must not delete previously stored keys, though.
    """

    if (
        not rounds
        or isinstance(ending_round, bool)
        or not isinstance(ending_round, int)
        or ending_round < 1
    ):
        return False
    numbers: list[int] = []
    for payload in rounds:
        number = payload.get("round_number")
        if isinstance(number, bool) or not isinstance(number, int):
            return False
        if not isinstance(payload.get("fighter_a"), dict) or not isinstance(
            payload.get("fighter_b"), dict
        ):
            return False
        numbers.append(number)
    return len(numbers) == len(set(numbers)) and set(numbers) == set(
        range(1, ending_round + 1)
    )


def _round_records(
    fight_id: str,
    rounds: list[dict[str, Any]],
    a_id: str,
    b_id: str,
) -> dict[tuple[int, str], dict[str, Any]]:
    """Materialize and validate all natural-key records before any mutation."""

    records: dict[tuple[int, str], dict[str, Any]] = {}
    for round_payload in rounds:
        number = round_payload.get("round_number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError(
                f"Fight {fight_id!r} has invalid round number {number!r}."
            )
        for side, fighter_id, opponent_id in (("fighter_a", a_id, b_id), ("fighter_b", b_id, a_id)):
            values = round_payload.get(side)
            if not isinstance(values, dict):
                raise ValueError(
                    f"Fight {fight_id!r} round {number} is missing {side} stats."
                )
            record = {key: values.get(key) for key in ROUND_STAT_FIELDS}
            splits = values.get("significant_strike_splits") or {}
            for position in SIGNIFICANT_STRIKE_SPLITS:
                split = splits.get(position) or {}
                record[f"significant_{position}_strikes_landed"] = split.get("landed")
                record[f"significant_{position}_strikes_attempted"] = split.get("attempted")
            record.update({"fight_id": fight_id, "round_number": number, "fighter_id": fighter_id, "opponent_id": opponent_id})
            natural_key = (number, fighter_id)
            if natural_key in records:
                raise ValueError(
                    f"Fight {fight_id!r} contains duplicate round key {natural_key!r}."
                )
            records[natural_key] = record
    return records


def _upsert_rounds(
    session: Session,
    fight_id: str,
    rounds: list[dict[str, Any]],
    a_id: str,
    b_id: str,
    *,
    complete_payload: bool,
) -> None:
    """Upsert by source identity and prune only after an authoritative scrape."""

    incoming = _round_records(fight_id, rounds, a_id, b_id)
    existing_rows = list(
        session.scalars(select(FightRound).where(FightRound.fight_id == fight_id))
    )
    existing = {
        (row.round_number, row.fighter_id): row
        for row in existing_rows
    }

    for natural_key, record in incoming.items():
        row = existing.get(natural_key)
        if row is None:
            session.add(FightRound(**record))
            continue
        for key, value in record.items():
            if key not in {"fight_id", "round_number", "fighter_id"}:
                setattr(row, key, value)

    if complete_payload:
        for natural_key in existing.keys() - incoming.keys():
            session.delete(existing[natural_key])
    session.flush()


def _validated_event_fight_manifest(event_payload: dict[str, Any]) -> dict[str, str]:
    """Return the exact source URL -> ID set after detecting truncation/aliasing."""

    raw_urls = event_payload.get("fight_urls")
    if not isinstance(raw_urls, list) or not raw_urls:
        raise ValueError("event page returned no fight URLs")

    manifest: dict[str, str] = {}
    source_ids: set[str] = set()
    for value in raw_urls:
        source_id = _source_id_from_url(value, label="event fight URL")
        if value in manifest:
            raise ValueError(f"event page repeats fight URL {value!r}")
        if source_id in source_ids:
            raise ValueError(f"event page aliases source fight ID {source_id!r} across URLs")
        manifest[value] = source_id
        source_ids.add(source_id)

    # Keep compatibility with custom/older scraper implementations that only
    # supplied fight_urls. The first-party scraper always supplies summaries,
    # so if that key is present it must be an exact one-to-one identity set.
    if "fight_summaries" not in event_payload:
        return manifest
    summaries = event_payload.get("fight_summaries")
    if not isinstance(summaries, list):
        raise ValueError("event fight_summaries must be a list")

    summary_urls: set[str] = set()
    summary_ids: set[str] = set()
    for summary in summaries:
        if not isinstance(summary, dict):
            raise ValueError("event fight_summaries contains a non-object row")
        summary_url = summary.get("fight_url")
        summary_id = summary.get("fight_id")
        if not isinstance(summary_url, str) or summary_url not in manifest:
            raise ValueError(f"event summary has unexpected fight URL {summary_url!r}")
        if not isinstance(summary_id, str) or summary_id != manifest[summary_url]:
            raise ValueError(
                f"event summary identity mismatch for {summary_url!r}: "
                f"expected {manifest[summary_url]!r}, got {summary_id!r}"
            )
        if summary_url in summary_urls or summary_id in summary_ids:
            raise ValueError(
                f"event fight_summaries repeats URL/identity {summary_url!r}/{summary_id!r}"
            )
        summary_urls.add(summary_url)
        summary_ids.add(summary_id)

    if summary_urls != set(manifest) or summary_ids != set(manifest.values()):
        raise ValueError(
            "event fight_summaries does not exactly match fight_urls "
            f"(missing_urls={sorted(set(manifest) - summary_urls)!r}, "
            f"extra_urls={sorted(summary_urls - set(manifest))!r})"
        )
    return manifest


def _validate_stats_refresh_authority(
    fight_id: str,
    fight_payload: dict[str, Any],
    rounds: list[dict[str, Any]],
    *,
    existing_has_complete_stats: bool,
) -> bool:
    """Validate parse authority and return whether round keys may be replaced."""

    status = fight_payload.get("stats_parse_status")
    structurally_complete = _round_payload_is_complete(
        rounds, fight_payload.get("ending_round")
    )
    if status is None:
        # Compatibility for scraper implementations predating explicit parse
        # authority. Never infer completeness unless every expected round is
        # present for both fighters.
        status = "complete" if structurally_complete else ("unavailable" if not rounds else "partial")
    if status not in {"complete", "unavailable", "partial"}:
        raise ValueError(f"Fight {fight_id!r} has invalid stats_parse_status {status!r}")

    detail = fight_payload.get("stats_parse_detail") or "no parser detail supplied"
    if status == "complete" and not structurally_complete:
        raise ValueError(
            f"Fight {fight_id!r} claims complete stats but round keys are incomplete: {detail}"
        )
    if status == "unavailable" and rounds:
        raise ValueError(
            f"Fight {fight_id!r} reports unavailable stats but supplied round rows"
        )
    if status == "partial":
        raise ValueError(f"Fight {fight_id!r} returned partial stats: {detail}")
    if existing_has_complete_stats and status != "complete":
        raise ValueError(
            f"Fight {fight_id!r} refused destructive complete->{status} stats downgrade: {detail}"
        )
    return status == "complete"


def _event_fight_summaries(event_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for summary in event_payload.get("fight_summaries") or []:
        if summary.get("fight_id"):
            summaries[str(summary["fight_id"])] = summary
        if summary.get("fight_url"):
            summaries[str(summary["fight_url"])] = summary
    return summaries


def _merge_fight_summary(
    fight_payload: dict[str, Any],
    fight_url: str,
    summaries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(fight_payload)
    summary = summaries.get(str(fight_payload.get("fight_id"))) or summaries.get(fight_url) or {}
    for key in ("weight_class", "method_of_victory", "event_method_code", "card_order"):
        if merged.get(key) in {None, ""} and summary.get(key) not in {None, ""}:
            merged[key] = summary[key]
    return merged


def _profile_refresh_due(
    fighter: Fighter,
    *,
    max_age_days: int = DEFAULT_PROFILE_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> bool:
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or max_age_days < 0:
        raise ValueError("profile_max_age_days must be a non-negative integer")
    if fighter.profile_scraped_at is None:
        return True
    observed_at = fighter.profile_scraped_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return observed_at <= reference - timedelta(days=max_age_days)


def _scrape_profile_if_needed(
    session: Session,
    scraper,
    fighter: Fighter,
    *,
    max_age_days: int = DEFAULT_PROFILE_MAX_AGE_DAYS,
) -> None:
    crawl_profile = getattr(scraper, "crawl_fighter_profile", None)
    if (
        not callable(crawl_profile)
        or not fighter.profile_url
        or not _profile_refresh_due(fighter, max_age_days=max_age_days)
    ):
        return
    profile = crawl_profile(fighter.profile_url)
    ensure_fighter(
        session,
        fighter.fighter_id,
        fighter.fighter_name,
        fighter.profile_url,
        profile=profile,
    )


def backfill_missing_profiles(
    session: Session,
    scraper,
    errors: list[str],
    limit: int | None = None,
) -> int:
    """Resume-safe profile enrichment for databases created by older versions."""
    crawl_profile = getattr(scraper, "crawl_fighter_profile", None)
    if not callable(crawl_profile):
        return 0
    statement = (
        select(Fighter)
        .where(Fighter.profile_scraped_at.is_(None), Fighter.profile_url.is_not(None))
        .order_by(Fighter.fighter_id)
    )
    fighters = list(session.scalars(statement))
    if limit is not None:
        fighters = fighters[:limit]
    updated = 0
    for index, fighter in enumerate(fighters, 1):
        try:
            logging.info("Profile backfill [%s/%s] %s", index, len(fighters), fighter.fighter_name)
            profile = crawl_profile(fighter.profile_url)
            ensure_fighter(
                session,
                fighter.fighter_id,
                fighter.fighter_name,
                fighter.profile_url,
                profile=profile,
            )
            session.commit()
            updated += 1
        except Exception as exc:
            session.rollback()
            message = f"Fighter profile failed: {fighter.profile_url}: {exc}"
            logging.warning(message)
            logging.debug("Fighter profile failure details", exc_info=True)
            errors.append(message)
    return updated


def backfill_missing_event_metadata(
    session: Session,
    scraper,
    errors: list[str],
    limit: int | None = None,
) -> int:
    """Fill method, division, and card order without re-fetching fight pages."""
    statement = (
        select(Event)
        .join(Fight, Fight.event_id == Event.event_id)
        .where(
            Event.event_url.is_not(None),
            or_(
                Fight.method_of_victory.is_(None),
                Fight.weight_class.is_(None),
                Fight.card_order.is_(None),
            ),
        )
        .distinct()
        .order_by(Event.event_date.desc())
    )
    events = list(session.scalars(statement))
    if limit is not None:
        events = events[:limit]
    updated = 0
    for index, event in enumerate(events, 1):
        try:
            logging.info("Event metadata backfill [%s/%s] %s", index, len(events), event.event_name)
            payload = scraper.crawl_event_details(event.event_url)
            manifest = _validated_event_fight_manifest(payload)
            summaries_by_source = _event_fight_summaries(payload)
            summaries = {
                fight_id: summaries_by_source[fight_id]
                for fight_id in manifest.values()
            }
            stored = list(session.scalars(select(Fight).where(Fight.event_id == event.event_id)))
            stored_ids = {fight.fight_id for fight in stored}
            source_ids = set(manifest.values())
            if stored_ids != source_ids:
                raise ValueError(
                    f"source/stored fight IDs differ (missing={sorted(source_ids - stored_ids)!r}, "
                    f"extra={sorted(stored_ids - source_ids)!r}); run a full event refresh"
                )
            event.source_fight_count = len(source_ids)
            event.last_scraped_at = datetime.now(timezone.utc)
            for fight in stored:
                summary = summaries[fight.fight_id]
                fight.weight_class = summary.get("weight_class")
                fight.card_order = summary.get("card_order")
                fight.event_method_code = summary.get("event_method_code")
                if summary.get("method_of_victory"):
                    fight.method_of_victory = summary["method_of_victory"]
                fight.source_event_url = event.event_url
            session.commit()
            updated += 1
        except Exception as exc:
            session.rollback()
            message = f"Event metadata failed: {event.event_url}: {exc}"
            logging.warning(message)
            logging.debug("Event metadata failure details", exc_info=True)
            errors.append(message)
    return updated


def run_pipeline(
    scraper,
    full: bool = False,
    max_events: int | None = None,
    refresh_events: int = 8,
    scrape_profiles: bool = True,
    backfill_profiles: bool = False,
    backfill_metadata: bool = False,
    profile_limit: int | None = None,
    metadata_limit: int | None = None,
    allow_source_deletions: bool = False,
    profile_max_age_days: int = DEFAULT_PROFILE_MAX_AGE_DAYS,
) -> tuple[int, int, list[str]]:
    """Reconcile UFCStats into the database and rebuild all derived history.

    Incremental mode always ingests missing events and refreshes a recent
    window, so an interrupted card or later UFCStats correction is retried.
    Existing fights are updated rather than skipped. Each event is atomic.
    """

    if refresh_events < 0:
        raise ValueError("refresh_events cannot be negative")
    if (
        isinstance(profile_max_age_days, bool)
        or not isinstance(profile_max_age_days, int)
        or profile_max_age_days < 0
    ):
        raise ValueError("profile_max_age_days must be a non-negative integer")

    session = database.SessionLocal()
    processed_events = 0
    processed_fights = 0
    errors: list[str] = []
    profile_attempted_ids: set[str] = set()

    try:
        # Older database versions may contain derived-field defects. Repair
        # those first so SQLite foreign-key enforcement cannot block a normal
        # weekly refresh of otherwise valid rows.
        try:
            rebuild_derived_state(session)
            session.commit()
        except Exception as exc:
            session.rollback()
            message = f"Pre-sync derived-state validation failed: {exc}"
            logging.exception(message)
            errors.append(message)
            return processed_events, processed_fights, errors

        event_summary = scraper.crawl_event_listing(max_events=max_events)
        errors.extend(
            f"Listing failed: {error.source_url}: {error.message}"
            for error in getattr(event_summary, "errors", [])
        )
        listing_urls = list(dict.fromkeys(getattr(event_summary, "event_urls", []) or []))
        if not listing_urls:
            errors.append("Completed-event listing returned no event URLs; database was not reconciled.")
        else:
            existing_urls = set(
                session.scalars(select(Event.event_url).where(Event.event_url.is_not(None)))
            )
            if full:
                target_urls = listing_urls
            else:
                missing_urls = [url for url in listing_urls if url not in existing_urls]
                target_urls = list(dict.fromkeys(missing_urls + listing_urls[:refresh_events]))

            pending: list[dict[str, Any]] = []
            for event_url in target_urls:
                try:
                    pending.append(scraper.crawl_event_details(event_url))
                except Exception as exc:
                    message = f"Event page failed: {event_url}: {exc}"
                    logging.warning(message)
                    logging.debug("Event page failure details", exc_info=True)
                    errors.append(message)

            pending.sort(
                key=lambda payload: (
                    parse_event_date(payload.get("event_date")) or date.min,
                    str(payload.get("event_url") or ""),
                )
            )
            logging.info("%s event(s) selected for full fight reconciliation", len(pending))

            for event_index, event_payload in enumerate(pending, 1):
                event_url = str(event_payload.get("event_url") or "")
                try:
                    event_fights_processed = 0
                    logging.info(
                        "[%s/%s] %s",
                        event_index,
                        len(pending),
                        event_payload.get("event_name") or event_url,
                    )
                    fight_manifest = _validated_event_fight_manifest(event_payload)
                    fight_urls = list(fight_manifest)
                    event = ensure_event(session, event_payload)
                    summaries = _event_fight_summaries(event_payload)
                    seen_fight_ids: set[str] = set()
                    event_profile_ids: set[str] = set()

                    # UFCStats cards are displayed main-event first. Reversing
                    # gives true within-card chronology for tournament events.
                    for fight_index, fight_url in enumerate(reversed(fight_urls), 1):
                        source_fight_payload = scraper.crawl_fight_card(fight_url)
                        if not isinstance(source_fight_payload, dict):
                            raise ValueError(f"Fight {fight_url!r} returned a non-object payload")
                        fight_payload = _merge_fight_summary(
                            source_fight_payload, fight_url, summaries
                        )
                        fight_id = fight_payload.get("fight_id")
                        source_fight_url = fight_payload.get("source_fight_url")
                        if fight_id != fight_manifest[fight_url]:
                            raise ValueError(
                                f"Fight page identity mismatch for {fight_url!r}: "
                                f"expected {fight_manifest[fight_url]!r}, got {fight_id!r}"
                            )
                        if source_fight_url != fight_url:
                            raise ValueError(
                                f"Fight payload URL mismatch: requested {fight_url!r}, "
                                f"got {source_fight_url!r}"
                            )
                        if fight_id in seen_fight_ids:
                            raise ValueError(f"event returned duplicate fight identity {fight_id!r}")
                        a_name = fight_payload.get("fighter_a_name") or "Unknown Fighter A"
                        b_name = fight_payload.get("fighter_b_name") or "Unknown Fighter B"
                        logging.info(
                            "  [%s/%s] %s vs %s",
                            fight_index,
                            len(fight_urls),
                            a_name,
                            b_name,
                        )
                        a_id = fight_payload.get("fighter_a_id") or build_fighter_id(a_name)
                        b_id = fight_payload.get("fighter_b_id") or build_fighter_id(b_name)
                        fighter_a = ensure_fighter(
                            session, a_id, a_name, fight_payload.get("fighter_a_url")
                        )
                        fighter_b = ensure_fighter(
                            session, b_id, b_name, fight_payload.get("fighter_b_url")
                        )
                        if scrape_profiles:
                            event_profile_ids.update((fighter_a.fighter_id, fighter_b.fighter_id))

                        raw_rounds = fight_payload.get("rounds")
                        if raw_rounds is None:
                            rounds: list[dict[str, Any]] = []
                        elif isinstance(raw_rounds, list):
                            rounds = raw_rounds
                        else:
                            raise ValueError(f"Fight {fight_id!r} rounds payload must be a list")
                        existing_fight = session.get(Fight, fight_id)
                        if existing_fight is not None:
                            stored_participants = (
                                existing_fight.fighter_a_id,
                                existing_fight.fighter_b_id,
                            )
                            incoming_participants = (a_id, b_id)
                            if stored_participants != incoming_participants:
                                raise ValueError(
                                    f"Fight {fight_id!r} participant identity/side mismatch: "
                                    f"stored={stored_participants!r}, incoming={incoming_participants!r}"
                                )
                            if (
                                existing_fight.source_fight_url
                                and existing_fight.source_fight_url != source_fight_url
                            ):
                                raise ValueError(
                                    f"Fight {fight_id!r} source URL mismatch: "
                                    f"stored={existing_fight.source_fight_url!r}, "
                                    f"incoming={source_fight_url!r}"
                                )
                        existing_round_id = session.scalar(
                            select(FightRound.round_stat_id)
                            .where(FightRound.fight_id == fight_id)
                            .limit(1)
                        )
                        complete_stats_payload = _validate_stats_refresh_authority(
                            fight_id,
                            fight_payload,
                            rounds,
                            existing_has_complete_stats=bool(
                                (existing_fight is not None and existing_fight.stats_available)
                                or existing_round_id is not None
                            ),
                        )
                        record = build_fight_record(
                            event=event,
                            fight_id=fight_id,
                            fighter_a_name=a_name,
                            fighter_b_name=b_name,
                            winner_name=fight_payload.get("winner_name"),
                            method_of_victory=fight_payload.get("method_of_victory"),
                            ending_round=fight_payload.get("ending_round"),
                            ending_time=fight_payload.get("ending_time"),
                            fighter_a_stats=fight_payload.get("fighter_a_stats") or {},
                            fighter_b_stats=fight_payload.get("fighter_b_stats") or {},
                            source_fight_url=source_fight_url,
                            fighter_a_id=a_id,
                            fighter_b_id=b_id,
                            method_detail=fight_payload.get("method_detail"),
                            result=fight_payload.get("result"),
                            referee=fight_payload.get("referee"),
                            time_format=fight_payload.get("time_format"),
                            weight_class=fight_payload.get("weight_class"),
                            card_order=fight_payload.get("card_order"),
                            event_method_code=fight_payload.get("event_method_code"),
                            stats_available=complete_stats_payload,
                        )
                        upsert_fight(session, record)
                        _upsert_rounds(
                            session,
                            fight_id,
                            rounds,
                            a_id,
                            b_id,
                            complete_payload=complete_stats_payload,
                        )
                        seen_fight_ids.add(fight_id)
                        event_fights_processed += 1

                    expected_fight_ids = set(fight_manifest.values())
                    if seen_fight_ids != expected_fight_ids:
                        raise ValueError(
                            "event crawl identity set differs from its validated manifest "
                            f"(missing={sorted(expected_fight_ids - seen_fight_ids)!r}, "
                            f"extra={sorted(seen_fight_ids - expected_fight_ids)!r})"
                        )
                    stale_ids = set(
                        session.scalars(select(Fight.fight_id).where(Fight.event_id == event.event_id))
                    ) - expected_fight_ids
                    if stale_ids:
                        if not allow_source_deletions:
                            raise ValueError(
                                "source manifest would delete stored fight(s) "
                                f"{sorted(stale_ids)!r}; refusing by default. "
                                "Re-run with allow_source_deletions=True only after reviewing the correction."
                            )
                        session.execute(delete(FightRound).where(FightRound.fight_id.in_(stale_ids)))
                        session.execute(delete(Fight).where(Fight.fight_id.in_(stale_ids)))
                    session.commit()
                    processed_events += 1
                    processed_fights += event_fights_processed

                    # Profile enrichment is deliberately outside the canonical
                    # event transaction. A profile timeout or identity error is
                    # auditable but cannot roll back valid event/fight rows.
                    if scrape_profiles:
                        for fighter_id in sorted(event_profile_ids - profile_attempted_ids):
                            profile_attempted_ids.add(fighter_id)
                            fighter = session.get(Fighter, fighter_id)
                            if fighter is None:
                                continue
                            profile_url = fighter.profile_url
                            try:
                                _scrape_profile_if_needed(
                                    session,
                                    scraper,
                                    fighter,
                                    max_age_days=profile_max_age_days,
                                )
                                session.commit()
                            except Exception as exc:
                                session.rollback()
                                message = f"Fighter profile failed: {profile_url}: {exc}"
                                logging.warning(message)
                                logging.debug("Fighter profile failure details", exc_info=True)
                                errors.append(message)
                except Exception as exc:
                    session.rollback()
                    message = f"Event reconciliation failed: {event_url}: {exc}"
                    logging.warning(message)
                    logging.debug("Event reconciliation failure details", exc_info=True)
                    errors.append(message)

        if backfill_metadata and not full:
            backfill_missing_event_metadata(
                session, scraper, errors, limit=metadata_limit
            )
        if backfill_profiles:
            backfill_missing_profiles(session, scraper, errors, limit=profile_limit)

        try:
            report = rebuild_derived_state(session)
            session.commit()
            logging.info(
                "Derived-state rebuild: fights=%s fighters=%s missing_stats=%s",
                report.fights_processed,
                report.fighters_rebuilt,
                report.fights_without_round_stats,
            )
        except Exception as exc:
            session.rollback()
            logging.exception("Derived-state rebuild failed")
            errors.append(f"Derived-state rebuild failed: {exc}")
    finally:
        session.close()
        close = getattr(scraper, "close", None)
        if callable(close):
            close()

    logging.info(
        "Pipeline summary: events=%s fights=%s errors=%s",
        processed_events,
        processed_fights,
        len(errors),
    )
    return processed_events, processed_fights, errors
