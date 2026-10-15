from __future__ import annotations

import copy
import hashlib
import json
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.db import init_db
from pipeline.models import Event, Fight, Fighter, UpcomingEvent, UpcomingFight
from pipeline.scraper import RateLimitedPlaywrightScraper, parse_upcoming_fight, parse_remaining_fights
from pipeline.upcoming import save_upcoming_card
from sql_agent import __main__ as cli
from sql_agent.analytics import MatchupTools
from sql_agent.cards import load_next_card, load_remaining_card, predict_card
from sql_agent.database import QuerySession
from sql_agent.evaluation import ReviewChecks


def pairing(position, a, b):
    fid = f"{position + 100:016x}"
    return {"fight_id": fid, "card_order": position,
            "fighter_a_id": f"{a:016x}", "fighter_a_name": f"Fighter {a}",
            "fighter_b_id": f"{b:016x}", "fighter_b_name": f"Fighter {b}",
            "weight_class": "Lightweight", "is_title_bout": False, "scheduled_rounds": None,
            "source_fight_url": f"http://www.ufcstats.com/fight-details/{fid}"}


@pytest.fixture
def card():
    return {"event_id": "eeeeeeeeeeeeeeee", "event_name": "UFC Upcoming Fixture",
            "event_date": "2099-01-10", "event_location": "Test City",
            "event_url": "http://www.ufcstats.com/event-details/eeeeeeeeeeeeeeee",
            "fights": [pairing(1, 1, 2), pairing(2, 3, 4)]}


@pytest.fixture
def schedule_db(tmp_path, card):
    path = tmp_path / "schedule.db"
    engine = init_db("sqlite:///" + path.as_posix())
    with Session(engine) as session:
        save_upcoming_card(session, card)
        session.commit()
    yield path, engine
    engine.dispose()


def test_schedule_replaces_changed_pairings_without_fake_results(schedule_db, card):
    path, engine = schedule_db
    changed = copy.deepcopy(card)
    changed["fights"][0]["fighter_b_id"] = "0000000000000005"
    changed["fights"][0]["fighter_b_name"] = "Replacement"
    changed["fights"] = changed["fights"][:1]
    with Session(engine) as session:
        save_upcoming_card(session, changed)
        session.commit()
        assert session.scalar(select(func.count()).select_from(UpcomingFight)) == 1
        assert session.scalar(select(func.count()).select_from(UpcomingEvent)) == 1
        for model in (Event, Fight, Fighter):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    stored = load_next_card(path)
    assert stored["fights"][0]["fighter_b_name"] == "Replacement"
    assert stored["source_fight_count"] == 1


def test_bad_schedule_keeps_prior_card_and_empty_listing_clears_it(schedule_db, card):
    path, engine = schedule_db
    bad = copy.deepcopy(card)
    bad["fights"][1]["fighter_a_id"] = bad["fights"][0]["fighter_a_id"]
    with Session(engine) as session:
        with pytest.raises(ValueError, match="repeated"):
            save_upcoming_card(session, bad)
        session.rollback()
    assert len(load_next_card(path)["fights"]) == 2
    with Session(engine) as session:
        save_upcoming_card(session, None)
        session.commit()
    with pytest.raises(ValueError, match="No upcoming card"):
        load_next_card(path)


def test_card_reader_is_readonly_and_rejects_past_or_completed_card(schedule_db, card):
    path, engine = schedule_db
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    load_next_card(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="passed"):
        load_next_card(path, today=date(2099, 1, 11))
    with Session(engine) as session:
        session.add(Event(event_id="completed", event_name=card["event_name"],
                          event_date=date(2099, 1, 10), event_url=card["event_url"]))
        session.commit()
    with pytest.raises(ValueError, match="No upcoming card"):
        load_next_card(path)


def test_schedule_only_debutant_resolves_without_leaking_bouts(schedule_db):
    path, _ = schedule_db
    with QuerySession(path, as_of_date="2020-01-01") as session:
        identity = session.resolve_fighter("Fighter 1")
        assert identity["fighter_id"] == "0000000000000001"
        assert identity["date_of_birth"] is None
        stats = MatchupTools(session).get_career_stats("Fighter 1")
        assert stats["career"]["fights"] == 0
        assert stats["career"]["sig_strikes_landed_per_min"] is None
        assert any("debut" in caveat for caveat in stats["caveats"])
        assert session.run_sql("SELECT COUNT(*) AS n FROM fights")["rows"] == [{"n": 0}]
        assert "error" in session.run_sql("SELECT * FROM upcoming_fights")


def source_row():
    headers = ["W/L", "Fighter", "KD", "STR", "TD", "SUB", "Weight class", "Method", "Round", "Time"]
    cells = [{"text": "", "paragraphs": []} for _ in headers]
    cells[6] = {"text": "Lightweight", "paragraphs": ["Lightweight"]}
    row = {"fight_url": "http://www.ufcstats.com/fight-details/ffffffffffffffff",
           "cells": cells, "result": "", "is_title_bout": True,
           "fighters": [{"name": "Alpha", "url": "http://www.ufcstats.com/fighter-details/aaaaaaaaaaaaaaaa"},
                        {"name": "Beta", "url": "http://www.ufcstats.com/fighter-details/bbbbbbbbbbbbbbbb"}]}
    return headers, row


def test_upcoming_row_parser_keeps_pairings_and_unknown_distance():
    headers, row = source_row()
    parsed = parse_upcoming_fight(headers, row, 1)
    assert parsed["fighter_a_name"] == "Alpha" and parsed["fighter_b_name"] == "Beta"
    assert parsed["is_title_bout"] and parsed["scheduled_rounds"] is None
    assert "winner_fighter_id" not in parsed and "result" not in parsed


def test_remaining_rows_skip_results_preserve_order_and_reject_unknown_status():
    headers, row = source_row()
    row["cells"][3]["text"] = "View\nMatchup"
    finished = copy.deepcopy(row)
    finished["fight_url"] = "http://www.ufcstats.com/fight-details/eeeeeeeeeeeeeeee"
    finished["result"] = "DRAW DRAW"
    final = copy.deepcopy(row)
    final["fight_url"] = "http://www.ufcstats.com/fight-details/dddddddddddddddd"
    remaining, completed = parse_remaining_fights(headers, [row, finished, final])
    assert completed == 1
    assert [f["card_order"] for f in remaining] == [1, 3]
    assert all("result" not in f for f in remaining)
    row["cells"][3]["text"] = ""
    with pytest.raises(ValueError, match="neither a result"):
        parse_remaining_fights(headers, [row])


def test_live_identity_is_memory_only_and_cannot_import_results(schedule_db):
    path, _ = schedule_db
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    live = {"fighter_id": "cccccccccccccccc", "fighter_name": "Live Debut", "wins": 99}
    with QuerySession(path, as_of_date="2026-10-10", fighter_identities=[live]) as session:
        identity = session.resolve_fighter("Live Debut")
        assert identity == {"fighter_id": live["fighter_id"], "fighter_name": "Live Debut", "date_of_birth": None}
        assert MatchupTools(session).get_career_stats("Live Debut")["career"]["fights"] == 0
        assert "error" in session.run_sql("SELECT wins FROM fighters")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with QuerySession(path) as session:
        with pytest.raises(ValueError):
            session.resolve_fighter("Live Debut")


def test_manual_exclusion_filters_only_an_exact_remaining_fighter(monkeypatch, card):
    closed = []
    monkeypatch.setattr(RateLimitedPlaywrightScraper, "crawl_remaining_card", lambda *a: copy.deepcopy(card))
    monkeypatch.setattr(RateLimitedPlaywrightScraper, "close", lambda *a: closed.append(True))
    remaining = load_remaining_card("Fixture", exclude_fights=["Fighter 1"])
    assert [f["card_order"] for f in remaining["fights"]] == [2]
    assert remaining["excluded_fights"][0]["fighter_a_name"] == "Fighter 1"
    assert closed == [True]
    with pytest.raises(ValueError, match="must match one"):
        load_remaining_card("Fixture", exclude_fights=["Typo"])


def test_remaining_source_errors_close_browser_without_a_cached_fallback(monkeypatch):
    closed = []
    def unavailable(*args):
        raise RuntimeError("source unavailable")
    monkeypatch.setattr(RateLimitedPlaywrightScraper, "crawl_remaining_card", unavailable)
    monkeypatch.setattr(RateLimitedPlaywrightScraper, "close", lambda *a: closed.append(True))
    with pytest.raises(RuntimeError, match="source unavailable"):
        load_remaining_card("Fixture")
    assert closed == [True]


@pytest.mark.parametrize("corruption", ["result", "method", "fighter", "fight_id"])
def test_upcoming_row_rejects_completed_or_incomplete_pairing(corruption):
    headers, row = source_row()
    if corruption == "result":
        row["result"] = "win"
    elif corruption == "method":
        row["cells"][7] = {"text": "KO/TKO", "paragraphs": ["KO/TKO"]}
    elif corruption == "fighter":
        row["fighters"].pop()
    else:
        row["fight_url"] = ""
    with pytest.raises(ValueError):
        parse_upcoming_fight(headers, row, 1)


class FakeAgent:
    def __init__(self, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    def predict(self, a, b, cutoff=None, **kwargs):
        self.calls.append((a, b, cutoff, kwargs))
        if self.fail_first and len(self.calls) == 1:
            raise ValueError("Fixture prediction failure")
        return {"fighter_a": a, "fighter_b": b, "predicted_winner": a, "win_probability": 0.7,
                "predicted_method": "Submission", "predicted_round": 2,
                "key_factors": ["Takedown advantage"], "caveats": [],
                "summary": "Submission threat after a takedown.", "queries_run": [{"sql": "SELECT 1"}],
                "evaluation": {"status": "approved", "review_rounds": 1, "revision_count": 0,
                               "summary": "Scripted approval for CLI tests.",
                               "checks": {name: True for name in ReviewChecks.model_fields}}}


def test_card_prediction_uses_date_and_distance_and_continues_after_failure(schedule_db):
    card = load_next_card(schedule_db[0])
    agent = FakeAgent(fail_first=True)
    delivered = []
    result = predict_card(agent, card, on_result=delivered.append)
    assert len(agent.calls) == len(delivered) == 2
    assert result["failed_fights"] == 1 and "error" in result["predictions"][0]
    assert result["predictions"][1]["predicted_round"] == 2
    assert all(call[2] == "2099-01-10" for call in agent.calls)
    assert [call[3]["scheduled_rounds"] for call in agent.calls] == [5, 3]
    assert all(call[3]["rounds_basis"].startswith("assumed") for call in agent.calls)
    with pytest.raises(ValueError, match="cutoff"):
        predict_card(agent, card, as_of_date="2099-01-11")


@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    directory = tmp_path / "predictions"
    monkeypatch.setattr(cli, "DEFAULT_OUTPUT_DIR", directory)
    return directory


def test_single_fight_cli_is_readable_with_optional_json(monkeypatch, capsys, report_dir):
    agent = FakeAgent()
    monkeypatch.setattr(cli, "SQLAnalysisAgent", lambda *a, **kw: agent)
    assert cli.main(["Alpha", "Beta", "--rounds", "5", "--event-name", "UFC Test Card"]) == 0
    text = capsys.readouterr().out
    assert "Pick: Alpha" in text and "Confidence: 70.0%" in text
    assert "Submission, round 2" in text and "Why:" in text
    assert "queries_run" not in text and '"sql"' not in text
    assert agent.calls[0][3]["scheduled_rounds"] == 5
    assert agent.calls[0][3]["event_name"] == "UFC Test Card"
    first_report = next(report_dir.glob("*.txt"))
    first_text = first_report.read_text(encoding="utf-8")
    assert "Pick: Alpha" in first_text and "Why:" in first_text
    assert cli.main(["Alpha", "Beta", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["queries_run"]
    assert len(list(report_dir.glob("*.txt"))) == 2
    assert first_report.read_text(encoding="utf-8") == first_text
    audit = json.loads(next(report_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert audit["queries_run"] == [{"sql": "SELECT 1"}]


def test_single_fight_failure_retains_usage_without_claiming_a_prediction(monkeypatch, capsys, report_dir):
    from types import SimpleNamespace
    from sql_agent.outcomes import read_report

    usage = {"request_count": 1, "input_tokens": 100, "output_tokens": 20, "reasoning_tokens": 0,
             "usage_complete": True, "estimated_usd": .001}
    def fail(*args, **kwargs):
        error = ValueError("Prediction withheld after review")
        error.model_usage = usage
        raise error
    monkeypatch.setattr(cli, "SQLAnalysisAgent", lambda *a, **kw: SimpleNamespace(predict=fail))
    assert cli.main(["Alpha", "Beta"]) == 1
    output = capsys.readouterr()
    assert "Prediction failed" in output.out and "Model usage: 1 calls" in output.out
    saved = read_report(next(report_dir.glob("*.txt")))["predictions"][0]
    assert saved["model_usage"] == usage and "predicted_winner" not in saved


def test_no_names_cli_predicts_saved_card_and_reports_partial_failure(schedule_db, monkeypatch, capsys, report_dir):
    agent = FakeAgent(fail_first=True)
    monkeypatch.setattr(cli, "SQLAnalysisAgent", lambda *a, **kw: agent)
    assert cli.main(["--db", str(schedule_db[0])]) == 1
    text = capsys.readouterr().out
    assert "UFC Upcoming Fixture" in text and "2 fights" in text
    assert "Prediction failed" in text and "Confidence: 70.0%" in text
    assert "queries_run" not in text
    saved = next(report_dir.glob("*.txt")).read_text(encoding="utf-8")
    assert "Fixture prediction failure" in saved and "Confidence: 70.0%" in saved
    assert "2 fight reports, 1 failed predictions" in saved


def test_interrupted_card_keeps_completed_reports_in_custom_folder(schedule_db, monkeypatch, capsys, report_dir):
    class InterruptedAgent(FakeAgent):
        def predict(self, *args, **kwargs):
            if self.calls:
                raise KeyboardInterrupt
            return super().predict(*args, **kwargs)
    agent = InterruptedAgent()
    monkeypatch.setattr(cli, "SQLAnalysisAgent", lambda *a, **kw: agent)
    custom = report_dir / "custom"
    assert cli.main(["--db", str(schedule_db[0]), "--output-dir", str(custom)]) == 130
    saved = next(custom.glob("*.txt")).read_text(encoding="utf-8")
    assert saved.count("Pick:") == 1
    assert "Run interrupted" in saved
    assert "Run complete:" not in saved
    assert "Prediction report:" in capsys.readouterr().err


def test_live_card_rechecks_before_and_after_each_prediction(schedule_db):
    card = load_next_card(schedule_db[0])
    card["live_remaining"] = True
    agent = FakeAgent()
    fresh = copy.deepcopy(card)
    fresh["fights"] = []
    snapshots = iter([card, fresh, fresh])
    delivered = []
    result = predict_card(agent, card, refresh_remaining=lambda: next(snapshots), on_result=delivered.append)
    assert len(agent.calls) == 1  # First finished while analyzing; second finished before its turn.
    assert agent.calls[0][2] == card["event_date"]
    assert len(agent.calls[0][3]["fighter_identities"]) == 2
    assert not delivered and not result["predictions"]
    assert len(result["skipped_fights"]) == 2


def test_explicit_event_cli_selects_remaining_card_and_not_next_card(schedule_db, monkeypatch, capsys, report_dir):
    card = load_next_card(schedule_db[0])
    card.update(live_remaining=True, completed_fights=2, excluded_fights=[])
    requests = []
    def load(event, **kwargs):
        requests.append(event)
        return card
    monkeypatch.setattr(cli, "load_remaining_card", load)
    monkeypatch.setattr(cli, "load_next_card", lambda *a: pytest.fail("Must not use the saved next card"))
    agent = FakeAgent()
    monkeypatch.setattr(cli, "SQLAnalysisAgent", lambda *a, **kw: agent)
    assert cli.main(["--event", "Allen vs Duncan"]) == 0
    output = capsys.readouterr().out
    assert "Skipped 2 fights with posted results" in output
    assert len(agent.calls) == 2 and requests[0] == "Allen vs Duncan"
    assert requests[1:] == [card["event_url"]] * 4
    card["fights"] = []
    monkeypatch.setattr(cli, "SQLAnalysisAgent", lambda *a, **kw: pytest.fail("No model call for finished card"))
    assert cli.main(["--event", "Allen vs Duncan"]) == 0
    assert "no remaining fights" in capsys.readouterr().out
