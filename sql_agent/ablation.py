"""Paid, bounded comparison of reasoning and review on an unchanged card cohort.

Actual results are only read by post-card grading AFTER all forecasts are saved.
The medium/no-review arm reuses the exact initial draft of the reviewed arm.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .database import ROOT
from .graph import SQLAnalysisAgent
from .outcomes import atomic_json, utc_now
from .replay import prepare_replay
from .reports import DEFAULT_OUTPUT_DIR, PredictionReport


POLICY = {"description": "Cost-first default; retrospective screen, not proof of generalization.",
          "min_extra_correct_winners": 2, "require_full_coverage": True,
          "require_lower_brier": True, "maximum_cost_multiplier": 2,
          "review_comparator": "medium_direct", "reasoning_comparator": "none_direct"}
VARIANTS = ("none_direct", "medium_direct", "medium_review")


def compare(report_path: Path, *, db: Path, output_dir: Path, workers=2,
            max_queries=15, agent_factory=SQLAnalysisAgent, progress=print):
    baseline, jobs, card, provenance = prepare_replay(report_path, db)
    provenance.update(experiment="reasoning_review_ablation", model="gpt-5.4", review_model="gpt-5.4",
                      max_queries=max_queries, max_revisions=2, sql_timeout_seconds=30,
                      research_mode="on", max_model_calls=24, max_output_tokens=12000,
                      shared_draft="medium_direct is the exact pre-review candidate from medium_review",
                      order="none-first on even indices, review-first on odd indices; keeps all outcomes",
                      decision_policy=POLICY)
    reports = {v: PredictionReport(output_dir, baseline["title"] + " - " + v, baseline["as_of_date"],
                                   card=card, analysis_mode="retrospective_replay", provenance={**provenance, "variant": v})
               for v in VARIANTS}
    run_path = reports["none_direct"].path.with_suffix(".experiment.json")
    manifest = {"started_at": utc_now(), "provenance": provenance,
                "reports": {v: str(r.path) for v, r in reports.items()}, "status": "running"}
    atomic_json(run_path, manifest)
    progress(f"Experiment manifest: {run_path}")
    checkpoint_dir = output_dir / "experiments" / run_path.stem

    def run(item):
        index, job = item
        label = f"{job['fighter_a']} vs {job['fighter_b']}"
        outputs = {}

        def save(variant, result):
            outputs[variant] = result
            atomic_json(checkpoint_dir / f"{index:02d}-{variant}.json", result)
            progress(f"[{index + 1}/{len(jobs)} {variant}] {label}: "
                     f"{result.get('predicted_winner', 'WITHHELD')}")

        for variant in (("none_direct", "medium_review") if index % 2 == 0 else ("medium_review", "none_direct")):
            reasoning = "none" if variant == "none_direct" else "medium"
            agent = agent_factory(db, model="gpt-5.4", review_model="gpt-5.4", review_mode="off" if variant == "none_direct" else "on",
                                  reasoning_effort=reasoning, max_queries=max_queries, timeout=30, max_revisions=2,
                                  research_mode="on", max_model_calls=24, max_output_tokens=12000)
            try:
                result = agent.predict(job["fighter_a"], job["fighter_b"], baseline["as_of_date"],
                    scheduled_rounds=job["scheduled_rounds"], rounds_basis="archived baseline scheduled distance",
                    fighter_identities=job["fighter_identities"],
                    on_progress=lambda m: progress(f"[{variant} {label}] {m}"),
                    on_initial_prediction=(lambda r: save("medium_direct", r)) if variant == "medium_review" else None)
            except Exception as exc:
                result = {"fighter_a": job["fighter_a"], "fighter_b": job["fighter_b"], "error": str(exc),
                          "error_type": type(exc).__name__, "model_usage": getattr(exc, "model_usage", None),
                          "review_history": getattr(exc, "review_history", [])}
            save(variant, result)
            if variant == "medium_review" and "medium_direct" not in outputs:
                save("medium_direct", {**result, "error": "No valid initial draft: " + result.get("error", "unknown failure")})
        return outputs

    all_results = {v: [] for v in VARIANTS}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for outputs in pool.map(run, enumerate(jobs)):
            for variant, result in outputs.items():
                reports[variant].add_prediction(result)
                all_results[variant].append(result)
    for variant, predictions in all_results.items():
        reports[variant].finish({**card, "variant": variant, "provenance": provenance, "predictions": predictions,
                                 "failed_fights": sum("error" in p for p in predictions)}, save_json=True)
    manifest.update(status="complete", finished_at=utc_now())
    atomic_json(run_path, manifest)
    return run_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--db", type=Path, default=ROOT / "ufc_fights.db")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--max-queries", type=int, default=15)
    args = parser.parse_args(argv)
    path = compare(args.report, db=args.db, output_dir=args.output_dir.resolve(), workers=args.workers,
                   max_queries=args.max_queries, progress=lambda m: print(m, flush=True))
    print(f"Comparison forecasts complete: {path}", flush=True)
    return 0


def finalize_interrupted(manifest_path: Path, reason: str):
    """Retain completed checkpoints and explicitly label missing bouts. No model calls."""
    import json

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["status"] != "running":
        raise ValueError("Only an unfinished experiment may be finalized from checkpoints")
    directory = manifest_path.parent / "experiments" / manifest_path.stem
    for variant, path in manifest["reports"].items():
        report = PredictionReport.__new__(PredictionReport)
        report.path = Path(path)
        report.ledger_path = report.path.with_suffix(".ledger.jsonl")
        ledger = [json.loads(line) for line in report.ledger_path.read_text(encoding="utf-8").splitlines()]
        saved = [r["prediction"] for r in ledger if r["kind"] == "prediction"]
        predictions = []
        for index, job in enumerate(manifest["provenance"]["worklist"]):
            checkpoint = directory / f"{index:02d}-{variant}.json"
            result = (json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else
                      {"fighter_a": job["fighter_a"], "fighter_b": job["fighter_b"],
                       "error": "Not completed: " + reason, "error_type": "ExperimentInterrupted", "model_usage": None})
            if index < len(saved):
                if result != saved[index]:
                    raise ValueError("Checkpoint differs from an already published prediction")
            else:
                report.add_prediction(result)
            predictions.append(result)
        report.append(f"\nExperiment stopped: {reason}. Missing results are not model predictions.\n")
        report.finish({"predictions": predictions, "failed_fights": sum("error" in p for p in predictions),
                       "experiment_stopped": reason, "provenance": manifest["provenance"]}, save_json=True)
    manifest.update(status="stopped_quota", finished_at=utc_now(), stop_reason=reason)
    atomic_json(manifest_path, manifest)


if __name__ == "__main__":
    raise SystemExit(main())
