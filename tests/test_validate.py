from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from pipeline.migrations import (
    FIGHTER_BOUT_HISTORY_VIEW,
    FIGHTER_BOUT_HISTORY_VIEW_SQL,
    upgrade_schema,
)
from pipeline.models import Base, Event, Fight, FightRound, Fighter
from pipeline.validate import validate_database


def _fighter(
    fighter_id: str,
    name: str,
    *,
    fights: int,
    wins: int,
    losses: int,
    win_streak: int,
    loss_streak: int,
    sig_landed: int,
    sig_attempted: int,
    td_landed: int,
    td_attempted: int,
    control: int,
    last_fight_date: date = date(2024, 1, 1),
) -> Fighter:
    return Fighter(
        fighter_id=fighter_id,
        fighter_name=name,
        fighter_nickname=f"{name} nickname",
        date_of_birth=date(1990, 1, 1),
        height_inches=70,
        reach_inches=72,
        stance="Orthodox",
        profile_url=f"http://example.test/fighter/{fighter_id}",
        fights=fights,
        wins=wins,
        losses=losses,
        draws=0,
        no_contests=0,
        current_win_streak=win_streak,
        current_loss_streak=loss_streak,
        significant_strikes_landed=sig_landed,
        significant_strikes_attempted=sig_attempted,
        takedowns_landed=td_landed,
        takedowns_attempted=td_attempted,
        control_time_seconds=control,
        last_fight_date=last_fight_date,
    )


def _fight(
    fight_id: str,
    event_id: str,
    fighter_a_id: str,
    fighter_a_name: str,
    fighter_b_id: str,
    fighter_b_name: str,
    *,
    fighter_a_wins: int = 0,
    fighter_a_win_streak: int = 0,
    fighter_a_rest: int | None = None,
    a_sig_landed: int = 10,
    a_sig_attempted: int = 20,
    a_total_landed: int = 15,
    a_total_attempted: int = 25,
    a_td_landed: int = 1,
    a_td_attempted: int = 2,
    a_control: int = 60,
    b_sig_landed: int = 8,
    b_sig_attempted: int = 18,
    b_total_landed: int = 12,
    b_total_attempted: int = 22,
    b_td_landed: int = 0,
    b_td_attempted: int = 1,
    b_control: int = 30,
    card_order: int = 1,
) -> Fight:
    return Fight(
        fight_id=fight_id,
        event_id=event_id,
        fighter_a_id=fighter_a_id,
        fighter_a_name=fighter_a_name,
        weight_class="Lightweight",
        card_order=card_order,
        event_method_code="U-DEC",
        stats_available=True,
        fighter_a_age=30,
        fighter_a_height_inches=70,
        fighter_a_reach_inches=72,
        fighter_a_stance="Orthodox",
        fighter_a_wins=fighter_a_wins,
        fighter_a_losses=0,
        fighter_a_draws=0,
        fighter_a_current_win_streak=fighter_a_win_streak,
        fighter_a_current_loss_streak=0,
        fighter_a_significant_strikes_landed=a_sig_landed,
        fighter_a_significant_strikes_attempted=a_sig_attempted,
        fighter_a_takedowns_landed=a_td_landed,
        fighter_a_takedowns_attempted=a_td_attempted,
        fighter_a_control_time_seconds=a_control,
        fighter_a_total_strikes_landed=a_total_landed,
        fighter_a_total_strikes_attempted=a_total_attempted,
        fighter_a_knockdowns=1,
        fighter_a_submission_attempts=0,
        fighter_a_reversals=0,
        fighter_a_days_since_last_fight=fighter_a_rest,
        fighter_b_id=fighter_b_id,
        fighter_b_name=fighter_b_name,
        fighter_b_age=30,
        fighter_b_height_inches=70,
        fighter_b_reach_inches=72,
        fighter_b_stance="Orthodox",
        fighter_b_wins=0,
        fighter_b_losses=0,
        fighter_b_draws=0,
        fighter_b_current_win_streak=0,
        fighter_b_current_loss_streak=0,
        fighter_b_significant_strikes_landed=b_sig_landed,
        fighter_b_significant_strikes_attempted=b_sig_attempted,
        fighter_b_takedowns_landed=b_td_landed,
        fighter_b_takedowns_attempted=b_td_attempted,
        fighter_b_control_time_seconds=b_control,
        fighter_b_total_strikes_landed=b_total_landed,
        fighter_b_total_strikes_attempted=b_total_attempted,
        fighter_b_knockdowns=0,
        fighter_b_submission_attempts=0,
        fighter_b_reversals=0,
        fighter_b_days_since_last_fight=None,
        winner_fighter_id=fighter_a_id,
        winner_name=fighter_a_name,
        method_of_victory="U-DEC",
        method_detail="Decision",
        result="completed",
        referee="Referee",
        time_format="1 Rnd (5)",
        ending_round=1,
        ending_time="5:00",
        head_to_head_fight_count=0,
        source_event_url="http://example.test/event/one",
        source_fight_url=f"http://example.test/fight/{fight_id}",
        last_scraped_at=datetime.now(timezone.utc),
    )


def _round(
    fight_id: str,
    fighter_id: str,
    opponent_id: str,
    *,
    kd: int,
    sig_landed: int,
    sig_attempted: int,
    total_landed: int,
    total_attempted: int,
    td_landed: int,
    td_attempted: int,
    control: int,
) -> FightRound:
    return FightRound(
        fight_id=fight_id,
        round_number=1,
        fighter_id=fighter_id,
        opponent_id=opponent_id,
        knockdowns=kd,
        significant_strikes_landed=sig_landed,
        significant_strikes_attempted=sig_attempted,
        total_strikes_landed=total_landed,
        total_strikes_attempted=total_attempted,
        takedowns_landed=td_landed,
        takedowns_attempted=td_attempted,
        submission_attempts=0,
        reversals=0,
        control_time_seconds=control,
    )


def _valid_database(tmp_path):
    path = tmp_path / "valid.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    upgrade_schema(engine)
    with Session(engine) as session:
        session.add(
            Event(
                event_id="event-one",
                event_name="Event One",
                event_date=date(2024, 1, 1),
                event_location="Las Vegas",
                event_url="http://example.test/event/one",
                source_fight_count=1,
                last_scraped_at=datetime.now(timezone.utc),
            )
        )
        session.add_all(
            [
                _fighter(
                    "fighter-a", "Fighter A", fights=1, wins=1, losses=0,
                    win_streak=1, loss_streak=0, sig_landed=10, sig_attempted=20,
                    td_landed=1, td_attempted=2, control=60,
                ),
                _fighter(
                    "fighter-b", "Fighter B", fights=1, wins=0, losses=1,
                    win_streak=0, loss_streak=1, sig_landed=8, sig_attempted=18,
                    td_landed=0, td_attempted=1, control=30,
                ),
            ]
        )
        session.add(_fight("fight-one", "event-one", "fighter-a", "Fighter A", "fighter-b", "Fighter B"))
        session.add_all(
            [
                _round(
                    "fight-one", "fighter-a", "fighter-b", kd=1,
                    sig_landed=10, sig_attempted=20, total_landed=15,
                    total_attempted=25, td_landed=1, td_attempted=2, control=60,
                ),
                _round(
                    "fight-one", "fighter-b", "fighter-a", kd=0,
                    sig_landed=8, sig_attempted=18, total_landed=12,
                    total_attempted=22, td_landed=0, td_attempted=1, control=30,
                ),
            ]
        )
        session.commit()
    engine.dispose()
    return path


def _statuses(report):
    return {check.check_id: check.status for check in report.checks}


def test_valid_database_passes_hard_checks_with_metadata_warnings(tmp_path):
    path = _valid_database(tmp_path)

    report = validate_database(path, feature_columns=["fights.fighter_a_wins"])

    assert report.ok
    assert report.hard_failure_count == 0
    assert _statuses(report)["schema.fighter_bout_history"] == "PASS"
    assert _statuses(report)["schema.fighter_bout_history_columns"] == "PASS"
    assert _statuses(report)["fighter_bout_history.cardinality"] == "PASS"
    assert _statuses(report)["fighter_bout_history.participants"] == "PASS"
    assert _statuses(report)["fighter_bout_history.outcomes"] == "PASS"
    assert _statuses(report)["totals.fight_vs_rounds"] == "PASS"
    assert _statuses(report)["chronology.bout_start_replay"] == "PASS"
    assert _statuses(report)["audit.manifest"] == "WARN"


def test_missing_fighter_bout_history_view_is_a_hard_failure(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(f'DROP VIEW "{FIGHTER_BOUT_HISTORY_VIEW}"')
    engine.dispose()

    report = validate_database(path)

    assert _statuses(report)["schema.fighter_bout_history"] == "FAIL"
    assert not report.ok


def test_fighter_bout_history_requires_named_time_domain_columns(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(f'DROP VIEW "{FIGHTER_BOUT_HISTORY_VIEW}"')
        connection.exec_driver_sql(
            f"CREATE VIEW {FIGHTER_BOUT_HISTORY_VIEW} AS SELECT fight_id FROM fights"
        )
    engine.dispose()

    report = validate_database(path)

    statuses = _statuses(report)
    assert statuses["schema.fighter_bout_history"] == "PASS"
    assert statuses["schema.fighter_bout_history_columns"] == "FAIL"
    assert not report.ok


def test_fighter_bout_history_requires_two_correct_participant_rows(tmp_path):
    path = _valid_database(tmp_path)
    one_sided_view = FIGHTER_BOUT_HISTORY_VIEW_SQL + "\nWHERE p.fighter_slot='a'"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(f'DROP VIEW "{FIGHTER_BOUT_HISTORY_VIEW}"')
        connection.exec_driver_sql(one_sided_view)
    engine.dispose()

    report = validate_database(path)

    statuses = _statuses(report)
    assert statuses["fighter_bout_history.cardinality"] == "FAIL"
    assert statuses["fighter_bout_history.participants"] == "FAIL"
    assert not report.ok


def test_fighter_bout_history_outcomes_must_match_each_perspective(tmp_path):
    path = _valid_database(tmp_path)
    incorrect_outcomes = FIGHTER_BOUT_HISTORY_VIEW_SQL.replace(
        "THEN 'win'", "THEN 'not-a-win'"
    )
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(f'DROP VIEW "{FIGHTER_BOUT_HISTORY_VIEW}"')
        connection.exec_driver_sql(incorrect_outcomes)
    engine.dispose()

    report = validate_database(path)

    statuses = _statuses(report)
    assert statuses["fighter_bout_history.cardinality"] == "PASS"
    assert statuses["fighter_bout_history.participants"] == "PASS"
    assert statuses["fighter_bout_history.outcomes"] == "FAIL"
    assert not report.ok


def test_history_view_feature_manifest_distinguishes_prefight_from_bout_data(tmp_path):
    path = _valid_database(tmp_path)

    safe = validate_database(
        path,
        feature_columns=[
            "fighter_bout_history.prefight_wins",
            "fighter_bout_history.opponent_prefight_losses",
        ],
    )
    leaked = validate_database(
        path,
        feature_columns=[
            "fighter_bout_history.prefight_wins",
            "fighter_bout_history.bout_outcome",
            "fighter_bout_history.opponent_bout_significant_strikes_landed",
        ],
    )

    assert _statuses(safe)["prediction.feature_manifest"] == "PASS"
    assert _statuses(leaked)["prediction.feature_manifest"] == "FAIL"
    assert not leaked.ok


def test_invalid_winner_id_is_a_foreign_key_and_semantic_hard_failure(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            update(Fight).where(Fight.fight_id == "fight-one").values(winner_fighter_id="fighter-a-name-slug")
        )
    engine.dispose()

    report = validate_database(path)

    statuses = _statuses(report)
    assert not report.ok
    assert statuses["sqlite.foreign_key_check"] == "FAIL"
    assert statuses["semantics.winner"] == "FAIL"


def test_round_shape_and_totals_mismatches_are_hard_failures(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            update(Fight).where(Fight.fight_id == "fight-one").values(fighter_a_significant_strikes_landed=9)
        )
        connection.exec_driver_sql(
            "DELETE FROM fight_rounds WHERE fight_id='fight-one' AND fighter_id='fighter-b'"
        )
    engine.dispose()

    report = validate_database(path)

    statuses = _statuses(report)
    assert not report.ok
    assert statuses["rounds.two_sided"] == "FAIL"
    # A totals-only corruption remains detectable even though the other fighter's
    # missing row prevents the joined two-sided totals check for this fight.
    assert statuses["aggregates.fighters"] == "FAIL"


def test_fight_vs_round_totals_mismatch_is_a_hard_failure(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            update(Fight).where(Fight.fight_id == "fight-one").values(fighter_a_significant_strikes_landed=9)
        )
    engine.dispose()

    report = validate_database(path)

    assert _statuses(report)["totals.fight_vs_rounds"] == "FAIL"
    assert not report.ok


def test_populated_strike_splits_must_match_both_partitions(tmp_path):
    path = _valid_database(tmp_path)
    fight_values = {
        "fighter_a_significant_head_strikes_landed": 11,
        "fighter_a_significant_head_strikes_attempted": 20,
        "fighter_a_significant_body_strikes_landed": 0,
        "fighter_a_significant_body_strikes_attempted": 0,
        "fighter_a_significant_leg_strikes_landed": 0,
        "fighter_a_significant_leg_strikes_attempted": 0,
        "fighter_a_significant_distance_strikes_landed": 10,
        "fighter_a_significant_distance_strikes_attempted": 20,
        "fighter_a_significant_clinch_strikes_landed": 0,
        "fighter_a_significant_clinch_strikes_attempted": 0,
        "fighter_a_significant_ground_strikes_landed": 0,
        "fighter_a_significant_ground_strikes_attempted": 0,
    }
    round_values = {
        key.removeprefix("fighter_a_"): value for key, value in fight_values.items()
    }
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            update(Fight).where(Fight.fight_id == "fight-one").values(**fight_values)
        )
        connection.execute(
            update(FightRound)
            .where(FightRound.fight_id == "fight-one", FightRound.fighter_id == "fighter-a")
            .values(**round_values)
        )
    engine.dispose()

    report = validate_database(path)

    statuses = _statuses(report)
    assert statuses["splits.fight_identities"] == "FAIL"
    assert statuses["splits.round_identities"] == "FAIL"
    # Fight and round split values are equal to each other; it is the two
    # significant-strike partition identities that reject them.
    assert statuses["totals.fight_vs_rounds"] == "PASS"


def test_required_metadata_only_hard_fails_the_named_column(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            update(Fight)
            .where(Fight.fight_id == "fight-one")
            .values(event_method_code=None, last_scraped_at=None)
        )
    engine.dispose()

    populated = validate_database(path, required_metadata={"weight_class"})
    assert populated.ok
    assert _statuses(populated)["metadata.fight_card_metadata"] == "WARN"

    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            update(Fight).where(Fight.fight_id == "fight-one").values(weight_class=None)
        )
    engine.dispose()
    missing = validate_database(path, required_metadata={"weight_class"})

    assert _statuses(missing)["metadata.fight_card_metadata.required"] == "FAIL"
    assert not missing.ok


def test_bout_start_policy_replays_card_order_desc_and_is_the_safe_default(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        fighter_a = session.get(Fighter, "fighter-a")
        event = session.get(Event, "event-one")
        event.source_fight_count = 2
        fighter_a.fights = 2
        fighter_a.wins = 2
        fighter_a.current_win_streak = 2
        fighter_a.significant_strikes_landed = 15
        fighter_a.significant_strikes_attempted = 30
        fighter_a.takedowns_landed = 1
        fighter_a.takedowns_attempted = 2
        fighter_a.control_time_seconds = 90
        # fight-one is card_order=1 (the main event), so the earlier
        # card_order=2 bout must contribute to its pre-bout state.
        fight_one = session.get(Fight, "fight-one")
        fight_one.fighter_a_wins = 1
        fight_one.fighter_a_current_win_streak = 1
        fight_one.fighter_a_days_since_last_fight = 0
        session.add(
            _fighter(
                "fighter-c", "Fighter C", fights=1, wins=0, losses=1,
                win_streak=0, loss_streak=1, sig_landed=4, sig_attempted=9,
                td_landed=0, td_attempted=0, control=10,
            )
        )
        session.add(
            _fight(
                "fight-two", "event-one", "fighter-a", "Fighter A", "fighter-c", "Fighter C",
                a_sig_landed=5, a_sig_attempted=10, a_total_landed=7,
                a_total_attempted=12, a_td_landed=0, a_td_attempted=0, a_control=30,
                b_sig_landed=4, b_sig_attempted=9, b_total_landed=5,
                b_total_attempted=10, b_td_landed=0, b_td_attempted=0, b_control=10,
                card_order=2,
            )
        )
        session.add_all(
            [
                _round(
                    "fight-two", "fighter-a", "fighter-c", kd=1,
                    sig_landed=5, sig_attempted=10, total_landed=7,
                    total_attempted=12, td_landed=0, td_attempted=0, control=30,
                ),
                _round(
                    "fight-two", "fighter-c", "fighter-a", kd=0,
                    sig_landed=4, sig_attempted=9, total_landed=5,
                    total_attempted=10, td_landed=0, td_attempted=0, control=10,
                ),
            ]
        )
        session.commit()
    engine.dispose()

    bout_report = validate_database(path)
    event_report = validate_database(path, temporal_policy="event-start")
    ingestion_report = validate_database(path, temporal_policy="ingestion")

    assert bout_report.ok
    assert _statuses(bout_report)["chronology.bout_start_replay"] == "PASS"
    assert _statuses(event_report)["chronology.event_start_features"] == "FAIL"
    assert _statuses(ingestion_report)["chronology.ingestion_replay"] == "FAIL"
    assert not ingestion_report.ok


def test_missing_card_order_is_a_hard_failure(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            update(Fight).where(Fight.fight_id == "fight-one").values(card_order=None)
        )
    engine.dispose()

    report = validate_database(path)

    assert _statuses(report)["coverage.card_order"] == "FAIL"
    assert not report.ok


def test_historical_gap_append_only_warns_on_rowid_order(tmp_path):
    path = _valid_database(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        session.add(
            Event(
                event_id="event-zero",
                event_name="Event Zero",
                event_date=date(2023, 1, 1),
                event_location="Denver",
                event_url="http://example.test/event/zero",
                source_fight_count=1,
                last_scraped_at=datetime.now(timezone.utc),
            )
        )
        session.add_all(
            [
                _fighter(
                    "fighter-c", "Fighter C", fights=1, wins=1, losses=0,
                    win_streak=1, loss_streak=0, sig_landed=6, sig_attempted=12,
                    td_landed=0, td_attempted=0, control=20,
                    last_fight_date=date(2023, 1, 1),
                ),
                _fighter(
                    "fighter-d", "Fighter D", fights=1, wins=0, losses=1,
                    win_streak=0, loss_streak=1, sig_landed=3, sig_attempted=8,
                    td_landed=0, td_attempted=0, control=10,
                    last_fight_date=date(2023, 1, 1),
                ),
            ]
        )
        fight = _fight(
            "fight-zero", "event-zero", "fighter-c", "Fighter C", "fighter-d", "Fighter D",
            a_sig_landed=6, a_sig_attempted=12, a_total_landed=8, a_total_attempted=14,
            a_td_landed=0, a_td_attempted=0, a_control=20,
            b_sig_landed=3, b_sig_attempted=8, b_total_landed=5, b_total_attempted=10,
            b_td_landed=0, b_td_attempted=0, b_control=10,
        )
        fight.source_event_url = "http://example.test/event/zero"
        session.add(fight)
        session.add_all(
            [
                _round(
                    "fight-zero", "fighter-c", "fighter-d", kd=1,
                    sig_landed=6, sig_attempted=12, total_landed=8,
                    total_attempted=14, td_landed=0, td_attempted=0, control=20,
                ),
                _round(
                    "fight-zero", "fighter-d", "fighter-c", kd=0,
                    sig_landed=3, sig_attempted=8, total_landed=5,
                    total_attempted=10, td_landed=0, td_attempted=0, control=10,
                ),
            ]
        )
        session.commit()
    engine.dispose()

    report = validate_database(path)

    assert report.ok
    assert _statuses(report)["chronology.insertion_order"] == "WARN"
    assert _statuses(report)["chronology.bout_start_replay"] == "PASS"


def test_promotion_gate_checks_cumulative_source_manifest(tmp_path):
    path = _valid_database(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "run_id": "run-001",
        "status": "completed",
        "code_version": "abc123",
        "started_at": now,
        "finished_at": now,
        "errors": [],
        "listing": {
            "url": "http://example.test/events/completed",
            "fetched_at": now,
            "event_urls": ["http://example.test/event/one"],
            "latest_event_date": "2024-01-01",
        },
        "event_fights": {
            "http://example.test/event/one": ["http://example.test/fight/fight-one"],
        },
        "page_counts": {
            "listing_pages_expected": 1,
            "listing_pages_succeeded": 1,
            "event_pages_expected": 1,
            "event_pages_succeeded": 1,
            "fight_pages_expected": 1,
            "fight_pages_succeeded": 1,
            "failed_pages": 0,
        },
        "feature_columns": ["fights.fighter_a_wins", "fights.head_to_head_fight_count"],
    }

    report = validate_database(
        path,
        manifest=manifest,
        promotion_gate=True,
        initial_load=True,
        max_manifest_age_hours=48,
        audit_target_declared=True,
    )

    assert report.ok
    assert report.run_id == "run-001"
    assert _statuses(report)["coverage.source_listing"] == "PASS"
    assert _statuses(report)["coverage.fight_manifests"] == "PASS"
    assert _statuses(report)["audit.page_accounting"] == "PASS"


def test_promotion_gate_rejects_unresolved_errors_and_incomplete_card(tmp_path):
    path = _valid_database(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "run_id": "run-002",
        "status": "completed",
        "code_version": "abc123",
        "started_at": now,
        "finished_at": now,
        "errors": [{"url": "http://example.test/fight/missing", "message": "timeout"}],
        "listing": {
            "url": "http://example.test/events/completed",
            "fetched_at": now,
            "event_urls": ["http://example.test/event/one"],
            "latest_event_date": "2024-01-01",
        },
        "event_fights": {
            "http://example.test/event/one": [
                "http://example.test/fight/fight-one",
                "http://example.test/fight/missing",
            ],
        },
        "page_counts": {
            "listing_pages_expected": 1,
            "listing_pages_succeeded": 1,
            "event_pages_expected": 1,
            "event_pages_succeeded": 1,
            "fight_pages_expected": 2,
            "fight_pages_succeeded": 1,
            "failed_pages": 1,
        },
    }

    report = validate_database(
        path,
        manifest=manifest,
        promotion_gate=True,
        initial_load=True,
        max_manifest_age_hours=48,
        audit_target_declared=True,
    )

    statuses = _statuses(report)
    assert not report.ok
    assert statuses["audit.unresolved_errors"] == "FAIL"
    assert statuses["audit.page_accounting"] == "FAIL"
    assert statuses["coverage.fight_manifests"] == "FAIL"
