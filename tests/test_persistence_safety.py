from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from pipeline.locking import InterProcessLock, LockUnavailableError
from pipeline.models import Base, Event, Fight, FightRound, Fighter
from pipeline.pipeline import (
    ROUND_STAT_FIELDS,
    SIGNIFICANT_STRIKE_SPLITS,
    _round_payload_is_complete,
    _upsert_rounds,
    upsert_fight,
)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_fight(session: Session) -> Fight:
    fighter_a = Fighter(fighter_id="a-id", fighter_name="Alpha")
    fighter_b = Fighter(fighter_id="b-id", fighter_name="Beta")
    event = Event(
        event_id="event-id",
        event_name="Event",
        event_date=date(2025, 1, 1),
        event_url="https://events/one",
    )
    fight = Fight(
        fight_id="fight-id",
        event_id=event.event_id,
        fighter_a_id=fighter_a.fighter_id,
        fighter_a_name=fighter_a.fighter_name,
        fighter_b_id=fighter_b.fighter_id,
        fighter_b_name=fighter_b.fighter_name,
        winner_fighter_id=fighter_a.fighter_id,
        winner_name=fighter_a.fighter_name,
        result="completed",
        fighter_a_age=29,
        fighter_a_height_inches=70,
        fighter_a_reach_inches=72,
        fighter_a_stance="Orthodox",
        fighter_b_age=31,
        fighter_b_height_inches=73,
        fighter_b_reach_inches=76,
        fighter_b_stance="Southpaw",
        method_detail="old detail",
    )
    session.add_all([fighter_a, fighter_b, event, fight])
    session.flush()
    return fight


def _round(number: int, seed: int) -> dict:
    def side(offset: int) -> dict:
        value = seed + offset
        stats = {field: value for field in ROUND_STAT_FIELDS}
        stats["significant_strike_splits"] = {
            position: {"landed": value, "attempted": value + 1}
            for position in SIGNIFICANT_STRIKE_SPLITS
        }
        return stats

    return {
        "round_number": number,
        "fighter_a": side(0),
        "fighter_b": side(100),
    }


def test_fight_refresh_does_not_erase_existing_physical_snapshots_with_none():
    with _session() as session:
        fight = _seed_fight(session)

        refreshed = upsert_fight(
            session,
            {
                "fight_id": fight.fight_id,
                "fighter_a_age": None,
                "fighter_a_height_inches": 71,
                "fighter_a_reach_inches": None,
                "fighter_a_stance": None,
                "fighter_b_age": None,
                "fighter_b_height_inches": None,
                "fighter_b_reach_inches": None,
                "fighter_b_stance": None,
                "method_detail": None,
            },
        )

        assert refreshed.fighter_a_age == 29
        assert refreshed.fighter_a_height_inches == 71
        assert refreshed.fighter_a_reach_inches == 72
        assert refreshed.fighter_a_stance == "Orthodox"
        assert refreshed.fighter_b_age == 31
        assert refreshed.fighter_b_height_inches == 73
        assert refreshed.fighter_b_reach_inches == 76
        assert refreshed.fighter_b_stance == "Southpaw"
        # The preservation rule is intentionally selective; authoritative
        # non-physical fields retain normal upsert semantics.
        assert refreshed.method_detail is None


def test_round_upsert_keeps_ids_and_prunes_stale_keys_only_when_complete():
    with _session() as session:
        _seed_fight(session)
        initial = [_round(1, 1), _round(2, 2)]
        _upsert_rounds(
            session,
            "fight-id",
            initial,
            "a-id",
            "b-id",
            complete_payload=True,
        )
        original = {
            (row.round_number, row.fighter_id): row.round_stat_id
            for row in session.scalars(select(FightRound))
        }

        partial = [_round(1, 10)]
        assert _round_payload_is_complete(partial, ending_round=2) is False
        _upsert_rounds(
            session,
            "fight-id",
            partial,
            "a-id",
            "b-id",
            complete_payload=False,
        )
        after_partial = {
            (row.round_number, row.fighter_id): row
            for row in session.scalars(select(FightRound))
        }
        assert set(after_partial) == set(original)
        assert after_partial[(1, "a-id")].round_stat_id == original[(1, "a-id")]
        assert after_partial[(1, "a-id")].knockdowns == 10
        assert after_partial[(2, "a-id")].knockdowns == 2

        assert _round_payload_is_complete(partial, ending_round=1) is True
        _upsert_rounds(
            session,
            "fight-id",
            partial,
            "a-id",
            "b-id",
            complete_payload=True,
        )
        after_complete = {
            (row.round_number, row.fighter_id): row
            for row in session.scalars(select(FightRound))
        }
        assert set(after_complete) == {(1, "a-id"), (1, "b-id")}
        assert after_complete[(1, "a-id")].round_stat_id == original[(1, "a-id")]
        assert after_complete[(1, "b-id")].round_stat_id == original[(1, "b-id")]


def test_invalid_round_batch_is_rejected_before_existing_rows_are_mutated():
    with _session() as session:
        _seed_fight(session)
        _upsert_rounds(
            session,
            "fight-id",
            [_round(1, 1)],
            "a-id",
            "b-id",
            complete_payload=True,
        )

        with pytest.raises(ValueError, match="duplicate round key"):
            _upsert_rounds(
                session,
                "fight-id",
                [_round(1, 8), _round(1, 9)],
                "a-id",
                "b-id",
                complete_payload=True,
            )

        rows = list(session.scalars(select(FightRound)))
        assert len(rows) == 2
        assert {row.knockdowns for row in rows} == {1, 101}


def test_interprocess_lock_fails_fast_and_is_reusable_after_release():
    lock_path = Path.cwd() / f".persistence-test-{uuid.uuid4().hex}.lock"
    first = InterProcessLock(lock_path)
    second = InterProcessLock(lock_path)
    try:
        first.acquire()
        with pytest.raises(LockUnavailableError, match="already holds"):
            second.acquire()
        first.release()

        with second:
            assert lock_path.exists()
    finally:
        second.release()
        first.release()
        lock_path.unlink(missing_ok=True)


def test_weekly_refresh_lock_wraps_the_entire_refresh(monkeypatch):
    import pipeline.weekly as weekly

    calls: list[object] = []
    sentinel = object()

    class RecordingLock:
        def __init__(self, path):
            calls.append(("lock", Path(path).name))

        def __enter__(self):
            calls.append("enter")
            return self

        def __exit__(self, *exc_info):
            calls.append("exit")

    def fake_refresh(database_path, **kwargs):
        assert calls[-1] == "enter"
        calls.append(("refresh", Path(database_path).name, kwargs["full"]))
        return sentinel

    monkeypatch.setattr(weekly, "InterProcessLock", RecordingLock)
    monkeypatch.setattr(weekly, "_refresh_database_unlocked", fake_refresh)

    result = weekly.refresh_database(Path("weekly-test.db"), full=True)

    assert result is sentinel
    assert calls == [
        ("lock", ".weekly-test.db.weekly.lock"),
        "enter",
        ("refresh", "weekly-test.db", True),
        "exit",
    ]
