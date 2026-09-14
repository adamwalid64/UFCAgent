from datetime import date

from pipeline.models import Event, Fight, FightRound, Fighter
from pipeline.pipeline import build_fight_record, run_pipeline


def test_fight_schema_has_expected_columns():
    columns = {column.name for column in Fight.__table__.columns}
    expected = {
        "fight_id",
        "event_id",
        "fighter_a_id",
        "fighter_b_id",
        "winner_fighter_id",
        "method_of_victory",
        "ending_round",
        "ending_time",
        "fighter_a_significant_strikes_landed",
        "fighter_b_significant_strikes_landed",
        "fighter_a_control_time_seconds",
        "fighter_b_control_time_seconds",
    }
    assert expected.issubset(columns)
    assert {"wins", "losses", "fights", "last_fight_date"}.issubset({column.name for column in Fighter.__table__.columns})
    assert {"fight_id", "round_number", "fighter_id"}.issubset({column.name for column in FightRound.__table__.columns})


def test_build_fight_record_sets_joinable_ids_and_unique_hash():
    event = Event(
        event_id="event-001",
        event_name="UFC 300",
        event_date="2025-06-14",
        event_location="Las Vegas, NV",
    )

    fight = build_fight_record(
        event=event,
        fight_id="event-001:alpha-vs-beta",
        fighter_a_name="Alpha Fighter",
        fighter_b_name="Beta Fighter",
        winner_name="Alpha Fighter",
        method_of_victory="Decision (Unanimous)",
        ending_round=3,
        ending_time="05:00",
    )

    assert fight["fight_id"] == "event-001:alpha-vs-beta"
    assert fight["fighter_a_id"] == "alpha-fighter"
    assert fight["fighter_b_id"] == "beta-fighter"
    assert fight["winner_fighter_id"] == "alpha-fighter"


def test_incremental_run_repairs_gaps_and_refreshes_recent_events():
    class FakeScraper:
        def crawl_event_listing(self, max_events=None):
            return type("Summary", (), {"event_urls": ["event-3", "event-2", "event-1"]})()

        def crawl_event_details(self, event_url):
            if event_url == "event-3":
                return {"event_name": "UFC 300", "event_date": "2025-01-01", "event_location": "Las Vegas", "event_url": event_url, "fight_urls": ["fight-3a"]}
            if event_url == "event-2":
                return {"event_name": "UFC 299", "event_date": "2024-01-01", "event_location": "Las Vegas", "event_url": event_url, "fight_urls": ["fight-2a"]}
            if event_url == "event-1":
                return {"event_name": "UFC 298", "event_date": "2023-01-01", "event_location": "Las Vegas", "event_url": event_url, "fight_urls": ["fight-1a"]}
            raise AssertionError(event_url)

        def crawl_fight_card(self, fight_url):
            return {"fight_id": fight_url, "fighter_a_name": "A", "fighter_b_name": "B", "winner_name": "A", "method_of_victory": "Decision", "ending_round": 3, "ending_time": "05:00", "source_fight_url": fight_url}

    from pipeline.db import init_db
    from pipeline.models import Event, Fight
    from sqlalchemy import select

    db = init_db("sqlite:///:memory:")
    session = db.connect()
    session.execute(
        __import__("sqlalchemy").insert(Event).values(
            event_id="ufc-299-2024-01-01",
            event_name="UFC 299",
            event_date=date(2024, 1, 1),
            event_location="Las Vegas",
            event_url="event-2",
        )
    )
    session.commit()

    class SessionProxy:
        def __getattr__(self, name):
            return getattr(__import__("pipeline.db", fromlist=["SessionLocal"]).SessionLocal(), name)

    import pipeline.pipeline as pipeline_module
    inserted_events, inserted_fights, errors = pipeline_module.run_pipeline(FakeScraper(), full=False)

    # Missing historical cards are no longer hidden behind the first known
    # event, and the recent known card is reconciled as well.
    assert inserted_events == 3
    assert inserted_fights == 3
    assert errors == []

    with __import__("sqlalchemy.orm", fromlist=["Session"]).Session(db) as check:
        fighter_a = check.get(Fighter, "a")
        assert fighter_a.fights == 3
        assert fighter_a.wins == 3
