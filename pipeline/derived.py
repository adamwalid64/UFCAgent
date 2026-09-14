from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from .models import Event, Fight, FightRound, Fighter


PREFIGHT_FIELDS = (
    "wins",
    "losses",
    "draws",
    "current_win_streak",
    "current_loss_streak",
    "days_since_last_fight",
)

FIGHT_STAT_FIELDS = (
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
    "significant_head_strikes_landed",
    "significant_head_strikes_attempted",
    "significant_body_strikes_landed",
    "significant_body_strikes_attempted",
    "significant_leg_strikes_landed",
    "significant_leg_strikes_attempted",
    "significant_distance_strikes_landed",
    "significant_distance_strikes_attempted",
    "significant_clinch_strikes_landed",
    "significant_clinch_strikes_attempted",
    "significant_ground_strikes_landed",
    "significant_ground_strikes_attempted",
)

FIGHTER_AGGREGATE_STAT_FIELDS = (
    "significant_strikes_landed",
    "significant_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "control_time_seconds",
)


class DerivedStateError(ValueError):
    """Raised when stored rows cannot be replayed without guessing."""


@dataclass(frozen=True)
class DerivedStateReport:
    fights_processed: int
    fighters_rebuilt: int
    fights_without_round_stats: int
    winner_ids_changed: int
    source_event_urls_changed: int
    stats_availability_changed: int
    card_orders_backfilled: int
    profile_fields_hydrated: int
    events_using_card_order: int
    events_using_fallback_order: int


@dataclass
class _FighterState:
    fights: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    no_contests: int = 0
    current_win_streak: int = 0
    current_loss_streak: int = 0
    last_fight_date: date | None = None
    stats: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in FIGHTER_AGGREGATE_STAT_FIELDS}
    )


@dataclass(frozen=True)
class _FightEntry:
    fight: Fight
    event: Event
    source_rowid: int | None


def _source_rows(session: Session) -> list[_FightEntry]:
    """Load fights plus a durable-order fallback available in SQLite.

    Newer schemas should persist ``Fight.card_order``. Existing databases do
    not have that column, but SQLite rowid retains the scraper's insertion
    order. The scraper visits a card from main event to opener, so descending
    rowid reconstructs chronological card order.
    """

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        statement = (
            select(
                Fight,
                Event,
                literal_column("fights.rowid").label("source_rowid"),
            )
            .join(Event, Event.event_id == Fight.event_id)
        )
        return [
            _FightEntry(fight=fight, event=event, source_rowid=int(source_rowid))
            for fight, event, source_rowid in session.execute(statement)
        ]

    statement = select(Fight, Event).join(Event, Event.event_id == Fight.event_id)
    return [
        _FightEntry(fight=fight, event=event, source_rowid=None)
        for fight, event in session.execute(statement)
    ]


def _integer_card_order(fight: Fight) -> int | None:
    value = getattr(fight, "card_order", None)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _has_repeated_fighter(entries: Iterable[_FightEntry]) -> bool:
    seen: set[str] = set()
    for entry in entries:
        for fighter_id in (entry.fight.fighter_a_id, entry.fight.fighter_b_id):
            if fighter_id in seen:
                return True
            seen.add(fighter_id)
    return False


def _created_at_key(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _fallback_source_order(entries: list[_FightEntry]) -> list[_FightEntry]:
    """Return UFCStats top-to-bottom card order from legacy provenance."""

    rowids = [entry.source_rowid for entry in entries]
    if all(value is not None for value in rowids) and len(set(rowids)) == len(entries):
        return sorted(entries, key=lambda entry: (int(entry.source_rowid), entry.fight.fight_id))

    created_values = [entry.fight.created_at for entry in entries]
    if all(value is not None for value in created_values) and len(set(created_values)) == len(entries):
        return sorted(
            entries,
            key=lambda entry: (
                _created_at_key(entry.fight.created_at),
                entry.fight.fight_id,
            ),
        )

    # If nobody appears twice, card order cannot affect derived fighter state.
    # Assigning stable positions still makes future rebuilds durable.
    if not _has_repeated_fighter(entries):
        return sorted(entries, key=lambda entry: entry.fight.fight_id)

    event_id = entries[0].event.event_id if entries else "unknown"
    raise DerivedStateError(
        f"Cannot safely order event {event_id!r}: card_order is missing or "
        "duplicated and no unique insertion-order fallback is available."
    )


def _order_one_event(
    entries: list[_FightEntry],
) -> tuple[list[_FightEntry], str, dict[str, int]]:
    card_orders = [_integer_card_order(entry.fight) for entry in entries]
    expected_orders = set(range(1, len(entries) + 1))
    nonnull_orders = [value for value in card_orders if value is not None]
    if len(nonnull_orders) != len(set(nonnull_orders)):
        event_id = entries[0].event.event_id if entries else "unknown"
        raise DerivedStateError(f"Event {event_id!r} has duplicate card_order values.")
    if any(value not in expected_orders for value in nonnull_orders):
        event_id = entries[0].event.event_id if entries else "unknown"
        raise DerivedStateError(
            f"Event {event_id!r} card_order values must be the integers 1..{len(entries)}."
        )

    if len(nonnull_orders) == len(entries):
        planned_orders = {
            entry.fight.fight_id: int(_integer_card_order(entry.fight)) for entry in entries
        }
        return (
            sorted(
                entries,
                key=lambda entry: (
                    -planned_orders[entry.fight.fight_id],
                    entry.fight.fight_id,
                ),
            ),
            "card_order",
            planned_orders,
        )

    source_order = _fallback_source_order(entries)
    fallback_orders = {
        entry.fight.fight_id: position for position, entry in enumerate(source_order, 1)
    }
    # Partially populated cards are safe to backfill only when their existing
    # positions agree with the legacy insertion provenance.
    for entry in entries:
        existing = _integer_card_order(entry.fight)
        if existing is not None and existing != fallback_orders[entry.fight.fight_id]:
            raise DerivedStateError(
                f"Event {entry.event.event_id!r} has a partial card_order that "
                "conflicts with insertion order."
            )
    return (
        sorted(
            entries,
            key=lambda entry: (-fallback_orders[entry.fight.fight_id], entry.fight.fight_id),
        ),
        "fallback",
        fallback_orders,
    )


def _ordered_fights(
    entries: list[_FightEntry],
) -> tuple[list[_FightEntry], Counter[str], dict[str, int]]:
    by_event: dict[str, list[_FightEntry]] = defaultdict(list)
    events: dict[str, Event] = {}
    for entry in entries:
        by_event[entry.event.event_id].append(entry)
        events[entry.event.event_id] = entry.event

    ordered: list[_FightEntry] = []
    ordering_counts: Counter[str] = Counter()
    planned_card_orders: dict[str, int] = {}
    for event_id in sorted(
        by_event,
        key=lambda value: (events[value].event_date, value),
    ):
        event_entries, ordering_basis, event_card_orders = _order_one_event(by_event[event_id])
        ordered.extend(event_entries)
        ordering_counts[ordering_basis] += 1
        planned_card_orders.update(event_card_orders)
    return ordered, ordering_counts, planned_card_orders


def _validate_same_day_events(entries: Iterable[_FightEntry]) -> None:
    """Reject chronology that needs unavailable event times to be guessed."""

    appearances: dict[tuple[date, str], str] = {}
    for entry in entries:
        event_date = entry.event.event_date
        if event_date is None:
            raise DerivedStateError(f"Event {entry.event.event_id!r} has no date.")
        for fighter_id in (entry.fight.fighter_a_id, entry.fight.fighter_b_id):
            key = (event_date, fighter_id)
            prior_event_id = appearances.setdefault(key, entry.event.event_id)
            if prior_event_id != entry.event.event_id:
                raise DerivedStateError(
                    f"Fighter {fighter_id!r} appears in events {prior_event_id!r} "
                    f"and {entry.event.event_id!r} on {event_date}; event time is "
                    "required for a leakage-safe replay."
                )


def _winner_id(fight: Fight) -> str | None:
    result = (fight.result or "").strip().lower()
    if result in {"draw", "no_contest"}:
        if fight.winner_name not in {None, ""}:
            raise DerivedStateError(
                f"Fight {fight.fight_id!r} is {result} but has winner "
                f"{fight.winner_name!r}."
            )
        return None
    if result != "completed":
        raise DerivedStateError(
            f"Fight {fight.fight_id!r} has unsupported result {fight.result!r}."
        )

    matches: list[str] = []
    if fight.winner_name == fight.fighter_a_name:
        matches.append(fight.fighter_a_id)
    if fight.winner_name == fight.fighter_b_name:
        matches.append(fight.fighter_b_id)
    if len(matches) != 1:
        raise DerivedStateError(
            f"Fight {fight.fight_id!r} winner {fight.winner_name!r} does not "
            "match exactly one side name."
        )
    return matches[0]


def _validate_fight(fight: Fight, fighter_ids: set[str]) -> str | None:
    if fight.fighter_a_id == fight.fighter_b_id:
        raise DerivedStateError(f"Fight {fight.fight_id!r} has the same fighter on both sides.")
    missing_ids = {fight.fighter_a_id, fight.fighter_b_id} - fighter_ids
    if missing_ids:
        raise DerivedStateError(
            f"Fight {fight.fight_id!r} references missing fighter IDs: "
            f"{sorted(missing_ids)!r}."
        )
    for side in ("a", "b"):
        for field_name in FIGHT_STAT_FIELDS:
            value = getattr(fight, f"fighter_{side}_{field_name}")
            if value is not None and value < 0:
                raise DerivedStateError(
                    f"Fight {fight.fight_id!r} has negative fighter_{side}_{field_name}."
                )
    return _winner_id(fight)


def _set_prefight_fields(fight: Fight, side: str, state: _FighterState, event_date: date) -> None:
    setattr(fight, f"fighter_{side}_wins", state.wins)
    setattr(fight, f"fighter_{side}_losses", state.losses)
    setattr(fight, f"fighter_{side}_draws", state.draws)
    setattr(fight, f"fighter_{side}_current_win_streak", state.current_win_streak)
    setattr(fight, f"fighter_{side}_current_loss_streak", state.current_loss_streak)
    if state.last_fight_date is None:
        days_since_last_fight = None
    else:
        days_since_last_fight = (event_date - state.last_fight_date).days
        if days_since_last_fight < 0:
            raise DerivedStateError(
                f"Negative rest interval while replaying fight {fight.fight_id!r}."
            )
    setattr(fight, f"fighter_{side}_days_since_last_fight", days_since_last_fight)


def _apply_outcome(state: _FighterState, outcome: str) -> None:
    state.fights += 1
    if outcome == "win":
        state.wins += 1
        state.current_win_streak += 1
        state.current_loss_streak = 0
    elif outcome == "loss":
        state.losses += 1
        state.current_loss_streak += 1
        state.current_win_streak = 0
    elif outcome == "draw":
        state.draws += 1
        state.current_win_streak = 0
        state.current_loss_streak = 0
    elif outcome == "no_contest":
        state.no_contests += 1
    else:  # pragma: no cover - guarded by _winner_id
        raise AssertionError(outcome)


def _add_fight_stats(fight: Fight, side: str, state: _FighterState) -> None:
    for field_name in FIGHTER_AGGREGATE_STAT_FIELDS:
        value = getattr(fight, f"fighter_{side}_{field_name}")
        if value is not None:
            state.stats[field_name] += int(value)


def _age_on_date(date_of_birth: date, event_date: date) -> int:
    age = event_date.year - date_of_birth.year
    if (event_date.month, event_date.day) < (date_of_birth.month, date_of_birth.day):
        age -= 1
    if age < 0:
        raise DerivedStateError(
            f"Fighter date of birth {date_of_birth} is after event date {event_date}."
        )
    return age


def _hydrate_fight_profile(
    fight: Fight,
    side: str,
    fighter: Fighter,
    event_date: date,
) -> int:
    """Fill absent bout fields from today's profile without replacing values.

    The caller marks the side as profile-imputed whenever this returns a
    positive count; reach, height, and stance copied retrospectively must not
    be presented as known historical measurements.
    """

    hydrated = 0
    if fight_value := fighter.date_of_birth:
        attribute = f"fighter_{side}_age"
        if getattr(fight, attribute) is None:
            setattr(fight, attribute, _age_on_date(fight_value, event_date))
            hydrated += 1

    for field_name in ("height_inches", "reach_inches", "stance"):
        profile_value = getattr(fighter, field_name)
        attribute = f"fighter_{side}_{field_name}"
        if getattr(fight, attribute) is None and profile_value is not None:
            setattr(fight, attribute, profile_value)
            hydrated += 1
    return hydrated


def _write_fighter_aggregate(fighter: Fighter, state: _FighterState) -> None:
    fighter.fights = state.fights
    fighter.wins = state.wins
    fighter.losses = state.losses
    fighter.draws = state.draws
    fighter.no_contests = state.no_contests
    fighter.current_win_streak = state.current_win_streak
    fighter.current_loss_streak = state.current_loss_streak
    fighter.last_fight_date = state.last_fight_date
    for field_name, value in state.stats.items():
        setattr(fighter, field_name, value)


def rebuild_derived_state(session: Session) -> DerivedStateReport:
    """Rebuild all history-derived columns from canonical fight rows.

    The operation is idempotent and deliberately does not commit; callers can
    compose it atomically with a full or incremental scrape. It refuses to
    guess ambiguous winners or chronology.
    """

    session.flush()
    fighters = {fighter.fighter_id: fighter for fighter in session.scalars(select(Fighter))}
    entries = _source_rows(session)
    _validate_same_day_events(entries)
    ordered_entries, ordering_counts, planned_card_orders = _ordered_fights(entries)
    round_fight_ids = set(session.scalars(select(FightRound.fight_id).distinct()))

    # Pre-validate the complete replay before mutating ORM objects. This keeps
    # bad source data from leaving a partially rebuilt in-memory state.
    expected_winners: dict[str, str | None] = {}
    fighter_ids = set(fighters)
    for entry in ordered_entries:
        expected_winners[entry.fight.fight_id] = _validate_fight(entry.fight, fighter_ids)

    winner_ids_changed = sum(
        entry.fight.winner_fighter_id != expected_winners[entry.fight.fight_id]
        for entry in ordered_entries
    )
    source_event_urls_changed = sum(
        entry.fight.source_event_url != entry.event.event_url for entry in ordered_entries
    )
    stats_availability_changed = sum(
        bool(entry.fight.stats_available)
        != (entry.fight.fight_id in round_fight_ids)
        for entry in ordered_entries
    )
    card_orders_backfilled = sum(
        _integer_card_order(entry.fight) is None for entry in ordered_entries
    )
    fights_without_round_stats = sum(
        entry.fight.fight_id not in round_fight_ids for entry in ordered_entries
    )

    states = {fighter_id: _FighterState() for fighter_id in fighters}
    head_to_head_counts: Counter[tuple[str, str]] = Counter()
    profile_fields_hydrated = 0

    # A savepoint ensures that a late database constraint error rolls back all
    # derived changes while leaving transaction ownership with the caller.
    with session.begin_nested():
        for entry in ordered_entries:
            fight = entry.fight
            event_date = entry.event.event_date
            if event_date is None:  # already checked, narrows the type
                raise AssertionError(entry.event.event_id)

            stats_available = fight.fight_id in round_fight_ids
            fight.stats_available = stats_available
            fight.card_order = planned_card_orders[fight.fight_id]
            if not stats_available:
                for side in ("a", "b"):
                    for field_name in FIGHT_STAT_FIELDS:
                        setattr(fight, f"fighter_{side}_{field_name}", None)

            winner_id = expected_winners[fight.fight_id]
            if (fight.result or "").strip().lower() in {"draw", "no_contest"}:
                # Canonical winner representation for outcomes without a winner.
                # The validator and downstream SQL treat NULL, not an empty
                # string, as the absence of a winner.
                fight.winner_name = None
            fight.winner_fighter_id = winner_id
            fight.source_event_url = entry.event.event_url

            state_a = states[fight.fighter_a_id]
            state_b = states[fight.fighter_b_id]
            hydrated_a = _hydrate_fight_profile(
                fight, "a", fighters[fight.fighter_a_id], event_date
            )
            hydrated_b = _hydrate_fight_profile(
                fight, "b", fighters[fight.fighter_b_id], event_date
            )
            if hydrated_a:
                fight.fighter_a_profile_imputed = True
            if hydrated_b:
                fight.fighter_b_profile_imputed = True
            profile_fields_hydrated += hydrated_a + hydrated_b
            _set_prefight_fields(fight, "a", state_a, event_date)
            _set_prefight_fields(fight, "b", state_b, event_date)

            pair = tuple(sorted((fight.fighter_a_id, fight.fighter_b_id)))
            fight.head_to_head_fight_count = head_to_head_counts[pair]

            result = (fight.result or "").strip().lower()
            if result == "draw":
                outcome_a = outcome_b = "draw"
            elif result == "no_contest":
                outcome_a = outcome_b = "no_contest"
            else:
                outcome_a = "win" if winner_id == fight.fighter_a_id else "loss"
                outcome_b = "win" if winner_id == fight.fighter_b_id else "loss"

            _apply_outcome(state_a, outcome_a)
            _apply_outcome(state_b, outcome_b)
            _add_fight_stats(fight, "a", state_a)
            _add_fight_stats(fight, "b", state_b)
            state_a.last_fight_date = event_date
            state_b.last_fight_date = event_date
            head_to_head_counts[pair] += 1

        for fighter_id, fighter in fighters.items():
            _write_fighter_aggregate(fighter, states[fighter_id])

        session.flush()

    return DerivedStateReport(
        fights_processed=len(ordered_entries),
        fighters_rebuilt=len(fighters),
        fights_without_round_stats=fights_without_round_stats,
        winner_ids_changed=winner_ids_changed,
        source_event_urls_changed=source_event_urls_changed,
        stats_availability_changed=stats_availability_changed,
        card_orders_backfilled=card_orders_backfilled,
        profile_fields_hydrated=profile_fields_hydrated,
        events_using_card_order=ordering_counts["card_order"],
        events_using_fallback_order=ordering_counts["fallback"],
    )


__all__ = [
    "DerivedStateError",
    "DerivedStateReport",
    "FIGHT_STAT_FIELDS",
    "FIGHTER_AGGREGATE_STAT_FIELDS",
    "PREFIGHT_FIELDS",
    "rebuild_derived_state",
]
