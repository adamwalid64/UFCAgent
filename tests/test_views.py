from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pipeline.db import init_db
from pipeline.migrations import FIGHTER_BOUT_HISTORY_VIEW, upgrade_schema
from pipeline.models import Event, Fight, Fighter


@pytest.fixture
def history_engine():
    engine = init_db("sqlite:///:memory:")
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    Fighter(fighter_id="alpha", fighter_name="Alpha"),
                    Fighter(fighter_id="beta", fighter_name="Beta"),
                    Fighter(fighter_id="gamma", fighter_name="Gamma"),
                ]
            )
            session.add(
                Event(
                    event_id="event-1",
                    event_name="UFC Test",
                    event_date=date(2025, 1, 1),
                    event_location="Test City",
                    source_fight_count=2,
                )
            )
            session.flush()
            # card_order=2 is the opener and therefore occurs before the
            # card_order=1 main event. Alpha deliberately changes A/B slots.
            session.add_all(
                [
                    Fight(
                        fight_id="opener",
                        event_id="event-1",
                        card_order=2,
                        weight_class="Lightweight",
                        fighter_a_id="alpha",
                        fighter_a_name="Alpha",
                        fighter_b_id="beta",
                        fighter_b_name="Beta",
                        fighter_a_age=25,
                        fighter_b_age=29,
                        fighter_a_profile_imputed=True,
                        fighter_b_profile_imputed=False,
                        fighter_a_wins=0,
                        fighter_a_losses=0,
                        fighter_a_draws=0,
                        fighter_a_current_win_streak=0,
                        fighter_a_current_loss_streak=0,
                        fighter_a_days_since_last_fight=None,
                        fighter_b_wins=2,
                        fighter_b_losses=1,
                        fighter_b_draws=0,
                        fighter_b_current_win_streak=1,
                        fighter_b_current_loss_streak=0,
                        fighter_b_days_since_last_fight=90,
                        fighter_a_significant_strikes_landed=10,
                        fighter_a_significant_strikes_attempted=20,
                        fighter_b_significant_strikes_landed=12,
                        fighter_b_significant_strikes_attempted=25,
                        winner_fighter_id="beta",
                        winner_name="Beta",
                        result="completed",
                        method_of_victory="Decision - Unanimous",
                        stats_available=True,
                        head_to_head_fight_count=0,
                    ),
                    Fight(
                        fight_id="main",
                        event_id="event-1",
                        card_order=1,
                        weight_class="Welterweight",
                        fighter_a_id="gamma",
                        fighter_a_name="Gamma",
                        fighter_b_id="alpha",
                        fighter_b_name="Alpha",
                        fighter_a_age=31,
                        fighter_b_age=25,
                        fighter_a_profile_imputed=False,
                        fighter_b_profile_imputed=True,
                        fighter_a_wins=5,
                        fighter_a_losses=2,
                        fighter_a_draws=0,
                        fighter_a_current_win_streak=2,
                        fighter_a_current_loss_streak=0,
                        fighter_a_days_since_last_fight=120,
                        fighter_b_wins=0,
                        fighter_b_losses=1,
                        fighter_b_draws=0,
                        fighter_b_current_win_streak=0,
                        fighter_b_current_loss_streak=1,
                        fighter_b_days_since_last_fight=0,
                        fighter_a_significant_strikes_landed=9,
                        fighter_a_significant_strikes_attempted=19,
                        fighter_b_significant_strikes_landed=20,
                        fighter_b_significant_strikes_attempted=30,
                        winner_fighter_id="alpha",
                        winner_name="Alpha",
                        result="completed",
                        method_of_victory="KO/TKO",
                        stats_available=True,
                        head_to_head_fight_count=0,
                    ),
                ]
            )
            session.commit()
        yield engine
    finally:
        engine.dispose()


def test_fighter_bout_history_normalizes_both_slots_and_labels_time_domains(
    history_engine,
):
    with history_engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT
                    fighter_bout_sequence,
                    event_bout_sequence,
                    source_card_order,
                    fighter_slot,
                    fighter_id,
                    opponent_id,
                    prefight_wins,
                    prefight_losses,
                    prefight_profile_imputed,
                    opponent_prefight_wins,
                    bout_outcome,
                    bout_significant_strikes_landed,
                    opponent_bout_significant_strikes_landed
                FROM {FIGHTER_BOUT_HISTORY_VIEW}
                WHERE fighter_id = 'alpha'
                ORDER BY fighter_bout_sequence
                """
            )
        ).mappings().all()
        perspective_count = connection.execute(
            text(f"SELECT count(*) FROM {FIGHTER_BOUT_HISTORY_VIEW}")
        ).scalar_one()

    assert perspective_count == 4
    assert [row["fighter_bout_sequence"] for row in rows] == [1, 2]
    assert [row["event_bout_sequence"] for row in rows] == [1, 2]
    assert [row["source_card_order"] for row in rows] == [2, 1]
    assert [row["fighter_slot"] for row in rows] == ["a", "b"]
    assert [row["opponent_id"] for row in rows] == ["beta", "gamma"]
    assert [row["prefight_wins"] for row in rows] == [0, 0]
    assert [row["prefight_losses"] for row in rows] == [0, 1]
    assert [row["prefight_profile_imputed"] for row in rows] == [1, 1]
    assert [row["opponent_prefight_wins"] for row in rows] == [2, 5]
    assert [row["bout_outcome"] for row in rows] == ["loss", "win"]
    assert [row["bout_significant_strikes_landed"] for row in rows] == [10, 20]
    assert [row["opponent_bout_significant_strikes_landed"] for row in rows] == [12, 9]


def test_history_view_migration_is_upgradeable_idempotent_and_read_only(history_engine):
    with history_engine.begin() as connection:
        connection.execute(text(f'DROP VIEW "{FIGHTER_BOUT_HISTORY_VIEW}"'))
        connection.execute(text(f"CREATE VIEW {FIGHTER_BOUT_HISTORY_VIEW} AS SELECT 1 AS stale"))

    # Re-running the weekly additive migration replaces an older view
    # definition without touching canonical rows.
    upgrade_schema(history_engine)
    upgrade_schema(history_engine)

    inspector = inspect(history_engine)
    columns = {column["name"] for column in inspector.get_columns(FIGHTER_BOUT_HISTORY_VIEW)}
    fight_columns = {column["name"]: column for column in inspector.get_columns("fights")}
    assert FIGHTER_BOUT_HISTORY_VIEW in inspector.get_view_names()
    assert "stale" not in columns
    assert {
        "fighter_id",
        "opponent_id",
        "prefight_wins",
        "opponent_prefight_wins",
        "bout_outcome",
        "bout_significant_strikes_landed",
        "opponent_bout_significant_strikes_landed",
    }.issubset(columns)
    assert "fighter_a_wins" not in columns
    assert "fighter_b_wins" not in columns
    assert fight_columns["fighter_a_profile_imputed"]["nullable"] is False
    assert fight_columns["fighter_b_profile_imputed"]["nullable"] is False

    with history_engine.begin() as connection:
        with pytest.raises(OperationalError, match="view"):
            connection.execute(
                text(
                    f"INSERT INTO {FIGHTER_BOUT_HISTORY_VIEW} "
                    "(fighter_id) VALUES ('cannot-write')"
                )
            )
