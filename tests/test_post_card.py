from __future__ import annotations

import copy
import csv
import json
import sqlite3
from pathlib import Path

import pytest

from pipeline import post_card
from sql_agent.evaluation import ReviewChecks
from sql_agent.outcomes import (
    START, annotate_report, archive_report, comparison_text, method_group, original_bytes,
    read_report, score_report, IncompleteReportError,
)
from sql_agent.replay import replay
from sql_agent.reports import PredictionReport


URL = "http://www.ufcstats.com/event-details/aaaaaaaaaaaaaaaa"
DAY = "2026-10-10"


def legacy(path: Path, *, winner="Alpha", method="Submission", ending_round=2):
    raw = (f"UFC Test | {DAY}\r\nStatus checked: 2026-10-10T15:00:00+00:00\r\nSource: {URL}\r\n"
           f"Evidence cutoff: strictly before {DAY}; today's results excluded.\r\n\r\n"
           "Alpha vs Beta | Lightweight | Outcome: WRONG\r\n"
           f"Pick: {winner}\r\nConfidence: 70.0%\r\nPredicted ending: {method}, round {ending_round}\r\n"
           "Why: Original justification stays.\r\n  - Alpha is 28 vs Beta's 31.\r\n"
           "Note: Scheduled distance assumes 3 rounds; verify the bout agreement.\r\n").encode()
    path.write_bytes(raw)
    return raw


@pytest.fixture
def source():
    return {"event_url": URL, "event_date": DAY, "event_name": "UFC Test", "fetched_at": "2026-10-11T03:00:00+00:00",
            "fights": [{"fight_id": "bbbbbbbbbbbbbbbb", "fighter_a_name": "Beta", "fighter_b_name": "Alpha",
                        "winner_name": "Alpha", "result": "completed", "method_of_victory": "Submission",
                        "ending_round": 2, "ending_time": "3:45", "source_fight_url": "https://www.ufcstats.com/fight-details/bbbbbbbbbbbbbbbb"}]}


@pytest.fixture
def report(tmp_path):
    path = tmp_path / "old.txt"
    legacy(path)
    return read_report(path)


def approved():
    return {"fighter_a": "Alpha", "fighter_b": "Beta", "predicted_winner": "Alpha", "win_probability": 0.71234,
            "predicted_method": "Submission", "predicted_round": 2, "scheduled_rounds": 3,
            "summary": "Evidence based summary", "key_factors": ["A finding"], "caveats": [],
            "evaluation": {"status": "approved", "checks": {k: True for k in ReviewChecks.model_fields},
                           "summary": "Passed", "review_rounds": 1, "revision_count": 0}}


def test_legacy_preserves_manual_annotations_and_does_not_parse_prose_as_matchup(report):
    assert len(report["predictions"]) == 1
    assert report["predictions"][0]["scheduled_rounds"] == 3
    path = Path(report["path"])
    assert archive_report(path).read_bytes() == path.read_bytes()


def test_inverted_source_order_matches_exact_names_and_scores_probability(report, source):
    scored = score_report(report, source)
    assert scored["fights"][0]["status"] == "RIGHT"  # ignores user's handwritten WRONG
    assert scored["summary"]["exact"] == {"correct": 1, "scored": 1, "accuracy": 1}
    assert scored["summary"]["brier_score"] == pytest.approx(0.09)


@pytest.mark.parametrize("method, expected", [("U-DEC", "Decision"), ("Decision - Split", "Decision"),
                                             ("M-DEC", "Decision"), ("KO/TKO", "KO/TKO"),
                                             ("TKO - Doctor's Stoppage", "KO/TKO"), ("SUB", "Submission"),
                                             ("DQ", "DQ"), ("unknown", None)])
def test_method_categories(method, expected):
    assert method_group(method) == expected


def test_wrong_winner_but_correct_ending_is_not_exact(report, source):
    source["fights"][0]["winner_name"] = "Beta"
    result = score_report(report, source)
    row = result["fights"][0]
    assert row["status"] == "WRONG" and row["method_correct"] and row["round_correct"]
    assert not row["exact_correct"]
    assert result["summary"]["brier_score"] == pytest.approx(0.49)


@pytest.mark.parametrize("status", ["draw", "no_contest", "pending"])
def test_no_binary_winner_is_unscored(report, source, status):
    source["fights"][0].update(result=status, winner_name=None)
    result = score_report(report, source)
    assert result["summary"]["winner"]["scored"] == 0
    assert result["summary"]["brier_score"] is None
    assert result["fights"][0]["actual"]["ending_time"] == "3:45"


def test_unknown_method_or_round_is_not_a_guessed_mismatch(report, source):
    source["fights"][0].update(method_of_victory="new source label", ending_round=None)
    row = score_report(report, source)["fights"][0]
    assert row["winner_correct"] and row["method_correct"] is None and row["round_correct"] is None
    assert row["exact_correct"] is None


def test_matchup_replacement_not_fuzzy_matched(report, source):
    source["fights"][0].update(fighter_a_name="Beta Jr.")
    result = score_report(report, source)
    assert result["summary"]["unscored"] == 1
    report["predictions"][0]["fight_id"] = "bbbbbbbbbbbbbbbb"
    with pytest.raises(ValueError, match="participants changed"):
        score_report(report, source)


def test_different_event_date_and_leaked_cutoff_rejected(report, source):
    bad = copy.deepcopy(source)
    bad["event_url"] = URL.replace("aaaa", "cccc")
    with pytest.raises(ValueError, match="same event"):
        score_report(report, bad)
    bad = copy.deepcopy(source)
    bad["event_date"] = "2026-10-11"
    with pytest.raises(ValueError, match="date"):
        score_report(report, bad)
    report["as_of_date"] = "2026-10-11"
    with pytest.raises(ValueError, match="cutoff"):
        score_report(report, source)


def test_annotation_idempotent_and_archive_byte_identical(report, source, tmp_path):
    path = Path(report["path"])
    original = path.read_bytes()
    for _ in range(2):
        parsed = read_report(path)
        result = score_report(parsed, source)
        archived = annotate_report(parsed, result, snapshot_path=tmp_path / "source.json")
    assert archived.read_bytes() == original == original_bytes(path)
    assert path.read_bytes().count(START) == 1
    assert "Actual result: Alpha; Submission, round 2, 3:45" in path.read_text()
    assert len(list((tmp_path / "archive").glob("*/*.txt"))) == 1


def test_outcome_correction_updates_annotations_not_original(report, source, tmp_path):
    original = Path(report["path"]).read_bytes()
    post_card.evaluate_event([report], source, tmp_path / "evaluations")
    source["fights"][0].update(winner_name=None, result="no_contest")
    post_card.evaluate_event([read_report(report["path"])], source, tmp_path / "evaluations")
    assert original_bytes(Path(report["path"])) == original
    assert "UNSCORED / no_contest" in Path(report["path"]).read_text()
    index = json.loads((tmp_path / "evaluations/index.json").read_text())
    assert len(index["reports"]) == 1
    assert next(iter(index["reports"].values()))["summary"]["winner"]["scored"] == 0
    assert len(list((tmp_path / "evaluations/aaaaaaaaaaaaaaaa/sources").glob("*.json"))) == 2


def test_ledger_saved_in_terminal_mode_keeps_full_precision_and_rejects_tampered_pick(tmp_path, source):
    card = {"event_date": DAY, "last_scraped_at": "2026-10-10", "event_url": URL, "caveats": []}
    report = PredictionReport(tmp_path, "UFC Test", DAY, card=card)
    prediction = approved()
    report.add_prediction(prediction)
    report.finish(prediction)
    parsed = read_report(report.path)
    assert parsed["predictions"][0]["win_probability"] == 0.71234
    assert report.ledger_path.exists() and not report.path.with_suffix(".json").exists()
    report.path.write_text(report.path.read_text().replace("Pick: Alpha", "Pick: Beta"))
    with pytest.raises(ValueError, match="disagree"):
        read_report(report.path)


def test_incomplete_forecast_cannot_be_annotated_while_writer_is_active(tmp_path):
    report = PredictionReport(tmp_path, "UFC Test", DAY)
    report.add_prediction(approved())
    before = report.path.read_bytes()
    with pytest.raises(IncompleteReportError):
        read_report(report.path)
    assert post_card.main(["--predictions-dir", str(tmp_path)]) == 0
    assert report.path.read_bytes() == before


def test_failed_predictions_retained_and_paired_comparison_uses_same_bouts(report, source):
    old = score_report(report, source)
    modified = copy.deepcopy(report)
    modified["analysis_mode"] = "retrospective_replay"
    modified["predictions"].append({"fighter_a": "Gamma", "fighter_b": "Delta", "error": "Review withheld"})
    new = score_report(modified, source)
    assert new["summary"]["attempted"] == 2 and new["summary"]["published"] == 1
    assert new["summary"]["failed_predictions"] == 1
    comparison = comparison_text([old, new])
    assert "1 shared scored fights" in comparison and "AFTER the event" in comparison


def test_replay_preserves_cohort_and_cutoff_without_sending_old_picks(report, tmp_path):
    db = tmp_path / "toy.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE fighters (fighter_id TEXT, fighter_name TEXT)")
        connection.executemany("INSERT INTO fighters VALUES (?, ?)", [("0000000000000001", "Alpha"), ("0000000000000002", "Beta")])
    calls = []

    class FakeAgent:
        def __init__(self, *a, **kw):
            pass

        def predict(self, a, b, cutoff, **kwargs):
            calls.append((a, b, cutoff, kwargs))
            raise RuntimeError("Reviewer withheld")

    path = replay(Path(report["path"]), db=db, output_dir=tmp_path, agent_factory=FakeAgent, on_progress=lambda _: None)
    assert calls[0][:3] == ("Alpha", "Beta", DAY)
    assert calls[0][3]["scheduled_rounds"] == 3
    assert not {"predicted_winner", "win_probability", "actual", "summary"} & calls[0][3].keys()
    parsed = read_report(path)
    assert parsed["analysis_mode"] == "retrospective_replay"
    assert parsed["predictions"][0]["error"] == "Reviewer withheld"
    assert "not a prospective test" in path.read_text()


def test_scrape_source_closes_and_retains_true_result(source):
    class Scraper:
        closed = False

        def crawl_event_details(self, url):
            return {"event_date": "October 10, 2026", "event_name": "UFC Test", "fight_urls": ["fight"]}

        def crawl_fight_card(self, url):
            return source["fights"][0]

        def close(self):
            self.closed = True

    scraper = Scraper()
    result = post_card.scrape_results(URL, scraper_factory=lambda: scraper)
    assert scraper.closed
    assert result["fights"][0]["winner_name"] == "Alpha" and result["event_date"] == DAY


def test_cli_offline_regrading_is_idempotent_without_model_calls(report, source, tmp_path, monkeypatch):
    snapshot = tmp_path / "source.json"
    snapshot.write_text(json.dumps(source))
    monkeypatch.setattr(post_card, "scrape_results", lambda *_: pytest.fail("No network in offline mode"))
    args = ["--report", report["path"], "--results-file", str(snapshot), "--predictions-dir", str(tmp_path)]
    assert post_card.main(args) == post_card.main(args) == 0
    assert len(list((tmp_path / "evaluations/aaaaaaaaaaaaaaaa").glob("*.evaluation.json"))) == 1
    with (tmp_path / "evaluations/weekly_metrics.csv").open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 1


def test_unmatched_event_error_leaves_forecast_unchanged(report, source, tmp_path):
    path = Path(report["path"])
    before = path.read_bytes()
    source["event_date"] = "2026-10-11"
    with pytest.raises(ValueError, match="date"):
        post_card.evaluate_event([report], source, tmp_path / "evaluations")
    assert path.read_bytes() == before


def test_ablation_reuses_the_initial_draft_and_never_passes_old_picks_to_agents(report, tmp_path):
    from sql_agent.ablation import compare

    db = tmp_path / "toy.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE fighters (fighter_id TEXT, fighter_name TEXT)")
        connection.executemany("INSERT INTO fighters VALUES (?, ?)", [("0000000000000001", "Alpha"), ("0000000000000002", "Beta")])
    calls = []

    class FakeAgent:
        def __init__(self, *a, **kwargs):
            self.config = kwargs

        def predict(self, a, b, cutoff, **kwargs):
            calls.append((self.config, a, b, cutoff, kwargs))
            initial = approved()
            callback = kwargs.get("on_initial_prediction")
            if callback:
                callback(initial)
            return {**initial, "win_probability": .6}

    path = compare(Path(report["path"]), db=db, output_dir=tmp_path, workers=1,
                   agent_factory=FakeAgent, progress=lambda _: None)
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "complete" and len(calls) == 2
    assert {call[0]["reasoning_effort"] for call in calls} == {"none", "medium"}
    assert all(call[3] == DAY and "predicted_winner" not in call[4] for call in calls)
    before = read_report(manifest["reports"]["medium_direct"])["predictions"][0]
    after = read_report(manifest["reports"]["medium_review"])["predictions"][0]
    assert before["win_probability"] == .71234 and after["win_probability"] == .6


def test_interrupted_experiment_recovers_checkpoints_and_marks_missing_bouts(tmp_path):
    from sql_agent.ablation import finalize_interrupted
    from sql_agent.outcomes import atomic_json

    report = PredictionReport(tmp_path, "UFC Test", DAY, card={"event_url": URL, "event_date": DAY,
                                                             "last_scraped_at": DAY, "caveats": []})
    manifest_path = report.path.with_suffix(".experiment.json")
    manifest = {"status": "running", "reports": {"none_direct": str(report.path)},
                "provenance": {"worklist": [{"fighter_a": "Alpha", "fighter_b": "Beta"},
                                             {"fighter_a": "Gamma", "fighter_b": "Delta"}]}}
    atomic_json(manifest_path, manifest)
    checkpoint = tmp_path / "experiments" / manifest_path.stem / "00-none_direct.json"
    atomic_json(checkpoint, approved())
    report.add_prediction(approved())  # Already published records must not be duplicated.
    finalize_interrupted(manifest_path, "API credits exhausted")
    restored = read_report(report.path)["predictions"]
    assert len(restored) == 2 and restored[0] == approved()
    assert restored[1]["error_type"] == "ExperimentInterrupted"
    assert "predicted_winner" not in restored[1]
    assert json.loads(manifest_path.read_text())["status"] == "stopped_quota"
    before = report.path.read_bytes()
    with pytest.raises(ValueError, match="Only an unfinished"):
        finalize_interrupted(manifest_path, "Retry must not overwrite results")
    assert report.path.read_bytes() == before
