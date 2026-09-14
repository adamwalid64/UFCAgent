from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from datetime import date
from pathlib import Path

import pytest

from pipeline import weekly


EVENT_ONE = "http://www.ufcstats.com/event-details/eeeeeeeeeeeeeee1"
EVENT_TWO = "http://www.ufcstats.com/event-details/eeeeeeeeeeeeeee2"
FIGHT_ONE = "http://www.ufcstats.com/fight-details/fffffffffffffff1"
FIGHT_TWO = "http://www.ufcstats.com/fight-details/fffffffffffffff2"


SOURCE_EVENTS = (
    {
        "event_url": EVENT_ONE,
        "event_name": "UFC Fixture One",
        "event_date": "2025-01-01",
        "event_location": "Las Vegas, Nevada",
        "fight_url": FIGHT_ONE,
        "fighter_a_id": "aaaaaaaaaaaaaaa1",
        "fighter_a_name": "Alpha One",
        "fighter_b_id": "bbbbbbbbbbbbbbb1",
        "fighter_b_name": "Beta One",
        "stat_seed": 0,
    },
    {
        "event_url": EVENT_TWO,
        "event_name": "UFC Fixture Two",
        "event_date": "2025-02-01",
        "event_location": "New York, New York",
        "fight_url": FIGHT_TWO,
        "fighter_a_id": "aaaaaaaaaaaaaaa2",
        "fighter_a_name": "Alpha Two",
        "fighter_b_id": "bbbbbbbbbbbbbbb2",
        "fighter_b_name": "Beta Two",
        "stat_seed": 2,
    },
)


def _stats(seed: int) -> dict:
    significant_landed = 10 + seed
    significant_attempted = 20 + seed
    return {
        "knockdowns": seed % 2,
        "significant_strikes_landed": significant_landed,
        "significant_strikes_attempted": significant_attempted,
        "total_strikes_landed": significant_landed + 5,
        "total_strikes_attempted": significant_attempted + 5,
        "takedowns_landed": 1,
        "takedowns_attempted": 2,
        "submission_attempts": 0,
        "reversals": 0,
        "control_time_seconds": 30 + seed,
        "significant_strike_splits": {
            "head": {"landed": significant_landed, "attempted": significant_attempted},
            "body": {"landed": 0, "attempted": 0},
            "leg": {"landed": 0, "attempted": 0},
            "distance": {
                "landed": significant_landed,
                "attempted": significant_attempted,
            },
            "clinch": {"landed": 0, "attempted": 0},
            "ground": {"landed": 0, "attempted": 0},
        },
    }


class FakeFirstPartyScraper:
    """Deterministic implementation of the first-party scraper contract."""

    def __init__(
        self,
        *,
        event_count: int,
        corrupt_fight_totals: bool,
        delay_seconds: float,
        headless: bool,
    ):
        assert delay_seconds == 0
        assert headless is True
        self.events = list(SOURCE_EVENTS[:event_count])
        self.corrupt_fight_totals = corrupt_fight_totals
        self._event_metadata = {
            event["event_url"]: {
                "event_name": event["event_name"],
                "event_date": event["event_date"],
                "event_location": event["event_location"],
            }
            for event in self.events
        }

    def crawl_event_listing(self, max_events=None):
        # UFCStats returns the newest completed event first.
        urls = [event["event_url"] for event in reversed(self.events)]
        if max_events is not None:
            urls = urls[:max_events]
        return type("Summary", (), {"event_urls": urls, "errors": []})()

    def crawl_event_details(self, event_url):
        event = self._event(event_url=event_url)
        return {
            "event_name": event["event_name"],
            "event_date": event["event_date"],
            "event_location": event["event_location"],
            "event_url": event_url,
            "fight_urls": [event["fight_url"]],
            "fight_summaries": [
                {
                    "fight_id": self._id(event["fight_url"]),
                    "fight_url": event["fight_url"],
                    "weight_class": "Lightweight",
                    "method_of_victory": "KO/TKO",
                    "event_method_code": "KO/TKO",
                    "card_order": 1,
                }
            ],
        }

    def crawl_fight_card(self, fight_url):
        event = self._event(fight_url=fight_url)
        round_a = _stats(event["stat_seed"])
        round_b = _stats(event["stat_seed"] + 1)
        total_a = _stats(
            event["stat_seed"] + (7 if self.corrupt_fight_totals else 0)
        )
        return {
            "fight_id": self._id(fight_url),
            "fighter_a_id": event["fighter_a_id"],
            "fighter_a_name": event["fighter_a_name"],
            "fighter_a_url": self._fighter_url(event["fighter_a_id"]),
            "fighter_b_id": event["fighter_b_id"],
            "fighter_b_name": event["fighter_b_name"],
            "fighter_b_url": self._fighter_url(event["fighter_b_id"]),
            "winner_name": event["fighter_a_name"],
            "result": "completed",
            "method_of_victory": "KO/TKO",
            "method_detail": "Punches",
            "ending_round": 1,
            "ending_time": "5:00",
            "time_format": "3 Rnd (5-5-5)",
            "referee": "Fixture Referee",
            "weight_class": "Lightweight",
            "event_method_code": "KO/TKO",
            "card_order": 1,
            "fighter_a_stats": total_a,
            "fighter_b_stats": round_b,
            "rounds": [
                {
                    "round_number": 1,
                    "fighter_a": round_a,
                    "fighter_b": round_b,
                }
            ],
            "stats_parse_status": "complete",
            "stats_parse_detail": "fixture includes aligned totals and round tables",
            "source_fight_url": fight_url,
        }

    def crawl_fighter_profile(self, fighter_url):
        fighter_id = self._id(fighter_url)
        event = self._event(fighter_id=fighter_id)
        if fighter_id == event["fighter_a_id"]:
            name = event["fighter_a_name"]
        else:
            name = event["fighter_b_name"]
        return {
            "fighter_id": fighter_id,
            "fighter_name": name,
            "fighter_nickname": f"{name} Nickname",
            "date_of_birth": date(1990, 1, 1),
            "height_inches": 70,
            "reach_inches": 72,
            "stance": "Orthodox",
            "profile_url": fighter_url,
        }

    def close(self):
        return None

    def _event(self, **identity):
        key, expected = next(iter(identity.items()))
        for event in self.events:
            if key == "fighter_id":
                actual = {event["fighter_a_id"], event["fighter_b_id"]}
                if expected in actual:
                    return event
            elif event[key] == expected:
                return event
        raise AssertionError(f"Unknown source identity: {key}={expected!r}")

    @staticmethod
    def _id(url):
        return str(url).rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _fighter_url(fighter_id):
        return f"http://www.ufcstats.com/fighter-details/{fighter_id}"


@pytest.fixture
def weekly_workspace():
    path = Path.cwd() / f".weekly-e2e-{uuid.uuid4().hex}"
    path.mkdir(parents=False, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def _install_fake(monkeypatch, scenario):
    def factory(*, delay_seconds, headless):
        return FakeFirstPartyScraper(
            event_count=scenario["event_count"],
            corrupt_fight_totals=scenario.get("corrupt_fight_totals", False),
            delay_seconds=delay_seconds,
            headless=headless,
        )

    # Everything except the network boundary remains the production path.
    monkeypatch.setattr(weekly, "RateLimitedPlaywrightScraper", factory)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _counts(path: Path) -> tuple[int, int, int, int]:
    connection = sqlite3.connect(path)
    try:
        return tuple(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("events", "fights", "fighters", "fight_rounds")
        )
    finally:
        connection.close()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_weekly_initial_load_promotes_and_next_success_retains_previous(
    monkeypatch, weekly_workspace
):
    scenario = {"event_count": 1}
    _install_fake(monkeypatch, scenario)
    database = weekly_workspace / "live.db"
    runs = weekly_workspace / "runs"

    first_code, first_run, first_report = weekly.refresh_database(
        database,
        full=True,
        delay_seconds=0,
        runs_directory=runs,
    )

    assert first_code == 0
    assert first_report.ok
    assert database.is_file()
    assert _counts(database) == (1, 1, 2, 2)
    assert not (weekly_workspace / "live.previous.db").exists()
    assert not (first_run / "live.staging.db").exists()
    first_manifest = _json(first_run / "manifest.json")
    assert first_manifest["status"] == "completed"
    assert first_manifest["processed_events"] == 1
    assert first_manifest["processed_fights"] == 1
    assert first_manifest["listing"]["event_urls"] == [EVENT_ONE]
    assert first_manifest["event_fights"] == {EVENT_ONE: [FIGHT_ONE]}
    assert _json(first_run / "validation.json")["ok"] is True

    scenario["event_count"] = 2
    second_code, second_run, second_report = weekly.refresh_database(
        database,
        full=True,
        delay_seconds=0,
        runs_directory=runs,
    )

    assert second_code == 0
    assert second_report.ok
    assert _counts(database) == (2, 2, 4, 4)
    previous = weekly_workspace / "live.previous.db"
    assert previous.is_file()
    assert _counts(previous) == (1, 1, 2, 2)
    assert not (second_run / "live.staging.db").exists()
    second_manifest = _json(second_run / "manifest.json")
    assert second_manifest["listing"]["event_urls"] == [EVENT_TWO, EVENT_ONE]
    assert second_manifest["event_fights"] == {
        EVENT_ONE: [FIGHT_ONE],
        EVENT_TWO: [FIGHT_TWO],
    }
    audit_rows = [
        json.loads(line)
        for line in (runs / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(audit_rows) == 2
    assert all(row["ok"] and row["promoted_to"] == str(database.resolve()) for row in audit_rows)


def test_failed_weekly_candidate_cannot_change_live_database(
    monkeypatch, weekly_workspace
):
    scenario = {"event_count": 1}
    _install_fake(monkeypatch, scenario)
    database = weekly_workspace / "live.db"
    runs = weekly_workspace / "runs"
    initial_code, _, initial_report = weekly.refresh_database(
        database,
        full=True,
        delay_seconds=0,
        runs_directory=runs,
    )
    assert initial_code == 0 and initial_report.ok
    live_hash = _sha256(database)
    live_counts = _counts(database)

    scenario["corrupt_fight_totals"] = True
    failed_code, failed_run, failed_report = weekly.refresh_database(
        database,
        full=True,
        delay_seconds=0,
        runs_directory=runs,
    )

    assert failed_code == 1
    assert not failed_report.ok
    assert any(
        check.check_id == "totals.fight_vs_rounds" and check.status == "FAIL"
        for check in failed_report.checks
    )
    assert _sha256(database) == live_hash
    assert _counts(database) == live_counts
    assert not (weekly_workspace / "live.previous.db").exists()
    retained_staging = failed_run / "live.staging.db"
    assert retained_staging.is_file()
    assert _sha256(retained_staging) != live_hash
    assert _json(failed_run / "manifest.json")["status"] == "completed"
    assert _json(failed_run / "validation.json")["ok"] is False
    audit_rows = [
        json.loads(line)
        for line in (runs / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(audit_rows) == 2
    assert audit_rows[-1]["ok"] is False
    assert audit_rows[-1]["promoted_to"] is None


def test_cross_volume_runs_directory_is_rejected_before_scraper_starts(
    monkeypatch, weekly_workspace
):
    database = weekly_workspace / "live.db"
    runs = weekly_workspace / "other-volume-runs"
    scraper_started = False

    def volume_identity(path):
        resolved = Path(path).resolve()
        return (1, "database") if resolved == database.parent.resolve() else (2, "runs")

    def forbidden_scraper(**_kwargs):
        nonlocal scraper_started
        scraper_started = True
        raise AssertionError("scraper must not start for a cross-volume staging path")

    monkeypatch.setattr(weekly, "_volume_identity", volume_identity)
    monkeypatch.setattr(weekly, "RateLimitedPlaywrightScraper", forbidden_scraper)

    with pytest.raises(ValueError, match="same filesystem volume"):
        weekly.refresh_database(database, delay_seconds=0, runs_directory=runs)

    assert scraper_started is False
    assert runs.is_dir()
    assert list(runs.iterdir()) == []


def _sharing_violation() -> PermissionError:
    error = PermissionError("file is open by another process")
    error.winerror = 32
    return error


def test_promotion_retries_sharing_violation_then_rotates_previous_after_commit(
    monkeypatch, weekly_workspace
):
    staging = weekly_workspace / "live.staging.db"
    destination = weekly_workspace / "live.db"
    previous = weekly_workspace / "live.previous.db"
    staging.write_text("validated candidate", encoding="utf-8")
    destination.write_text("current live", encoding="utf-8")
    previous.write_text("older good previous", encoding="utf-8")
    monkeypatch.setattr(weekly, "_sqlite_backup", shutil.copyfile)

    real_replace = weekly.os.replace
    live_attempts = 0
    sleeps: list[float] = []

    def flaky_replace(source, target):
        nonlocal live_attempts
        source = Path(source)
        target = Path(target)
        if source == staging and target == destination:
            live_attempts += 1
            # A blocked live commit must not disturb the established recovery
            # version while retries are in progress.
            assert previous.read_text(encoding="utf-8") == "older good previous"
            if live_attempts < 3:
                raise _sharing_violation()
        if target == previous:
            # Rotation is deliberately after the candidate commit point.
            assert destination.read_text(encoding="utf-8") == "validated candidate"
            assert previous.read_text(encoding="utf-8") == "older good previous"
        return real_replace(source, target)

    monkeypatch.setattr(weekly.os, "replace", flaky_replace)
    monkeypatch.setattr(weekly.time, "sleep", sleeps.append)

    retained = weekly._promote(staging, destination)

    assert retained == previous
    assert live_attempts == 3
    assert len(sleeps) == 2
    assert destination.read_text(encoding="utf-8") == "validated candidate"
    assert previous.read_text(encoding="utf-8") == "current live"


def test_exhausted_promotion_retries_leave_live_and_good_previous_unchanged(
    monkeypatch, weekly_workspace
):
    staging = weekly_workspace / "live.staging.db"
    destination = weekly_workspace / "live.db"
    previous = weekly_workspace / "live.previous.db"
    staging.write_text("validated candidate", encoding="utf-8")
    destination.write_text("current live", encoding="utf-8")
    previous.write_text("older good previous", encoding="utf-8")
    monkeypatch.setattr(weekly, "_sqlite_backup", shutil.copyfile)

    real_replace = weekly.os.replace
    live_attempts = 0

    def blocked_replace(source, target):
        nonlocal live_attempts
        if Path(source) == staging and Path(target) == destination:
            live_attempts += 1
            raise _sharing_violation()
        return real_replace(source, target)

    monkeypatch.setattr(weekly.os, "replace", blocked_replace)
    monkeypatch.setattr(weekly.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="another process"):
        weekly._promote(staging, destination)

    assert live_attempts == weekly._REPLACE_ATTEMPTS
    assert staging.read_text(encoding="utf-8") == "validated candidate"
    assert destination.read_text(encoding="utf-8") == "current live"
    assert previous.read_text(encoding="utf-8") == "older good previous"
    assert list(weekly_workspace.glob(".live.previous.db.*.tmp")) == []


def test_failed_previous_rotation_keeps_new_live_and_both_recovery_versions(
    monkeypatch, weekly_workspace
):
    staging = weekly_workspace / "live.staging.db"
    destination = weekly_workspace / "live.db"
    previous = weekly_workspace / "live.previous.db"
    staging.write_text("validated candidate", encoding="utf-8")
    destination.write_text("current live", encoding="utf-8")
    previous.write_text("older good previous", encoding="utf-8")
    monkeypatch.setattr(weekly, "_sqlite_backup", shutil.copyfile)

    real_replace = weekly.os.replace
    rotation_attempts = 0

    def blocked_rotation(source, target):
        nonlocal rotation_attempts
        if Path(target) == previous:
            rotation_attempts += 1
            raise _sharing_violation()
        return real_replace(source, target)

    monkeypatch.setattr(weekly.os, "replace", blocked_rotation)
    monkeypatch.setattr(weekly.time, "sleep", lambda _seconds: None)

    retained = weekly._promote(staging, destination)

    assert rotation_attempts == weekly._REPLACE_ATTEMPTS
    assert destination.read_text(encoding="utf-8") == "validated candidate"
    assert previous.read_text(encoding="utf-8") == "older good previous"
    assert retained != previous
    assert retained.is_file()
    assert retained.read_text(encoding="utf-8") == "current live"


def test_post_promotion_audit_failure_returns_success_without_repromotion(
    monkeypatch, weekly_workspace, caplog
):
    scenario = {"event_count": 1}
    _install_fake(monkeypatch, scenario)
    database = weekly_workspace / "live.db"
    runs = weekly_workspace / "runs"
    original_promote = weekly._promote
    promotion_calls = 0

    def tracked_promote(staging, destination):
        nonlocal promotion_calls
        promotion_calls += 1
        return original_promote(staging, destination)

    def failed_audit(*_args, **_kwargs):
        raise OSError("fault-injected audit disk error")

    monkeypatch.setattr(weekly, "_promote", tracked_promote)
    monkeypatch.setattr(weekly, "_append_audit", failed_audit)
    caplog.set_level("ERROR", logger=weekly.__name__)

    exit_code = weekly.main(
        [
            "--db",
            str(database),
            "--full",
            "--delay",
            "0",
            "--runs-dir",
            str(runs),
        ]
    )

    assert exit_code == 0
    assert promotion_calls == 1
    assert _counts(database) == (1, 1, 2, 2)
    assert any(
        "post-promotion audit append failed" in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        "failed before promotion" in record.getMessage().lower()
        for record in caplog.records
    )
    run_directories = [path for path in runs.iterdir() if path.is_dir()]
    assert len(run_directories) == 1
    assert _json(run_directories[0] / "validation.json")["ok"] is True


def test_staging_refresh_restores_database_globals(monkeypatch, weekly_workspace):
    scenario = {"event_count": 1}
    _install_fake(monkeypatch, scenario)
    database = weekly_workspace / "live.db"
    original_engine = weekly.database.engine
    original_session_local = weekly.database.SessionLocal

    code, _, report = weekly.refresh_database(
        database,
        full=True,
        delay_seconds=0,
        runs_directory=weekly_workspace / "runs",
    )

    assert code == 0 and report.ok
    assert weekly.database.engine is original_engine
    assert weekly.database.SessionLocal is original_session_local
