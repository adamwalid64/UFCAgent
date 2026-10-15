from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from pipeline.db import init_db
from pipeline.models import Event, Fight, Fighter, FightRound
from sql_agent.analytics import MatchupTools, duration_minutes, metrics
from sql_agent.database import ROOT, QueryLimitError, QuerySession
from sql_agent.graph import Action, PredictionDraft, SQLAnalysisAgent
from sql_agent.compact import pack_evidence, unpack_evidence
from sql_agent.evidence import build_matchup_evidence
from sql_agent.evaluation import PredictionReview, ReviewChecks, PredictionReviewError, validate_review_claims, review_evidence_view
from sql_agent.reports import PredictionReport


CUTOFF = "2021-01-01"
NAMES = {"a": "Alpha", "b": "Beta", "c": "Gamma", "d": "Delta", "e": "Debut"}


def add_bout(session, fight_id, day, a, b, *, result="completed", method="Decision - Unanimous",
             winner=None, volume=20):
    event_id = "event-" + fight_id
    session.add(Event(event_id=event_id, event_name=event_id, event_date=date.fromisoformat(day)))
    session.flush()
    session.add(Fight(
        fight_id=fight_id, event_id=event_id, card_order=1,
        fighter_a_id=a, fighter_a_name=NAMES[a], fighter_b_id=b, fighter_b_name=NAMES[b],
        winner_fighter_id=winner or a, winner_name=NAMES[winner or a], result=result,
        method_of_victory=method, ending_round=3, ending_time="5:00", time_format="3 Rnd (5-5-5)",
        stats_available=True,
        fighter_a_significant_strikes_landed=volume, fighter_a_significant_strikes_attempted=40,
        fighter_b_significant_strikes_landed=10, fighter_b_significant_strikes_attempted=40,
        fighter_a_takedowns_landed=2, fighter_a_takedowns_attempted=4,
        fighter_b_takedowns_landed=1, fighter_b_takedowns_attempted=5,
        fighter_a_submission_attempts=1, fighter_b_submission_attempts=0,
    ))
    session.flush()
    session.add(FightRound(fight_id=fight_id, fighter_id=a, opponent_id=b, round_number=1,
                          significant_strikes_landed=volume, significant_strikes_attempted=40))


@pytest.fixture
def toy_db(tmp_path):
    """Multigraph: A->C->B (two A/C bouts), A->B, B->D->A, plus draw and NC."""
    path = tmp_path / "toy.db"
    engine = init_db("sqlite:///" + path.as_posix())
    with Session(engine) as session:
        session.add_all(Fighter(fighter_id=fid, fighter_name=name, date_of_birth=date(1990, 1, 1),
                                wins=999, significant_strikes_landed=999999)
                        for fid, name in NAMES.items())
        session.flush()
        add_bout(session, "older-ac", "2019-12-01", "a", "c", method="KO/TKO")
        add_bout(session, "ac", "2020-01-01", "a", "c", method="KO/TKO")
        add_bout(session, "cb", "2020-02-01", "c", "b")
        add_bout(session, "bd", "2020-03-01", "b", "d", method="Submission")
        add_bout(session, "da", "2020-04-01", "d", "a")
        add_bout(session, "ab", "2020-05-01", "a", "b")
        add_bout(session, "draw-cd", "2020-06-01", "c", "d", result="draw")
        add_bout(session, "nc-ba", "2020-07-01", "b", "a", result="no_contest", method="Overturned")
        session.commit()
    engine.dispose()
    return path


@pytest.mark.parametrize("sql", [
    "INSERT INTO fighters(fighter_id) VALUES ('bad')", "UPDATE fighters SET fighter_name='bad'",
    "DELETE FROM fights", "DROP TABLE fights", "ALTER TABLE fights ADD COLUMN bad TEXT",
    "ATTACH DATABASE ':memory:' AS escape", "PRAGMA query_only=OFF", "PRAGMA writable_schema=ON",
    "WITH x AS (SELECT 1) DELETE FROM fights", "SELECT 1; DROP TABLE fights",
    "/* SELECT */ VACUUM", "REPLACE INTO fighters VALUES ('bad')",
    "SELECT load_extension('evil')", "SELECT writefile('evil', 'x')",
    "SELECT * FROM pragma_table_info('fighters')", "SELECT * FROM sqlite_master",
    "SELECT * FROM temp.fights", "SELECT * FROM source.fights",
])
def test_read_only_guards_reject_writes_and_escape(toy_db, sql):
    digest = hashlib.sha256(toy_db.read_bytes()).hexdigest()
    with QuerySession(toy_db, as_of_date=CUTOFF) as session:
        result = session.run_sql(sql, "Attempt forbidden access")
        assert "error" in result
        assert session.attempts == 1
        assert session.queries_run[0]["error"]
        assert session.run_sql("SELECT COUNT(*) AS n FROM fights")["rows"] == [{"n": 8}]
        with pytest.raises(sqlite3.DatabaseError):
            session._connection.execute("DELETE FROM fights")
    assert hashlib.sha256(toy_db.read_bytes()).hexdigest() == digest


def test_source_connection_is_opened_readonly(toy_db, monkeypatch):
    original = sqlite3.connect
    calls = []

    def connect(database, *args, **kwargs):
        calls.append((database, kwargs))
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with QuerySession(toy_db) as session:
        assert session.run_sql("SELECT 1 AS ok")["rows"][0]["ok"] == 1
    files = [(path, options) for path, options in calls if path != ":memory:"]
    assert len(files) == 1 and files[0][0].endswith("?mode=ro") and files[0][1]["uri"]


def test_sql_limits_timeout_and_error_budget(toy_db):
    with QuerySession(toy_db, row_limit=2, timeout=0.01, max_queries=4) as session:
        result = session.run_sql("WITH x AS (SELECT * FROM fights) SELECT * FROM x")
        assert len(result["rows"]) == 2 and result["truncated"]
        assert "error" in session.run_sql("SELECT nonexistent FROM fights")
        result = session.run_sql("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n")
        assert "interrupt" in result["error"].lower()
        assert session.run_sql("SELECT 'DROP DELETE' AS text /* UPDATE */")["rows"][0]["text"] == "DROP DELETE"
        assert session.remaining == 0
        with pytest.raises(QueryLimitError):
            session.run_sql("SELECT 1")
        assert len(session.queries_run) == 4


def test_mma_math_multigraph_counts_weights_and_conflict(toy_db):
    # A small arbitrary-SQL row cap must not truncate the helper's evidence.
    with QuerySession(toy_db, as_of_date=CUTOFF, row_limit=1) as session:
        tools = MatchupTools(session)
        result = tools.get_mma_math_paths("Alpha", "Beta")
        assert result["a_to_b"]["path_count"] == 3
        assert result["b_to_a"]["path_count"] == 1
        assert result["conflicting_evidence"] is True
        paths = result["a_to_b"]["strongest_paths"]
        assert {tuple(p["fighter_ids"]) for p in paths} == {("a", "b"), ("a", "c", "b")}
        assert paths[0]["fighter_ids"] == ["a", "b"]
        recent = next(p for p in paths if p["fights"][0]["fight_id"] == "ac")
        older = next(p for p in paths if p["fights"][0]["fight_id"] == "older-ac")
        assert recent["weight"] > older["weight"]
        expected = 1.1 * 2 ** (-366 / 1095.75) * 2 ** (-335 / 1095.75) / 4
        assert recent["weight"] == pytest.approx(expected)
        one = tools.get_mma_math_paths("a", "b", max_depth=1)
        assert one["a_to_b"]["path_count"] == 1 and one["b_to_a"]["path_count"] == 0
        all_ids = {e["fight_id"] for direction in ("a_to_b", "b_to_a")
                   for p in result[direction]["strongest_paths"] for e in p["fights"]}
        assert not all_ids & {"draw-cd", "nc-ba"}


def helper_outputs(path):
    with QuerySession(path, as_of_date=CUTOFF) as session:
        tools = MatchupTools(session)
        return {
            "schema": session.describe_schema(),
            "history": tools.get_fighter_history("Alpha"),
            "career": tools.get_career_stats("Alpha"),
            "common": tools.get_common_opponents("Alpha", "Beta"),
            "math": tools.get_mma_math_paths("Alpha", "Beta"),
            "opposition": tools.get_opponent_quality("Alpha"),
            "style": tools.get_style_matchup("Alpha", "Beta"),
            "mandatory": build_matchup_evidence(session.history_rows(None, "Test mandatory evidence"),
                                                 session.resolve_fighter("a"), session.resolve_fighter("b"), CUTOFF),
        }


def test_point_in_time_all_helpers_and_arbitrary_sql_ignore_future(toy_db):
    before = helper_outputs(toy_db)
    engine = init_db("sqlite:///" + toy_db.as_posix())
    with Session(engine) as session:
        add_bout(session, "on-cutoff", CUTOFF, "a", "e", volume=900000)
        add_bout(session, "future", "2022-01-01", "b", "e", volume=800000)
        session.get(Fighter, "a").wins = 100000
        session.commit()
    engine.dispose()
    after = helper_outputs(toy_db)
    assert before == after
    assert "on-cutoff" not in json.dumps(after) and '"future"' not in json.dumps(after)
    with QuerySession(toy_db, as_of_date=CUTOFF) as session:
        for table in ("fights", "fight_rounds", "fighter_bout_history"):
            rows = session.run_sql(f"WITH x AS (SELECT * FROM main.{table}) SELECT * FROM x")["rows"]
            assert rows and all(r["fight_id"] not in {"on-cutoff", "future"} for r in rows)
        assert session.run_sql("SELECT MAX(event_date) AS latest FROM events")["rows"] == [{"latest": "2020-07-01"}]
        assert "error" in session.run_sql("SELECT wins FROM fighters")
        with pytest.raises(ValueError, match="cutoff"):
            MatchupTools(session).get_fighter_history("a", "2025-01-01")


def test_metrics_denominators_missing_values_and_unusual_rounds(toy_db):
    with QuerySession(toy_db, as_of_date=CUTOFF) as session:
        stats = MatchupTools(session).get_career_stats("Alpha")
        assert stats["career"]["record"] == {"win": 3, "loss": 1, "draw": 0, "no_contest": 1}
        assert stats["career"]["takedown_defense"] == pytest.approx(1 - 7 / 23)
        assert stats["career"]["finish_rate_among_wins"] == pytest.approx(2 / 3)
        assert stats["days_since_last_fight"] == 184
        assert stats["age"] == 31
        assert stats["win_streak"] == 0
        history = MatchupTools(session).get_fighter_history("Alpha")["fights"]
        good = history[0]
        missing = dict(good, bout_stats_available=0, bout_significant_strikes_landed=99999)
        assert metrics([good, missing])["sig_strikes_landed_per_min"] == pytest.approx(20 / 15)
        assert duration_minutes(dict(good, bout_time_format="1 Rnd + OT (12-3)",
                                     bout_ending_round=2, bout_ending_time="2:30")) == 14.5
        assert duration_minutes(dict(good, bout_time_format=None)) is None
        debut = MatchupTools(session).get_career_stats("Debut")
        assert debut["career"]["fights"] == 0 and debut["career"]["striking_accuracy"] is None


def test_opponent_quality_is_measured_before_each_fight(toy_db):
    with QuerySession(toy_db, as_of_date=CUTOFF) as session:
        result = MatchupTools(session).get_opponent_quality("Alpha")
        first, second = result["opponents"][:2]
        assert first["quality"] is None and first["tier"] == "unknown"
        assert second["opponent_prior_record"]["loss"] == 1
        assert second["quality"] == pytest.approx(1 / 3)
        assert result["ranking"] is None and result["elo"] is None
        assert result["best_win"]["result"] == "win"
        assert result["opponents"][2]["opponent_prior_record"]["loss"] == 1


def test_opponent_quality_excludes_other_fights_on_same_day(toy_db):
    engine = init_db("sqlite:///" + toy_db.as_posix())
    with Session(engine) as session:
        add_bout(session, "same-day-debut", "2020-02-01", "e", "c")
        session.commit()
    engine.dispose()
    with QuerySession(toy_db, as_of_date=CUTOFF) as session:
        result = MatchupTools(session).get_opponent_quality("Debut")
        record = result["opponents"][0]["opponent_prior_record"]
        assert record == {"win": 0, "loss": 2, "draw": 0, "no_contest": 0}


def test_style_percentiles_and_binary_sql_are_json_safe(toy_db):
    with QuerySession(toy_db, as_of_date=CUTOFF) as session:
        result = MatchupTools(session).get_style_matchup("a", "b")
        assert result["roster_size"] == 4  # The profile-only debut does not affect percentiles.
        assert result["a_offense_vs_b"]["submissions"]["opponent_submission_defense"] is None
        assert "counter striker" not in result["a"]["tags"]
        for rank in result["a"]["percentiles"].values():
            assert rank is None or 0 <= rank <= 1
        raw = session.run_sql("SELECT CAST('abc' AS BLOB) AS blob, 1e999 AS overflow")
        assert raw["rows"] == [{"blob": {"hex": "616263"}, "overflow": None}]
        json.dumps(raw, allow_nan=False)


def evidence_for(path, a="a", b="b"):
    with QuerySession(path, as_of_date=CUTOFF) as session:
        return build_matchup_evidence(session.history_rows(None, "Test historical replay"),
                                      session.resolve_fighter(a), session.resolve_fighter(b), CUTOFF)


def test_historical_opposition_and_style_do_not_use_same_day_or_later_form(toy_db):
    before = evidence_for(toy_db, "a", "c")
    engine = init_db("sqlite:///" + toy_db.as_posix())
    with Session(engine) as session:
        add_bout(session, "target-ac", "2020-08-01", "a", "c")
        add_bout(session, "same-day-cd", "2020-08-01", "c", "d", volume=100)
        session.commit()
    initial = evidence_for(toy_db)
    target = next(b for b in initial["a"]["bouts"] if b["fight_id"] == "target-ac")
    assert target["opponent_prior_record"] == before["b"]["career"]["record"]
    assert target["opponent_prior_elo"] == before["b"]["current_derived_elo"]
    assert target["opponent_style_before_bout"]["prior_stat_fights"] == 4
    assert target["opponent_style_before_bout"]["status"] == "measured proxy"
    with Session(engine) as session:
        add_bout(session, "later-cd", "2020-09-01", "c", "d", volume=200)
        session.commit()
    engine.dispose()
    after = evidence_for(toy_db)
    assert initial["a"]["bouts"] == after["a"]["bouts"]
    assert initial["a"]["history_by_opponent_style"] == after["a"]["history_by_opponent_style"]
    first = after["a"]["bouts"][0]
    assert first["opponent_tier"] == "unknown" and first["strength_provisional"]
    assert first["opponent_style_before_bout"]["tags"] == []
    assert first["opponent_style_before_bout"]["status"] == "insufficient history"


def test_elo_rewards_stronger_opposition_without_finish_bonus_and_ignores_nc(toy_db):
    with QuerySession(toy_db, as_of_date=CUTOFF) as session:
        templates = session.history_rows(None, "Replay templates")[:2]
        a, b = session.resolve_fighter("a"), session.resolve_fighter("b")

    def bout(fid, day, winner, loser, method="Decision - Unanimous"):
        return [dict(templates[0], fight_id=fid, event_date=day, fighter_id=winner, opponent_id=loser,
                     opponent_name=NAMES[loser], bout_outcome="win", bout_method_of_victory=method),
                dict(templates[1], fight_id=fid, event_date=day, fighter_id=loser, opponent_id=winner,
                     opponent_name=NAMES[winner], bout_outcome="loss", bout_method_of_victory=method)]

    history = sum([bout(str(n), f"2020-0{n}-01", "c", "d") for n in range(1, 4)], [])
    history += bout("strong-decision", "2020-04-01", "a", "c")
    history += bout("weak-ko", "2020-04-01", "b", "d", "KO/TKO")
    context = build_matchup_evidence(history, a, b, CUTOFF)
    assert context["a"]["current_derived_elo"] > context["b"]["current_derived_elo"] > 1500
    assert context["a"]["bouts"][0]["opponent_tier"] == "higher"
    assert context["b"]["bouts"][0]["opponent_tier"] == "lower"
    alternate = [dict(row, bout_method_of_victory="Submission") for row in history]
    alternate += [dict(row, bout_outcome="no_contest") for row in bout("nc", "2020-05-01", "a", "b")]
    other = build_matchup_evidence(alternate, a, b, CUTOFF)
    for side in ("a", "b"):
        assert context[side]["current_derived_elo"] == other[side]["current_derived_elo"]
    with pytest.raises(ValueError, match="cutoff"):
        build_matchup_evidence(history + bout("future", CUTOFF, "a", "b"), a, b, CUTOFF)


def test_debut_evidence_keeps_absent_opposition_and_styles_unknown(toy_db):
    context = evidence_for(toy_db, "e", "b")["a"]
    assert context["bouts"] == [] and context["career"]["fights"] == 0
    assert context["opposition"]["average_opponent_prior_elo"] is None
    assert context["current_style"]["status"] == "insufficient history"
    assert context["similar_to_next_opponent"] == []


def test_parameterized_identity_and_default_date(toy_db):
    with QuerySession(toy_db) as session:
        assert session.as_of_date == date.today().isoformat()
        with pytest.raises(ValueError):
            MatchupTools(session).get_fighter_history("Alpha' OR 1=1 --")
        MatchupTools(session).get_fighter_history("Alpha")
        log = session.queries_run[-1]
        assert ":fighter_0" in log["sql"] and "Alpha" not in log["sql"]
        assert log["parameters"]["fighter_0"] == "a"


def draft(winner="Alpha", evidence=None):
    assessments = {side: {"analysis": f"Evidence assessment for {side}; limited proxy data.",
                          "fight_ids": [evidence[side]["bouts"][0]["fight_id"]] if evidence and evidence[side]["bouts"] else []}
                   for side in ("a", "b")}
    return {
        "predicted_winner": winner, "win_probability": 0.6, "predicted_method": "Decision", "predicted_round": 3,
        "key_factors": ["Scripted test response; no predictive claim."],
        "dimension_findings": {key: "Scripted test finding." for key in
                               ("form", "stats", "opposition", "common_opponents", "mma_math", "style", "activity_and_age")},
        "caveats": ["Scripted model for repeatable integration testing."],
        "summary": "The graph and real SQL evidence were exercised with a scripted model.",
        "opponent_quality_analysis": assessments, "style_history_analysis": assessments,
        "counterargument": "The other fighter has a plausible route; evidence is limited.",
        "confidence_justification": "Confidence reflects limited historical data and competing evidence.",
    }


def scripted_claims(packet):
    return [{"fighter_side": side, "category": category, "claim": "Scripted source check",
             "evidence_path": f"{side}.{path}", "evidence_value": json.dumps(value), "supported": True}
            for side in ("a", "b") for category, path, value in (
                ("opposition", "opposition.provisional_opponents", packet[side]["opposition"]["provisional_opponents"]),
                ("style_history", "history_by_opponent_style.high-volume striker.fights",
                 (packet[side]["history_by_opponent_style"]["high-volume striker"] if isinstance(packet[side]["history_by_opponent_style"], dict)
                  else packet[side]["history_by_opponent_style"][0])["fights"]))]


class ScriptedModel:
    """Injects model decisions only; graph, SQL, tools and validation all run normally."""
    def __init__(self, actions, winner="Alpha", *, reviews=None, draft_changes=None):
        self.actions, self.winner = iter(actions), winner
        self.calls = []
        self.plan_count = 0
        self.reviews = iter(reviews) if reviews is not None else None
        self.draft_changes = iter(draft_changes) if draft_changes is not None else None
        self.review_count = 0

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class Runner:
            def invoke(self, messages):
                parent.calls.append(list(messages))
                if schema is Action:
                    parent.plan_count += 1
                    return Action.model_validate(next(parent.actions))
                if schema is PredictionReview:
                    parent.review_count += 1
                    payload = next(parent.reviews) if parent.reviews is not None else {
                        "checks": {name: True for name in ReviewChecks.model_fields}, "issues": [],
                        "cautions": [], "summary": "Scripted evidence review approved."}
                    if "checks" in payload and "claim_checks" not in payload:
                        packet = json.loads(messages[1].content)["mandatory_evidence"]
                        payload = {**payload, "claim_checks": scripted_claims(packet)}
                    return PredictionReview.model_validate(payload)
                evidence = next(json.loads(m.content)["mandatory_evidence"] for m in messages
                                if m.type == "human" and "mandatory_evidence" in json.loads(m.content))
                result = draft(parent.winner, unpack_evidence(evidence))
                if parent.draft_changes is not None:
                    result.update(next(parent.draft_changes))
                return PredictionDraft.model_validate(result)

        return Runner()


def test_youtube_context_stays_private_in_predictions_reports_and_ledgers(toy_db, tmp_path):
    from fight_coverage import FightCoverage
    from sql_agent.reports import PredictionReport
    from types import SimpleNamespace
    phrase = "Unique private transcript content about these athletes must remain inside analysis only."
    coverage = FightCoverage(sources=[dict(video_id="abcdefghijk", channel="Test analyst",
        url="https://www.youtube.com/watch?v=abcdefghijk", layer="mcp", cache_hit=True)],
        caveats=["Third-party opinion is not independent statistical evidence."],
        _analysis=[{"segments": [{"text": phrase}]}])
    coverage.protect([{"text": phrase}])
    calls, initial = [], []
    def fetch(fight, event_date):
        calls.append((fight, event_date))
        return coverage
    model = ScriptedModel([])
    result = SQLAnalysisAgent(toy_db, llm=model, research_mode="off",
        coverage_provider=SimpleNamespace(get_fight_coverage=fetch)).predict(
            "Alpha", "Beta", CUTOFF, event_name="UFC 500", on_initial_prediction=initial.append)
    assert calls[0][0]["event_name"] == "UFC 500" and calls[0][1] == CUTOFF
    # A one-call direct run preserves the prediction budget and skips extraction;
    # raw analyst text must not bypass the sentiment stage into the SQL model.
    assert phrase not in str([m.content for m in model.calls[0]])
    assert phrase not in json.dumps(result) and phrase not in json.dumps(initial)
    assert result["external_coverage"]["sources"][0]["layer"] == "mcp"
    report = PredictionReport(tmp_path, "Private source test", CUTOFF)
    report.add_prediction(result)
    saved = report.finish(result, save_json=True)
    for path in (report.path, report.ledger_path, saved):
        assert phrase not in path.read_text(encoding="utf-8")
        assert "youtube.com/watch?v=abcdefghijk" in path.read_text(encoding="utf-8")


def test_copied_youtube_passage_is_repaired_before_publication(toy_db):
    from fight_coverage import FightCoverage
    from types import SimpleNamespace
    phrase = "This particular private commentary is deliberately copied by the first invalid model draft."
    coverage = FightCoverage(_analysis=[{"text": phrase}])
    coverage.protect([{"text": phrase}])
    model = ScriptedModel([], draft_changes=[{"summary": phrase}, {"summary": "Independent original explanation."}])
    result = SQLAnalysisAgent(toy_db, llm=model, research_mode="off", max_model_calls=3,
        coverage_provider=SimpleNamespace(get_fight_coverage=lambda *args: coverage)).predict("Alpha", "Beta", CUTOFF)
    assert result["summary"] == "Independent original explanation."
    assert result["evaluation"]["revision_count"] == 1
    assert phrase not in json.dumps(result)


def test_sentiment_extraction_and_central_market_node_are_isolated(toy_db, tmp_path):
    from fight_coverage import FightCoverage
    from fight_coverage.signals import ExtractionBatch
    from types import SimpleNamespace
    from datetime import datetime, timedelta
    from sql_agent.reports import format_prediction
    phrase = "Unique private raw source discussion of footwork and grappling for the upcoming contest."
    source = dict(video_id="abcdefghijk", content_id="abcdefghijk", source_id="analyst", channel="Analyst",
        url="https://www.youtube.com/watch?v=abcdefghijk", layer="mcp", cache_hit=True,
        type="technical breakdown", credibility_weight=1.0)
    coverage = FightCoverage(sources=[source], _analysis=[{"content_id": "abcdefghijk", "segments": [{"text": phrase}]}])
    coverage.protect([{"text": phrase}])
    class SourceModel(ScriptedModel):
        def with_structured_output(self, schema, **kwargs):
            if schema is ExtractionBatch:
                def invoke(messages):
                    self.calls.append(list(messages))
                    return ExtractionBatch.model_validate({"sources": [{"content_id": "abcdefghijk", "pick": None,
                        "pick_evidence": None, "signals": [{"fighter": "Alpha", "kind": "matchup",
                            "analysis": "The analyst discusses how range control interacts with wrestling entries.",
                            "evidence_segments": [0]}]}]})
                return SimpleNamespace(invoke=invoke)
            return super().with_structured_output(schema, **kwargs)
    model = SourceModel([])
    observed = (datetime.fromisoformat(CUTOFF) - timedelta(days=1)).isoformat() + "Z"
    market = tmp_path / "market.json"
    market.write_text(json.dumps({"fights": [{"fighter_a": "Alpha", "fighter_b": "Beta", "event_date": CUTOFF,
        "odds": {"bookmaker": "PRIVATE_MARKET_INPUT", "observed_at": observed, "moneyline": {"Alpha": -150, "Beta": 130}},
        "public_sentiment": {"source": "PRIVATE_PUBLIC_POLL_INPUT", "observed_at": observed, "percentages": {"Alpha": 90, "Beta": 10}}}]}))
    result = SQLAnalysisAgent(toy_db, llm=model, research_mode="off", max_model_calls=3,
        coverage_provider=SimpleNamespace(get_fight_coverage=lambda *args: coverage), market_context_path=market).predict("Alpha", "Beta", CUTOFF)
    assert phrase in str([m.content for m in model.calls[0]])
    assert phrase not in str([m.content for call in model.calls[1:] for m in call])
    assert "PRIVATE_MARKET_INPUT" not in str(model.calls) and "PRIVATE_PUBLIC_POLL_INPUT" not in str(model.calls)
    assert result["external_coverage"]["signals"][0]["pick"] is None
    assert result["market_context"]["public_sentiment"]["source"] == "PRIVATE_PUBLIC_POLL_INPUT"
    assert result["model_usage"]["request_count"] == 2
    report = format_prediction(result)
    assert "pick=null" in report and "Central market comparison" in report and phrase not in report


def review_verdict(*, reject=False):
    checks = {name: True for name in ReviewChecks.model_fields}
    checks["confidence_justified"] = not reject
    return {"checks": checks, "issues": ["Reduce confidence: sparse style evidence."] if reject else [],
            "cautions": [] if reject else ["Confidence remains uncalibrated."],
            "summary": "Confidence needs revision." if reject else "Evidence and explanation agree."}


def test_final_review_revises_then_publishes_only_approved_result(toy_db, tmp_path):
    model = ScriptedModel([], reviews=[review_verdict(reject=True), review_verdict()],
                          draft_changes=[{"win_probability": 0.9}, {"win_probability": 0.6}])
    result = SQLAnalysisAgent(toy_db, llm=model, max_queries=1, review_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert model.plan_count == 0 and model.review_count == 2
    assert result["win_probability"] == 0.6 and len(result["queries_run"]) == 1
    evaluation = result["evaluation"]
    assert evaluation["status"] == "approved" and evaluation["revision_count"] == 1
    assert [r["approved"] for r in evaluation["history"]] == [False, True]
    assert "Confidence remains uncalibrated." in result["caveats"]
    review_calls = [call for call in model.calls if json.loads(call[-1].content).get("phase") == "review"]
    assert all(len(call) == 2 for call in review_calls)  # Fresh reviewer context, no writer conversation.
    assert review_calls[0][0].content != model.calls[0][0].content
    report = PredictionReport(tmp_path, "Alpha vs Beta", CUTOFF)
    report.add_prediction(result)
    audit_path = report.finish(result, save_json=True)
    saved = report.path.read_text(encoding="utf-8")
    assert "Confidence: 60.0%" in saved and "Confidence: 90.0%" not in saved
    assert "Level of opposition:" in saved and "History against relevant styles:" in saved
    assert "Case for the other fighter:" in saved and "Cited historical evidence:" in saved
    assert "opponent entering" in saved and "Final evaluation: approved after 2 review(s), 1 revision(s)" in saved
    assert json.loads(audit_path.read_text(encoding="utf-8"))["evaluation"] == evaluation


@pytest.mark.parametrize("revisions", [0, 2])
def test_failed_review_is_bounded_and_withholds_prediction(toy_db, revisions):
    model = ScriptedModel([], reviews=[review_verdict(reject=True)] * (revisions + 1))
    with pytest.raises(PredictionReviewError, match="Prediction withheld"):
        SQLAnalysisAgent(toy_db, llm=model, max_queries=1, max_revisions=revisions, review_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert model.review_count == revisions + 1


def test_deterministic_checks_cannot_be_overruled_by_reviewer(toy_db):
    bad_citations = {side: {"analysis": "Unsupported example", "fight_ids": ["future-or-invented"]}
                     for side in ("a", "b")}
    model = ScriptedModel([], draft_changes=[{"opponent_quality_analysis": bad_citations}],
                          reviews=[review_verdict()])
    with pytest.raises(PredictionReviewError, match="unavailable or wrong-fighter"):
        SQLAnalysisAgent(toy_db, llm=model, max_queries=1, max_revisions=0, review_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert model.review_count == 1


def test_reviewer_cannot_fabricate_its_supporting_evidence(toy_db):
    verdict = review_verdict()
    verdict["claim_checks"] = scripted_claims(evidence_for(toy_db))
    for check in verdict["claim_checks"]:
        check["evidence_value"] = '"invented supporting evidence"'
    model = ScriptedModel([], reviews=[verdict, verdict])
    with pytest.raises(PredictionReviewError, match="Reviewer misquoted evidence"):
        SQLAnalysisAgent(toy_db, llm=model, max_queries=1, max_revisions=0, review_mode="on").predict("Alpha", "Beta", CUTOFF)
    recoverable = ScriptedModel([], reviews=[verdict, review_verdict()])
    result = SQLAnalysisAgent(toy_db, llm=recoverable, max_queries=1, review_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert recoverable.review_count == 2 and result["evaluation"]["revision_count"] == 0
    assert "actual source value" in recoverable.calls[-1][-1].content


def test_review_sources_use_stable_fight_ids_instead_of_list_positions(toy_db):
    packet = evidence_for(toy_db)
    verdict = review_verdict()
    verdict["claim_checks"] = scripted_claims(packet) + [{
        "fighter_side": "a", "category": "other", "claim": "Gamma entered at the initial rating",
        "evidence_path": "a.bouts.older-ac.opponent_prior_elo", "evidence_value": "1500.0", "supported": True}]
    view = review_evidence_view(packet)
    assert view["a"]["bouts"]["older-ac"]["opponent_name"] == "Gamma"
    packet["a"]["bouts"].reverse()
    assert validate_review_claims(PredictionReview.model_validate(verdict), packet) == []


def test_claim_audit_rejects_invalid_paths_and_unsupported_claims(toy_db):
    packet = evidence_for(toy_db)
    verdict = review_verdict()
    verdict["claim_checks"] = scripted_claims(packet)
    assert validate_review_claims(PredictionReview.model_validate(verdict), packet) == []
    verdict["claim_checks"][0]["evidence_path"] = "a.opposition.missing"
    verdict["claim_checks"][1]["evidence_path"] = "a.history_by_opponent_style.-1.record"
    verdict["claim_checks"][2]["supported"] = False
    issues = validate_review_claims(PredictionReview.model_validate(verdict), packet)
    assert sum("invalid evidence path" in issue for issue in issues) == 2
    assert any("Unsupported claim" in issue for issue in issues)


def test_review_accepts_exact_plain_strings_and_wrapped_paths_without_accepting_wrong_values(toy_db):
    packet = evidence_for(toy_db)
    verdict = review_verdict()
    verdict["claim_checks"] = scripted_claims(packet)
    verdict["claim_checks"][0].update(evidence_path="mandatory_evidence.a.bouts.older-ac.result", evidence_value="win")
    assert validate_review_claims(PredictionReview.model_validate(verdict), packet) == []
    verdict["claim_checks"][0]["evidence_value"] = '"loss"'
    issues = validate_review_claims(PredictionReview.model_validate(verdict), packet)
    assert any("misquoted evidence" in issue and '"win"' in issue for issue in issues)


def test_empty_historical_collection_uses_same_shape_as_reviewer_view(toy_db):
    packet = evidence_for(toy_db)
    packet["a"]["similar_to_next_opponent"] = []
    verdict = review_verdict()
    verdict["claim_checks"] = scripted_claims(packet)
    verdict["claim_checks"][1].update(evidence_path="a.similar_to_next_opponent", evidence_value="{}")
    assert validate_review_claims(PredictionReview.model_validate(verdict), packet) == []


def test_no_self_review_still_requires_deterministic_checks_and_is_labeled(toy_db, tmp_path):
    model = ScriptedModel([])
    result = SQLAnalysisAgent(toy_db, llm=model, max_queries=1, review_mode="off").predict("Alpha", "Beta", CUTOFF)
    assert model.review_count == 0 and len(model.calls) == 1
    assert result["evaluation"]["status"] == "validated" and "checks" not in result["evaluation"]
    report = PredictionReport(tmp_path, "Alpha vs Beta", CUTOFF)
    report.add_prediction(result)
    assert "no LLM self-review" in report.path.read_text()
    bad = ScriptedModel([], winner="Gamma")
    with pytest.raises(PredictionReviewError, match="outside"):
        SQLAnalysisAgent(toy_db, llm=bad, max_queries=1, max_revisions=0, review_mode="off").predict("Alpha", "Beta", CUTOFF)
    assert bad.review_count == 0


def test_opt_in_direct_prediction_uses_one_call_with_complete_compact_evidence(toy_db):
    model = ScriptedModel([])
    result = SQLAnalysisAgent(toy_db, llm=model, research_mode="off").predict("Alpha", "Beta", CUTOFF)
    assert len(model.calls) == 1 and model.plan_count == model.review_count == 0
    assert result["model_usage"]["request_count"] == 1
    supplied = json.loads(model.calls[0][2].content)
    assert unpack_evidence(supplied["mandatory_evidence"]) == result["matchup_evidence"]
    observations = supplied["supplemental_observations"]
    assert observations[0]["result"]["common_opponents"]
    assert observations[1]["result"]["a_to_b"]["path_count"] > 0
    assert result["evaluation"]["mode"] == "deterministic"


def test_compact_evidence_round_trip_keeps_every_bout_value_and_unknown(toy_db):
    packet = evidence_for(toy_db)
    before = json.dumps(packet)
    encoded = pack_evidence(packet)
    assert unpack_evidence(json.loads(json.dumps(encoded))) == packet
    assert json.dumps(packet) == before
    assert len(json.dumps(encoded, separators=(",", ":"))) < len(before) * .75
    debut = evidence_for(toy_db, "e", "b")
    assert unpack_evidence(pack_evidence(debut)) == debut


def test_explicit_one_call_budget_does_not_spend_on_a_second_attempt(toy_db):
    model = ScriptedModel([], draft_changes=[{"predicted_winner": "Gamma"}])
    with pytest.raises(PredictionReviewError, match="outside this matchup"):
        SQLAnalysisAgent(toy_db, llm=model, research_mode="off", max_model_calls=1).predict("Alpha", "Beta", CUTOFF)
    assert len(model.calls) == 1


def test_research_reserves_one_request_for_drafting(toy_db):
    model = ScriptedModel([{"action": "describe_schema", "purpose": "Explore"}] * 5)
    result = SQLAnalysisAgent(toy_db, llm=model, research_mode="on", max_model_calls=2).predict("Alpha", "Beta", CUTOFF)
    assert model.plan_count == 1 and len(model.calls) == 2
    assert result["predicted_winner"] == "Alpha"


def test_default_draft_can_repair_invalid_citations_without_publishing_them(toy_db):
    bad = {side: {"analysis": "Incorrect citation", "fight_ids": ["future-or-invented"]} for side in ("a", "b")}
    model = ScriptedModel([], draft_changes=[{"opponent_quality_analysis": bad}, {}])
    result = SQLAnalysisAgent(toy_db, llm=model, max_queries=1).predict("Alpha", "Beta", CUTOFF)
    assert len(model.calls) == 2 and result["evaluation"]["revision_count"] == 1
    assert "future-or-invented" not in json.dumps(result["opponent_quality_analysis"])
    feedback = [json.loads(m.content) for m in model.calls[-1] if m.type == "human"]
    assert any("required_corrections" in item for item in feedback)


def test_default_research_retains_normal_field_names_and_references_duplicate_evidence(toy_db):
    actions = [{"action": "get_opponent_quality", "fighter": "Alpha", "purpose": "Inspect opposition"},
               {"action": "finish", "purpose": "Enough evidence"}]
    model = ScriptedModel(actions)
    result = SQLAnalysisAgent(toy_db, llm=model).predict("Alpha", "Beta", CUTOFF)
    assert model.plan_count == 2 and model.review_count == 0
    messages = [json.loads(m.content) for m in model.calls[-1] if m.type == "human"]
    packets = [m["mandatory_evidence"] for m in messages if "mandatory_evidence" in m]
    assert packets == [result["matchup_evidence"]] and "encoding" not in packets[0]
    observation = next(m["result"] for m in messages if m.get("operation", {}).get("action") == "get_opponent_quality")
    assert observation["evidence_references"][0]["path"] == "mandatory_evidence.a"
    assert "fighters" not in observation
    synthesis = messages[-1]
    assert "matchup_focus_references" in synthesis and "output_style" not in synthesis


def test_research_budget_reserves_room_for_validation_repairs(toy_db):
    model = ScriptedModel([{"action": "describe_schema", "purpose": "Explore"}] * 10,
                          draft_changes=[{"predicted_winner": "Gamma"}, {}])
    result = SQLAnalysisAgent(toy_db, llm=model, max_model_calls=8).predict("Alpha", "Beta", CUTOFF)
    assert model.plan_count == 4 and len(model.calls) == 6
    assert result["evaluation"]["revision_count"] == 1


def test_shared_initial_prediction_is_frozen_before_review_changes(toy_db):
    model = ScriptedModel([], reviews=[review_verdict(reject=True), review_verdict()],
                          draft_changes=[{"win_probability": .9}, {"win_probability": .6}])
    initial = []
    result = SQLAnalysisAgent(toy_db, llm=model, max_queries=1, review_mode="on").predict(
        "Alpha", "Beta", CUTOFF, on_initial_prediction=initial.append)
    assert len(initial) == 1 and initial[0]["win_probability"] == .9 and result["win_probability"] == .6
    assert initial[0]["model_usage"]["request_count"] == 1
    assert result["model_usage"]["request_count"] == 4
    assert initial[0]["model_config"]["review_mode"] == "off"


def test_failed_reviewer_retains_usage_and_previous_verdict_in_feedback(toy_db):
    verdict = review_verdict()
    verdict["claim_checks"] = scripted_claims(evidence_for(toy_db))
    verdict["claim_checks"][0]["evidence_value"] = "1000000"
    model = ScriptedModel([], reviews=[verdict, verdict])
    with pytest.raises(PredictionReviewError) as captured:
        SQLAnalysisAgent(toy_db, llm=model, max_queries=1, review_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert captured.value.model_usage["request_count"] == 3
    assert json.loads(model.calls[-1][-1].content)["previous_invalid_review"]["claim_checks"][0]["evidence_value"] == "1000000"


def test_reasoning_and_separate_reviewer_configuration(monkeypatch, toy_db):
    import langchain_openai
    import sql_agent.graph as graph_module

    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Client)
    monkeypatch.setattr(graph_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("OPENAI_MODEL", "configured-analysis-model")
    monkeypatch.setenv("OPENAI_REVIEW_MODEL", "configured-review-model")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    agent = SQLAnalysisAgent(toy_db, model_timeout=90, review_mode="on")
    assert agent.review_llm is not agent.llm
    assert calls == [
        {"model": "configured-analysis-model", "timeout": 90, "max_retries": 0, "max_tokens": 12000, "use_responses_api": True, "reasoning_effort": "medium"},
        {"model": "configured-review-model", "timeout": 90, "max_retries": 0, "max_tokens": 12000, "use_responses_api": True}]
    calls.clear()
    for name in ("OPENAI_MODEL", "OPENAI_REVIEW_MODEL", "OPENAI_REASONING_EFFORT", "SQL_AGENT_REVIEW_MODE", "SQL_AGENT_RESEARCH_MODE", "SQL_AGENT_MAX_MODEL_CALLS", "SQL_AGENT_MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    default = SQLAnalysisAgent(toy_db)
    assert default.review_mode == "off" and default.model_config["review_model"] is None
    assert default.research_mode == "on" and default.max_model_calls == 24
    assert calls == [
        {"model": "gpt-5.4", "timeout": 120.0, "max_retries": 0, "max_tokens": 12000, "use_responses_api": True, "reasoning_effort": "medium"}]
    calls.clear()
    SQLAnalysisAgent(toy_db, review_mode="on", reasoning_effort="medium")
    assert calls == [
        {"model": "gpt-5.4", "timeout": 120.0, "max_retries": 0, "max_tokens": 12000, "use_responses_api": True, "reasoning_effort": "medium"},
        {"model": "gpt-5.4", "timeout": 120.0, "max_retries": 0, "max_tokens": 12000, "use_responses_api": True}]


def test_agent_opposition_and_style_tools_use_consistent_mandatory_evidence(toy_db):
    actions = [{"action": "get_opponent_quality", "fighter": "Alpha", "purpose": "Check strength"},
               {"action": "get_style_matchup", "a": "Beta", "b": "Alpha", "purpose": "Check style"}]
    model = ScriptedModel(actions)
    result = SQLAnalysisAgent(toy_db, llm=model, max_queries=3, review_mode="on", research_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert len(result["queries_run"]) == 1  # Cached reads count toward attempts, not fabricated SQL queries.
    assert model.plan_count == 2
    observations = json.loads(model.calls[-1][1].content)["supplemental_observations"]
    assert [r["path"] for r in observations[0]["result"]["evidence_references"]] == ["mandatory_evidence.a"]
    assert [r["path"] for r in observations[1]["result"]["evidence_references"]] == ["mandatory_evidence.b", "mandatory_evidence.a"]


def test_invalid_or_unavailable_reviewer_fails_closed(toy_db):
    malformed = ScriptedModel([], reviews=[{}, {}])
    with pytest.raises(PredictionReviewError, match="invalid assessment"):
        SQLAnalysisAgent(toy_db, llm=malformed, max_queries=1, review_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert malformed.review_count == 2

    class OfflineReviewer:
        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, messages):
            raise TimeoutError("Reviewer unavailable")

    with pytest.raises(TimeoutError, match="Reviewer unavailable"):
        SQLAnalysisAgent(toy_db, llm=ScriptedModel([]), review_llm=OfflineReviewer(), max_queries=1, review_mode="on").predict("Alpha", "Beta", CUTOFF)


def test_reviewer_must_explain_a_failed_check_before_revision(toy_db):
    incomplete = review_verdict(reject=True)
    incomplete["issues"] = []
    model = ScriptedModel([], reviews=[incomplete, review_verdict()])
    result = SQLAnalysisAgent(toy_db, llm=model, max_queries=1, review_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert model.review_count == 2 and result["evaluation"]["revision_count"] == 0
    assert "concrete issue" in model.calls[-1][-1].content


@pytest.mark.parametrize("evaluation", [{}, {"status": "approved"}, {
    "status": "approved", "checks": {name: name != "evidence_grounded" for name in ReviewChecks.model_fields}}])
def test_report_blocks_unreviewed_or_failed_candidates(tmp_path, evaluation):
    report = PredictionReport(tmp_path, "Alpha vs Beta", CUTOFF)
    result = {**draft(), "fighter_a": "Alpha", "fighter_b": "Beta", "evaluation": evaluation}
    with pytest.raises(ValueError, match="publication was blocked"):
        report.add_prediction(result)
    with pytest.raises(ValueError, match="publication was blocked"):
        report.finish(result, save_json=True)
    assert "Pick:" not in report.path.read_text(encoding="utf-8")
    assert not report.path.with_suffix(".json").exists()


def test_graph_repairs_sql_and_reloads_prompt(toy_db, tmp_path):
    prompt = tmp_path / "system.md"
    prompt.write_text("User-owned prompt v1", encoding="utf-8")
    actions = [
        {"action": "run_sql", "purpose": "Misspelled column", "query": "SELECT no_such_column FROM fights"},
        {"action": "run_sql", "purpose": "Fix spelling", "query": "SELECT COUNT(*) AS n FROM fights"},
        {"action": "finish", "purpose": "Enough evidence"},
    ]
    model = ScriptedModel(actions * 2)
    agent = SQLAnalysisAgent(toy_db, llm=model, prompt_path=prompt, research_mode="on")
    result = agent.predict("Alpha", "Beta", CUTOFF)
    assert len(result["queries_run"]) == 3
    assert result["queries_run"][1]["error"] and result["queries_run"][2]["rows_returned"] == 1
    assert "no such column" in model.calls[1][-1].content
    assert model.calls[0][0].content == "User-owned prompt v1"
    first_run_calls = len(model.calls)
    prompt.write_text("User-owned prompt v2", encoding="utf-8")
    agent.predict("Alpha", "Beta", CUTOFF)
    assert model.calls[first_run_calls][0].content == "User-owned prompt v2"


def test_invalid_planner_operation_retries_with_feedback_and_respects_budget(toy_db):
    invalid = {"action": "predict_winner", "purpose": "Malformed operation"}
    model = ScriptedModel([invalid, {"action": "finish", "purpose": "Corrected operation"}])
    result = SQLAnalysisAgent(toy_db, llm=model, research_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert result["predicted_winner"] == "Alpha"
    assert model.plan_count == 2
    assert "operation_validation_error" in model.calls[1][-1].content
    assert "finish" in model.calls[1][-1].content
    exhausted = ScriptedModel([invalid])
    result = SQLAnalysisAgent(toy_db, llm=exhausted, max_queries=2, research_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert exhausted.plan_count == 1
    assert any("budget" in c for c in result["caveats"])
    schema = Action.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert all("default" not in field for field in schema["properties"].values())


@pytest.mark.parametrize("action", [
    {"action": "run_sql", "purpose": "Repeated errors", "query": "DROP TABLE fights"},
    {"action": "get_fighter_history", "purpose": "Missing fighter", "fighter": "unknown"},
    {"action": "describe_schema", "purpose": "Repeated schema discovery"},
])
def test_graph_hard_cap_including_errors_and_schema(toy_db, action):
    model = ScriptedModel([action] * 20)
    result = SQLAnalysisAgent(toy_db, llm=model, max_queries=3, research_mode="on").predict("Alpha", "Beta", CUTOFF)
    assert model.plan_count == 2
    assert any("budget" in caveat for caveat in result["caveats"])
    assert len(result["queries_run"]) <= 3


def test_output_validation_rejects_probability_and_other_winner(toy_db):
    with pytest.raises(ValidationError):
        PredictionDraft.model_validate(dict(draft(), win_probability=0.49))
    with pytest.raises(ValidationError):
        PredictionDraft.model_validate(dict(draft(), win_probability=float("nan")))
    model = ScriptedModel([{"action": "finish", "purpose": "Done"}], winner="Gamma")
    with pytest.raises(ValueError, match="outside"):
        SQLAnalysisAgent(toy_db, llm=model).predict("Alpha", "Beta", CUTOFF)


def test_round_validation_rejects_impossible_distance_and_early_decision(toy_db):
    for ending_round in (0, 6):
        with pytest.raises(ValidationError):
            PredictionDraft.model_validate(dict(draft(), predicted_round=ending_round))
    model = ScriptedModel([{"action": "finish", "purpose": "Done"}])
    # The scripted round-3 decision cannot be accepted for a five-round bout.
    with pytest.raises(ValueError, match="decision must end in round 5"):
        SQLAnalysisAgent(toy_db, llm=model).predict("Alpha", "Beta", CUTOFF, scheduled_rounds=5)


def real_matchup_actions():
    actions = [{"action": "describe_schema", "purpose": "Discover query surface"}]
    for fighter in ("Jon Jones", "Daniel Cormier"):
        for action in ("get_fighter_history", "get_career_stats", "get_opponent_quality"):
            actions.append({"action": action, "fighter": fighter, "purpose": f"Inspect {fighter}: {action}"})
    for action in ("get_common_opponents", "get_mma_math_paths", "get_style_matchup"):
        actions.append({"action": action, "a": "Jon Jones", "b": "Daniel Cormier", "purpose": action})
    actions.append({"action": "finish", "purpose": "Complete integration test"})
    return actions


def test_end_to_end_real_database_matchup():
    path = ROOT / "ufc_fights.db"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    model = ScriptedModel(real_matchup_actions(), winner="Jon Jones")
    result = SQLAnalysisAgent(path, llm=model, research_mode="on").predict("Jon Jones", "Daniel Cormier", "2015-01-03")
    assert result["fighter_a"] == "Jon Jones" and result["fighter_b"] == "Daniel Cormier"
    assert result["as_of_date"] == "2015-01-03" and len(result["queries_run"]) == 7
    assert all("error" not in item for item in result["queries_run"])
    assert all(item["rows_returned"] > 0 for item in result["queries_run"])
    observations = [json.loads(call[-1].content) for call in model.calls[1:-1]]
    career = next(o["result"] for o in observations if o.get("operation", {}).get("action") == "get_career_stats")
    assert career["career"]["record"]["win"] == 14
    assert all(r["date"] < "2015-01-03" for r in career["recent_results"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.skipif(os.getenv("RUN_LIVE_SQL_AGENT") != "1" or not os.getenv("OPENAI_API_KEY"),
                    reason="Opt in with RUN_LIVE_SQL_AGENT=1 and OPENAI_API_KEY for a paid live model test")
def test_live_model_real_matchup():
    result = SQLAnalysisAgent().predict("Jon Jones", "Daniel Cormier", "2015-01-03")
    assert result["predicted_winner"] in {"Jon Jones", "Daniel Cormier"}
    assert 0.5 <= result["win_probability"] <= 1
    assert result["queries_run"]
