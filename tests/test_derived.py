from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pipeline.db import init_db
from pipeline.derived import DerivedStateError, FIGHT_STAT_FIELDS, rebuild_derived_state
from pipeline.migrations import upgrade_schema
from pipeline.models import Event, Fight, FightRound, Fighter


def _session() -> Session:
    engine = init_db("sqlite:///:memory:")
    return Session(engine, expire_on_commit=False)


def _fighter(fighter_id: str, name: str) -> Fighter:
    # Deliberately corrupt aggregates prove that the replay replaces rather
    # than increments existing derived state.
    return Fighter(
        fighter_id=fighter_id,
        fighter_name=name,
        fights=99,
        wins=99,
        losses=99,
        draws=99,
        no_contests=99,
        current_win_streak=99,
        current_loss_streak=99,
        significant_strikes_landed=99,
        significant_strikes_attempted=99,
        takedowns_landed=99,
        takedowns_attempted=99,
        control_time_seconds=99,
    )


def _fight(
    fight_id: str,
    event_id: str,
    fighter_a: Fighter,
    fighter_b: Fighter,
    winner_name: str | None,
    *,
    result: str = "completed",
    stat_seed: int = 1,
    created_at: datetime | None = None,
) -> Fight:
    fight = Fight(
        fight_id=fight_id,
        event_id=event_id,
        fighter_a_id=fighter_a.fighter_id,
        fighter_a_name=fighter_a.fighter_name,
        fighter_b_id=fighter_b.fighter_id,
        fighter_b_name=fighter_b.fighter_name,
        winner_name=winner_name,
        winner_fighter_id=None,
        result=result,
        source_event_url="stale-event-url",
        fighter_a_wins=88,
        fighter_a_losses=88,
        fighter_a_draws=88,
        fighter_a_current_win_streak=88,
        fighter_a_current_loss_streak=88,
        fighter_a_days_since_last_fight=88,
        fighter_b_wins=88,
        fighter_b_losses=88,
        fighter_b_draws=88,
        fighter_b_current_win_streak=88,
        fighter_b_current_loss_streak=88,
        fighter_b_days_since_last_fight=88,
        head_to_head_fight_count=88,
        fighter_a_knockdowns=stat_seed,
        fighter_a_significant_strikes_landed=stat_seed * 10,
        fighter_a_significant_strikes_attempted=stat_seed * 20,
        fighter_a_total_strikes_landed=stat_seed * 12,
        fighter_a_total_strikes_attempted=stat_seed * 22,
        fighter_a_takedowns_landed=stat_seed,
        fighter_a_takedowns_attempted=stat_seed * 2,
        fighter_a_submission_attempts=stat_seed,
        fighter_a_reversals=0,
        fighter_a_control_time_seconds=stat_seed * 30,
        fighter_b_knockdowns=0,
        fighter_b_significant_strikes_landed=stat_seed * 5,
        fighter_b_significant_strikes_attempted=stat_seed * 10,
        fighter_b_total_strikes_landed=stat_seed * 6,
        fighter_b_total_strikes_attempted=stat_seed * 11,
        fighter_b_takedowns_landed=0,
        fighter_b_takedowns_attempted=stat_seed,
        fighter_b_submission_attempts=0,
        fighter_b_reversals=0,
        fighter_b_control_time_seconds=stat_seed * 10,
        created_at=created_at,
    )
    # The newly persisted significant-strike splits are part of availability
    # semantics too. Zero is a genuine measured value when round rows exist.
    for side in ("a", "b"):
        for field_name in FIGHT_STAT_FIELDS[10:]:
            setattr(fight, f"fighter_{side}_{field_name}", 0)
    return fight


def _add_round_rows(session: Session, fight: Fight) -> None:
    for fighter_id, opponent_id, side in (
        (fight.fighter_a_id, fight.fighter_b_id, "a"),
        (fight.fighter_b_id, fight.fighter_a_id, "b"),
    ):
        round_row = FightRound(
                fight_id=fight.fight_id,
                round_number=1,
                fighter_id=fighter_id,
                opponent_id=opponent_id,
                knockdowns=getattr(fight, f"fighter_{side}_knockdowns"),
                significant_strikes_landed=getattr(
                    fight, f"fighter_{side}_significant_strikes_landed"
                ),
                significant_strikes_attempted=getattr(
                    fight, f"fighter_{side}_significant_strikes_attempted"
                ),
                total_strikes_landed=getattr(fight, f"fighter_{side}_total_strikes_landed"),
                total_strikes_attempted=getattr(
                    fight, f"fighter_{side}_total_strikes_attempted"
                ),
                takedowns_landed=getattr(fight, f"fighter_{side}_takedowns_landed"),
                takedowns_attempted=getattr(fight, f"fighter_{side}_takedowns_attempted"),
                submission_attempts=getattr(
                    fight, f"fighter_{side}_submission_attempts"
                ),
                reversals=getattr(fight, f"fighter_{side}_reversals"),
                control_time_seconds=getattr(
                    fight, f"fighter_{side}_control_time_seconds"
                ),
            )
        for field_name in FIGHT_STAT_FIELDS[10:]:
            setattr(round_row, field_name, 0)
        session.add(round_row)


def _derived_snapshot(session: Session) -> tuple:
    fights = []
    for fight in session.scalars(select(Fight).order_by(Fight.fight_id)):
        fights.append(
            (
                fight.fight_id,
                fight.winner_fighter_id,
                fight.source_event_url,
                fight.stats_available,
                fight.card_order,
                fight.fighter_a_age,
                fight.fighter_a_height_inches,
                fight.fighter_a_reach_inches,
                fight.fighter_a_stance,
                fight.fighter_a_profile_imputed,
                fight.fighter_b_age,
                fight.fighter_b_height_inches,
                fight.fighter_b_reach_inches,
                fight.fighter_b_stance,
                fight.fighter_b_profile_imputed,
                fight.fighter_a_wins,
                fight.fighter_a_losses,
                fight.fighter_a_draws,
                fight.fighter_a_current_win_streak,
                fight.fighter_a_current_loss_streak,
                fight.fighter_a_days_since_last_fight,
                fight.fighter_b_wins,
                fight.fighter_b_losses,
                fight.fighter_b_draws,
                fight.fighter_b_current_win_streak,
                fight.fighter_b_current_loss_streak,
                fight.fighter_b_days_since_last_fight,
                fight.head_to_head_fight_count,
                *(getattr(fight, f"fighter_{side}_{name}") for side in ("a", "b") for name in FIGHT_STAT_FIELDS),
            )
        )
    fighters = [
        (
            fighter.fighter_id,
            fighter.fights,
            fighter.wins,
            fighter.losses,
            fighter.draws,
            fighter.no_contests,
            fighter.current_win_streak,
            fighter.current_loss_streak,
            fighter.significant_strikes_landed,
            fighter.significant_strikes_attempted,
            fighter.takedowns_landed,
            fighter.takedowns_attempted,
            fighter.control_time_seconds,
            fighter.last_fight_date,
        )
        for fighter in session.scalars(select(Fighter).order_by(Fighter.fighter_id))
    ]
    return tuple(fights), tuple(fighters)


def test_rebuild_uses_descending_card_order_and_recomputes_all_derived_state():
    with _session() as session:
        alpha = _fighter("alpha-id", "Alpha")
        beta = _fighter("beta-id", "Beta")
        charlie = _fighter("charlie-id", "Charlie")
        alpha.date_of_birth = date(1990, 6, 15)
        alpha.height_inches = 72
        alpha.reach_inches = 75
        alpha.stance = "Orthodox"
        charlie.date_of_birth = date(1992, 1, 1)
        charlie.height_inches = 70
        charlie.reach_inches = 73
        charlie.stance = "Southpaw"
        session.add_all([alpha, beta, charlie])

        tournament = Event(
            event_id="event-1",
            event_name="Tournament",
            event_date=date(2024, 1, 1),
            event_url="https://events/1",
        )
        rematch_event = Event(
            event_id="event-2",
            event_name="Rematch",
            event_date=date(2024, 2, 1),
            event_url="https://events/2",
        )
        session.add_all([tournament, rematch_event])
        session.flush()

        # Deliberately insert the opener first so SQLite rowid would give the
        # wrong order. Persisted card_order must win: larger means earlier.
        opening = _fight("opening", "event-1", alpha, beta, "Alpha", stat_seed=1)
        final = _fight("final", "event-1", alpha, charlie, "Charlie", stat_seed=2)
        rematch = _fight("rematch", "event-2", alpha, charlie, "Alpha", stat_seed=3)
        setattr(opening, "card_order", 2)
        setattr(final, "card_order", 1)
        setattr(rematch, "card_order", 1)
        # A pre-existing fight-time snapshot is more authoritative than a
        # later fighter-profile value and must not be overwritten.
        final.fighter_a_stance = "Switch"
        session.add_all([opening, final, rematch])
        session.flush()
        for fight in (opening, final, rematch):
            _add_round_rows(session, fight)
        session.flush()

        report = rebuild_derived_state(session)

        assert report.fights_processed == 3
        assert report.fighters_rebuilt == 3
        assert report.events_using_card_order == 2
        assert report.events_using_fallback_order == 0
        assert report.winner_ids_changed == 3
        assert report.source_event_urls_changed == 3
        assert report.stats_availability_changed == 3
        assert report.card_orders_backfilled == 0
        assert report.profile_fields_hydrated > 0

        assert opening.winner_fighter_id == "alpha-id"
        assert final.winner_fighter_id == "charlie-id"
        assert rematch.winner_fighter_id == "alpha-id"
        assert opening.source_event_url == "https://events/1"
        assert rematch.source_event_url == "https://events/2"
        assert opening.stats_available is True
        assert opening.fighter_a_age == 33
        assert opening.fighter_a_height_inches == 72
        assert opening.fighter_a_reach_inches == 75
        assert opening.fighter_a_stance == "Orthodox"
        assert opening.fighter_a_profile_imputed is True
        assert opening.fighter_b_profile_imputed is False
        assert final.fighter_a_stance == "Switch"
        assert final.fighter_a_profile_imputed is True
        assert final.fighter_b_age == 32
        assert final.fighter_b_profile_imputed is True

        assert opening.fighter_a_wins == 0
        assert opening.fighter_a_days_since_last_fight is None
        assert final.fighter_a_wins == 1
        assert final.fighter_a_losses == 0
        assert final.fighter_a_current_win_streak == 1
        assert final.fighter_a_days_since_last_fight == 0
        assert rematch.fighter_a_wins == 1
        assert rematch.fighter_a_losses == 1
        assert rematch.fighter_a_current_loss_streak == 1
        assert rematch.fighter_a_days_since_last_fight == 31
        assert rematch.head_to_head_fight_count == 1

        assert alpha.fights == 3
        assert alpha.wins == 2
        assert alpha.losses == 1
        assert alpha.current_win_streak == 1
        assert alpha.current_loss_streak == 0
        assert alpha.significant_strikes_landed == 60
        assert alpha.significant_strikes_attempted == 120
        assert alpha.takedowns_landed == 6
        assert alpha.takedowns_attempted == 12
        assert alpha.control_time_seconds == 180
        assert alpha.last_fight_date == date(2024, 2, 1)

        first_snapshot = _derived_snapshot(session)
        second_report = rebuild_derived_state(session)
        assert _derived_snapshot(session) == first_snapshot
        assert second_report.winner_ids_changed == 0
        assert second_report.source_event_urls_changed == 0


def test_rebuild_falls_back_to_reverse_insertion_order_and_nulls_only_missing_stats():
    with _session() as session:
        alpha = _fighter("alpha-id", "Alpha")
        beta = _fighter("beta-id", "Beta")
        charlie = _fighter("charlie-id", "Charlie")
        session.add_all([alpha, beta, charlie])
        event = Event(
            event_id="event-1",
            event_name="Old Tournament",
            event_date=date(1995, 1, 1),
            event_url="https://events/old",
        )
        session.add(event)
        session.flush()

        # Source cards are inserted main-event first. Descending rowid puts
        # the later-inserted opener before the final during replay.
        final = _fight("final", "event-1", alpha, charlie, "Charlie", stat_seed=0)
        opener = _fight("opener", "event-1", alpha, beta, "Alpha", stat_seed=0)
        session.add_all([final, opener])
        session.flush()
        _add_round_rows(session, opener)
        session.flush()

        report = rebuild_derived_state(session)

        assert report.events_using_card_order == 0
        assert report.events_using_fallback_order == 1
        assert report.fights_without_round_stats == 1
        assert report.card_orders_backfilled == 2
        assert report.stats_availability_changed == 1
        assert final.card_order == 1
        assert opener.card_order == 2
        assert final.stats_available is False
        assert opener.stats_available is True
        assert opener.fighter_a_wins == 0
        assert final.fighter_a_wins == 1

        # A real all-zero performance has round rows and remains zero.
        for side in ("a", "b"):
            for field_name in FIGHT_STAT_FIELDS:
                assert getattr(opener, f"fighter_{side}_{field_name}") == 0

        # Parser-generated zeroes without any round rows become explicit NULL.
        for side in ("a", "b"):
            for field_name in FIGHT_STAT_FIELDS:
                assert getattr(final, f"fighter_{side}_{field_name}") is None

        first_snapshot = _derived_snapshot(session)
        rebuild_derived_state(session)
        assert _derived_snapshot(session) == first_snapshot


def test_rebuild_rejects_an_unjoinable_winner_before_mutating_rows():
    with _session() as session:
        alpha = _fighter("alpha-id", "Alpha")
        beta = _fighter("beta-id", "Beta")
        event = Event(
            event_id="event-1",
            event_name="Bad Data",
            event_date=date(2024, 1, 1),
            event_url="https://events/1",
        )
        fight = _fight("bad-fight", "event-1", alpha, beta, "Somebody Else")
        session.add_all([alpha, beta, event])
        session.flush()
        session.add(fight)
        session.flush()

        with pytest.raises(DerivedStateError, match="does not match exactly one side"):
            rebuild_derived_state(session)

        assert fight.winner_fighter_id is None
        assert fight.source_event_url == "stale-event-url"
        assert fight.fighter_a_wins == 88


def test_draw_resets_streak_and_no_contest_preserves_it():
    with _session() as session:
        alpha = _fighter("alpha-id", "Alpha")
        opponents = [
            _fighter("beta-id", "Beta"),
            _fighter("charlie-id", "Charlie"),
            _fighter("delta-id", "Delta"),
            _fighter("echo-id", "Echo"),
        ]
        session.add_all([alpha, *opponents])
        events = [
            Event(
                event_id=f"event-{index}",
                event_name=f"Event {index}",
                event_date=event_date,
                event_url=f"https://events/{index}",
            )
            for index, event_date in enumerate(
                (date(2024, 1, 1), date(2024, 1, 10), date(2024, 1, 20), date(2024, 2, 1)),
                1,
            )
        ]
        session.add_all(events)
        session.flush()

        win = _fight("win", "event-1", alpha, opponents[0], "Alpha")
        no_contest = _fight(
            "no-contest",
            "event-2",
            alpha,
            opponents[1],
            "",
            result="no_contest",
        )
        draw = _fight("draw", "event-3", alpha, opponents[2], None, result="draw")
        loss = _fight("loss", "event-4", alpha, opponents[3], "Echo")
        for fight in (win, no_contest, draw, loss):
            fight.card_order = 1
        session.add_all([win, no_contest, draw, loss])
        session.flush()

        rebuild_derived_state(session)

        assert win.winner_fighter_id == "alpha-id"
        assert no_contest.winner_fighter_id is None
        assert no_contest.winner_name is None
        assert draw.winner_fighter_id is None
        assert loss.winner_fighter_id == "echo-id"

        assert no_contest.fighter_a_wins == 1
        assert no_contest.fighter_a_current_win_streak == 1
        assert no_contest.fighter_a_days_since_last_fight == 9
        # A no contest does not alter the streak, but it is the most recent
        # physical bout for rest-day calculations.
        assert draw.fighter_a_current_win_streak == 1
        assert draw.fighter_a_days_since_last_fight == 10
        # A draw breaks both streaks before the next fight.
        assert loss.fighter_a_draws == 1
        assert loss.fighter_a_current_win_streak == 0
        assert loss.fighter_a_current_loss_streak == 0
        assert loss.fighter_a_days_since_last_fight == 12

        assert alpha.fights == 4
        assert alpha.wins == 1
        assert alpha.losses == 1
        assert alpha.draws == 1
        assert alpha.no_contests == 1
        assert alpha.current_win_streak == 0
        assert alpha.current_loss_streak == 1


def test_sqlite_migrations_are_idempotent_and_foreign_keys_are_enforced():
    engine = init_db("sqlite:///:memory:")
    try:
        first_schema = {
            table: tuple(column["name"] for column in inspect(engine).get_columns(table))
            for table in ("fighters", "events", "fights", "fight_rounds")
        }
        upgrade_schema(engine)
        upgrade_schema(engine)
        second_schema = {
            table: tuple(column["name"] for column in inspect(engine).get_columns(table))
            for table in ("fighters", "events", "fights", "fight_rounds")
        }
        assert second_schema == first_schema

        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO fights (
                            fight_id,event_id,fighter_a_id,fighter_a_name,
                            fighter_b_id,fighter_b_name,stats_available
                        ) VALUES (
                            'orphan','missing-event','missing-a','A',
                            'missing-b','B',0
                        )
                        """
                    )
                )
    finally:
        engine.dispose()
