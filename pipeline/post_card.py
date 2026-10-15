"""Fetch final UFCStats results and update saved forecasts, without model calls."""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
from collections import defaultdict
from pathlib import Path

from sql_agent.outcomes import (
    annotate_report, archive_report, atomic_bytes, atomic_json, comparison_text, digest,
    event_key, read_report, score_report, utc_now, validate_results, IncompleteReportError,
)
from sql_agent.reports import DEFAULT_OUTPUT_DIR

from .locking import InterProcessLock
from .pipeline import parse_event_date
from .scraper import RateLimitedPlaywrightScraper


def scrape_results(event_url: str, *, scraper_factory=RateLimitedPlaywrightScraper) -> dict:
    key = event_key(event_url)
    event_url = f"http://www.ufcstats.com/event-details/{key}"
    scraper = scraper_factory()
    try:
        event = scraper.crawl_event_details(event_url)
        day = parse_event_date(event.get("event_date"))
        if not day:
            raise ValueError("Source event has no date")
        fights = []
        for url in event["fight_urls"]:
            fight = scraper.crawl_fight_card(url)
            # Only source result fields are stored: no prediction, inference or LLM judging.
            fields = ("fight_id", "fighter_a_id", "fighter_b_id", "fighter_a_name", "fighter_b_name",
                      "winner_name", "result", "method_of_victory", "method_detail", "ending_round",
                      "ending_time", "time_format", "source_fight_url")
            actual = {key: fight.get(key) for key in fields}
            if actual["result"] == "completed" and not actual["winner_name"]:
                actual["result"] = "pending"
            fights.append(actual)
        results = {"schema_version": 1, "event_url": event_url, "event_name": event["event_name"],
                   "event_date": day.isoformat(), "fetched_at": utc_now(), "fights": fights,
                   "has_unplayed_bouts": event.get("has_unplayed_bouts", False)}
        validate_results(results)
        return results
    finally:
        scraper.close()


def update_index(directory: Path, evaluations: list[dict]) -> None:
    """One row per original report, replaced on regrading rather than double-counted."""
    index_path = directory / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"schema_version": 1, "reports": {}}
    for evaluated in evaluations:
        key = evaluated["original_sha256"]
        index["reports"][key] = {k: evaluated[k] for k in (
            "report_path", "original_sha256", "analysis_mode", "prediction_generated_at", "event_name", "event_date",
            "event_url", "results_fetched_at", "summary", "evaluation_path")}
    atomic_json(index_path, index)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["event_date", "event_name", "mode", "report", "published", "attempted", "failed",
                     "winner_correct", "winner_scored", "method_correct", "method_scored", "round_correct", "round_scored",
                     "exact_correct", "exact_scored", "brier", "evaluation"])
    for record in sorted(index["reports"].values(), key=lambda r: (r["event_date"], r["report_path"])):
        s = record["summary"]
        writer.writerow([record["event_date"], record["event_name"], record["analysis_mode"], record["report_path"],
                         s["published"], s["attempted"], s["failed_predictions"],
                         *[s[field][part] for field in ("winner", "method", "round", "exact") for part in ("correct", "scored")],
                         s["brier_score"], record["evaluation_path"]])
    atomic_bytes(directory / "weekly_metrics.csv", output.getvalue().encode("utf-8"))


def evaluate_event(reports: list[dict], results: dict, directory: Path) -> tuple[list[dict], Path]:
    # Validate every match before changing any readable forecast.
    evaluations = [score_report(report, results) for report in reports]
    event_directory = directory / event_key(results["event_url"])
    encoded = json.dumps(results, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    snapshot_path = event_directory / "sources" / f"{digest(encoded)}.json"
    if not snapshot_path.exists():
        atomic_json(snapshot_path, results)
    for report, evaluated in zip(reports, evaluations):
        annotate_report(report, evaluated, snapshot_path=snapshot_path)
        path = event_directory / f"{Path(report['path']).stem}-{report['original_sha256'][:12]}.evaluation.json"
        evaluated["evaluation_path"] = str(path)
        atomic_json(path, evaluated)
    # Include previous scored runs for this event, so subsequent weekly grading cannot erase a comparison.
    all_evaluations = {}
    for path in sorted(event_directory.glob("*.evaluation.json")):
        prior = json.loads(path.read_text(encoding="utf-8"))
        # Re-score prior immutable forecasts against this snapshot to handle later corrections fairly.
        original = read_report(prior["original_archive"], event_url=results["event_url"])
        refreshed = score_report(original, results)
        all_evaluations[prior["original_sha256"]] = {**prior, **refreshed, "report_path": prior["report_path"]}
    ordered = sorted(all_evaluations.values(), key=lambda e: (e["prediction_generated_at"] or "", e["report_path"]))
    comparison = event_directory / "comparison.txt"
    atomic_bytes(comparison, comparison_text(ordered).encode("utf-8"))
    update_index(directory, evaluations)
    return evaluations, comparison


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Post-card source scrape and deterministic grading of saved predictions; no LLM calls")
    parser.add_argument("--report", type=Path, action="append", help="Saved text report; repeat to compare runs. Default: card reports in predictions/")
    parser.add_argument("--predictions-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--event", help="Explicit UFCStats event URL; also binds a single-fight report to its actual event")
    parser.add_argument("--results-file", type=Path, help="Reuse a saved source snapshot offline instead of scraping")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    directory = args.predictions_dir.resolve() / "evaluations"
    try:
        if args.event:
            event_key(args.event)
        cached = json.loads(args.results_file.read_text(encoding="utf-8")) if args.results_file else None
        if cached:
            validate_results(cached)
        groups = defaultdict(list)
        errors = []
        for path in args.report or sorted(args.predictions_dir.glob("*.txt")):
            try:
                if args.event and not args.report:
                    saved = read_report(path)
                    if saved["event_url"] and event_key(saved["event_url"]) != event_key(args.event):
                        continue
                report = read_report(path, event_url=args.event)
                if not report["event_url"]:
                    if args.report:
                        raise ValueError("Single-fight/legacy report needs --event with its exact event URL")
                    print(f"Skipped {path.name}: no event URL; grade explicitly with --report and --event.", flush=True)
                    continue
                if cached and event_key(cached["event_url"]) != event_key(report["event_url"]):
                    if args.report:
                        raise ValueError("Result snapshot belongs to a different event")
                    continue
                # Archive first, including any incomplete run; later source failure cannot lose the forecasts.
                archive_report(path.resolve())
                groups[event_key(report["event_url"])].append(report)
            except IncompleteReportError as exc:
                if args.report:
                    errors.append(str(exc))
                else:
                    print(f"Skipped incomplete report: {exc}", flush=True)
            except (ValueError, OSError, KeyError) as exc:
                errors.append(f"{path.name}: {exc}")
        if not groups and not errors:
            print("No saved card predictions to evaluate.", flush=True)
        with InterProcessLock(directory / ".post-card.lock"):
            for reports in groups.values():
                try:
                    source_url = reports[0]["event_url"]
                    print(f"Fetching results: {source_url}" if not cached else f"Using saved results: {args.results_file}", flush=True)
                    results = cached or scrape_results(source_url)
                    evaluations, comparison = evaluate_event(reports, results, directory)
                    for evaluated in evaluations:
                        s = evaluated["summary"]
                        print(f"Updated {evaluated['report_path']}: winner {s['winner']['correct']}/{s['winner']['scored']}, "
                              f"exact {s['exact']['correct']}/{s['exact']['scored']}; {s['failed_predictions']} failed predictions", flush=True)
                    print(f"Comparison: {comparison}", flush=True)
                except Exception as exc:
                    errors.append(f"{reports[0]['event_url']}: {exc}")
        for error in errors:
            print(f"Post-card error: {error}", flush=True)
        return 2 if errors else 0
    except Exception as exc:
        print(f"Post-card error: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
