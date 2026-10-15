"""One explicitly requested retrospective run of the same saved matchups."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from .database import ROOT
from .outcomes import archive_report, read_report
from .reports import DEFAULT_OUTPUT_DIR, PredictionReport


def prepare_replay(report_path: Path, db: Path):
    baseline = read_report(report_path)
    if not baseline["event_url"] or not baseline["event_date"]:
        raise ValueError("Replay needs a card report with an event URL and date")
    if baseline["as_of_date"] > baseline["event_date"]:
        raise ValueError("Baseline evidence cutoff is after the event date")
    archived = archive_report(report_path)
    # Freeze a minimal worklist. Old picks, explanations and actual outcomes never enter model requests.
    jobs = []
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        for prediction in baseline["predictions"]:
            identities = []
            for side in ("a", "b"):
                name = prediction[f"fighter_{side}"]
                rows = connection.execute("SELECT fighter_id, fighter_name FROM fighters WHERE fighter_name = ?", (name,)).fetchall()
                if len(rows) != 1:
                    raise ValueError(f"Replay requires an unambiguous saved fighter identity: {name}")
                identities.append({"fighter_id": rows[0][0], "fighter_name": rows[0][1]})
            rounds = prediction.get("scheduled_rounds")
            if rounds not in (3, 5):
                raise ValueError("Baseline must record scheduled rounds; do not infer them from actual results")
            jobs.append({"fighter_a": prediction["fighter_a"], "fighter_b": prediction["fighter_b"],
                         "scheduled_rounds": rounds, "fighter_identities": identities})
    card = {"event_name": baseline["title"], "event_date": baseline["event_date"],
            "event_url": baseline["event_url"], "last_scraped_at": baseline["generated_at"],
            "caveats": ["Same matchup cohort and scheduled distances as the archived baseline."]}
    provenance = {"baseline_archive": str(archived), "baseline_sha256": baseline["original_sha256"],
                  "database_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
                  "prompt_sha256": {name: hashlib.sha256((ROOT / "prompts" / name).read_bytes()).hexdigest()
                                    for name in ("sql_agent_system.md", "sql_agent_review.md")},
                  "worklist": jobs}
    return baseline, jobs, card, provenance


def replay(report_path: Path, *, db: Path, output_dir: Path, workers: int = 1,
           model: str | None = None, review_model: str | None = None,
           timeout: float = 30.0, on_progress=print, agent_factory=None,
           review_mode: str | None = None, reasoning_effort: str | None = None,
           research_mode: str | None = None, max_model_calls: int | None = None,
           max_output_tokens: int | None = None) -> Path:
    from .graph import SQLAnalysisAgent

    if workers not in (1, 2, 3) or timeout <= 0:
        raise ValueError("Use 1-3 replay workers and a positive SQL timeout")
    baseline, jobs, card, provenance = prepare_replay(report_path, db)
    provenance.update(sql_timeout_seconds=timeout, workers=workers)
    report = PredictionReport(output_dir, baseline["title"] + " - updated system replay", baseline["as_of_date"],
                              card=card, analysis_mode="retrospective_replay", provenance=provenance)
    on_progress(f"Replay report: {report.path}")

    def run(job):
        label = f"{job['fighter_a']} vs {job['fighter_b']}"
        try:
            agent = (agent_factory or SQLAnalysisAgent)(db, model=model, review_model=review_model, timeout=timeout,
                                                       review_mode=review_mode, reasoning_effort=reasoning_effort,
                                                       research_mode=research_mode, max_model_calls=max_model_calls,
                                                       max_output_tokens=max_output_tokens)
            return agent.predict(job["fighter_a"], job["fighter_b"], baseline["as_of_date"],
                                 scheduled_rounds=job["scheduled_rounds"],
                                 rounds_basis="archived baseline scheduled distance",
                                 fighter_identities=job["fighter_identities"],
                                 on_progress=lambda message: on_progress(f"[{label}] {message}"))
        except Exception as exc:
            return {"fighter_a": job["fighter_a"], "fighter_b": job["fighter_b"], "error": str(exc),
                    "error_type": type(exc).__name__, "review_history": getattr(exc, "review_history", []),
                    "model_usage": getattr(exc, "model_usage", None)}

    # Futures are published in baseline order, independent of their outcomes.
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for position, result in enumerate(pool.map(run, jobs), 1):
            report.add_prediction(result)
            results.append(result)
            on_progress(f"Saved {position}/{len(jobs)}: {result['fighter_a']} vs {result['fighter_b']} "
                        f"({'FAILED / withheld' if 'error' in result else result['predicted_winner']})")
    report.finish({**card, "analysis_mode": "retrospective_replay", "provenance": provenance,
                   "as_of_date": baseline["as_of_date"], "predictions": results,
                   "failed_fights": sum("error" in r for r in results)}, save_json=True)
    return report.path


def main(argv=None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Paid, retrospective updated-agent replay of an archived card report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--db", type=Path, default=ROOT / "ufc_fights.db")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--model")
    parser.add_argument("--review-model")
    parser.add_argument("--review", choices=("on", "off"))
    parser.add_argument("--research", choices=("on", "off"))
    parser.add_argument("--max-model-calls", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--reasoning-effort", choices=("none", "low", "medium", "high", "xhigh"))
    parser.add_argument("--timeout", type=float, default=30.0, help="SQL timeout per query; replay workers share CPU")
    args = parser.parse_args(argv)
    try:
        path = replay(args.report, db=args.db, output_dir=args.output_dir, workers=args.workers,
                      model=args.model, review_model=args.review_model,
                      timeout=args.timeout, review_mode=args.review, reasoning_effort=args.reasoning_effort,
                      research_mode=args.research, max_model_calls=args.max_model_calls, max_output_tokens=args.max_output_tokens,
                      on_progress=lambda message: print(message, flush=True))
        print(f"Retrospective replay saved: {path}")
        return 1 if any("error" in p for p in read_report(path)["predictions"]) else 0
    except Exception as exc:
        print(f"Replay: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
