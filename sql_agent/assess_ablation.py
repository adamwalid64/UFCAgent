"""Grade a completed experiment and apply its predeclared cost-first policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .outcomes import atomic_bytes, atomic_json, metric_text, pair_key, read_report, score_report, summarize, utc_now
from .usage import is_quota_error


def usage_summary(predictions):
    usages = [p.get("model_usage") for p in predictions]
    complete = bool(usages) and all(u and u["usage_complete"] and u["estimated_usd"] is not None for u in usages)
    totals = {key: sum((u or {}).get(key, 0) for u in usages) for key in (
        "request_count", "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens")}
    return {**totals, "usage_complete": complete,
            "known_estimated_usd": sum((u or {}).get("known_estimated_usd",
                sum(c.get("estimated_usd", 0) for c in u["calls"]) if u and "calls" in u else (u or {}).get("estimated_usd") or 0) for u in usages),
            "estimated_usd": sum(u["estimated_usd"] for u in usages) if complete else None}


def worthwhile(candidate, baseline, policy):
    c, b = candidate["summary"], baseline["summary"]
    cc, bc = candidate["usage"]["estimated_usd"], baseline["usage"]["estimated_usd"]
    comparable = c["published"] == c["attempted"] == b["published"] == b["attempted"]
    same_scored = c["winner"]["scored"] == b["winner"]["scored"] and c["winner"]["scored"] > 0
    checks = {
        "full_coverage": comparable,
        "same_scored_count": same_scored,
        "additional_winners": (c["winner"]["correct"] - b["winner"]["correct"] >= policy["min_extra_correct_winners"]
                               if comparable and same_scored else None),
        "lower_brier": (c["brier_score"] < b["brier_score"] if comparable and same_scored else None),
        "cost_limit": cc is not None and bc is not None and bc > 0 and cc <= bc * policy["maximum_cost_multiplier"],
    }
    return {"passes": all(checks.values()), "checks": checks}


def failure_categories(predictions):
    counts = {"published": 0, "review_withheld": 0, "billing_failure": 0, "interrupted": 0, "other_failure": 0}
    for prediction in predictions:
        if "error" not in prediction:
            category = "published"
        elif prediction.get("error_type") == "ExperimentInterrupted":
            category = "interrupted"
        elif is_quota_error(RuntimeError(prediction["error"])) or prediction.get("error_type") == "QuotaExhaustedError":
            category = "billing_failure"
        elif prediction.get("error_type") == "PredictionReviewError":
            category = "review_withheld"
        else:
            category = "other_failure"
        counts[category] += 1
    return counts


def compare_subset(left_report, right_report, left_evaluation, right_evaluation, *, completed_reviews=False):
    """Compare identical bouts; distinguish publication coverage from scored accuracy."""
    key = lambda p: pair_key(p["fighter_a"], p["fighter_b"])
    a, b = ({key(p): p for p in e["fights"]} for e in (left_evaluation, right_evaluation))
    raw_a, raw_b = ({key(p): p for p in r["predictions"]} for r in (left_report, right_report))
    shared = sorted(k for k in a.keys() & b.keys() if a[k]["winner_correct"] is not None
                    and (b[k]["winner_correct"] is not None or
                         (completed_reviews and raw_b[k].get("error_type") == "PredictionReviewError")))
    return {"fight_count": len(shared), "matchups": [f"{a[k]['fighter_a']} vs {a[k]['fighter_b']}" for k in shared],
            "left": {"summary": summarize([a[k] for k in shared]), "usage": usage_summary([raw_a[k] for k in shared])},
            "right": {"summary": summarize([b[k] for k in shared]), "usage": usage_summary([raw_b[k] for k in shared])}}


def assess(manifest_path: Path, results_path: Path):
    from pipeline.post_card import evaluate_event

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["status"] not in {"complete", "stopped_quota"}:
        raise ValueError("All forecasts must be finalized before loading outcomes or selecting defaults")
    results = json.loads(results_path.read_text(encoding="utf-8"))
    reports = {v: read_report(path) for v, path in manifest["reports"].items()}
    directory = manifest_path.parent / "evaluations"
    evaluations, _ = evaluate_event(list(reports.values()), results, directory)
    evaluations = dict(zip(reports, evaluations))
    variants = {v: {"summary": e["summary"], "usage": usage_summary(reports[v]["predictions"]),
                    "failure_categories": failure_categories(reports[v]["predictions"]),
                    "report": manifest["reports"][v]}
                for v, e in evaluations.items()}
    baseline = read_report(manifest["provenance"]["baseline_archive"])
    old_evaluation = score_report(baseline, results)
    historical = old_evaluation["summary"]
    paired = {f"old_vs_{v}": compare_subset(baseline, reports[v], old_evaluation, evaluations[v]) for v in reports}
    paired["none_vs_medium"] = compare_subset(reports["none_direct"], reports["medium_direct"],
                                               evaluations["none_direct"], evaluations["medium_direct"])
    paired["draft_vs_completed_review_attempts"] = compare_subset(reports["medium_direct"], reports["medium_review"],
                        evaluations["medium_direct"], evaluations["medium_review"], completed_reviews=True)
    policy = manifest["provenance"]["decision_policy"]
    reasoning = worthwhile(variants["medium_direct"], variants["none_direct"], policy)
    review = worthwhile(variants["medium_review"], variants["medium_direct"], policy)
    combined = worthwhile(variants["medium_review"], variants["none_direct"], policy)
    selected = "medium_review" if review["passes"] and combined["passes"] else "medium_direct" if reasoning["passes"] else "none_direct"
    settings = {"reasoning_effort": "none" if selected == "none_direct" else "medium",
                "review_mode": "on" if selected == "medium_review" else "off"}
    costs = [variants[v]["usage"]["estimated_usd"] for v in ("none_direct", "medium_review")]
    decision = {"manifest": str(manifest_path), "assessed_at": utc_now(), "variants": variants,
                "experiment_status": manifest["status"], "stop_reason": manifest.get("stop_reason"),
                "paired_comparisons": paired,
                "historical_baseline": historical, "reasoning_test": reasoning, "review_test": review,
                "combined_test": combined, "selected_variant": selected, "recommended_defaults": settings,
                "experiment_estimated_usd": sum(costs) if all(c is not None for c in costs) else None,
                "experiment_known_estimated_usd": sum(variants[v]["usage"]["known_estimated_usd"] for v in ("none_direct", "medium_review")),
                "selection_basis": ("Incomplete experiment: optional paid additions have not met the retention rule; use conservative defaults. This does not establish that no reasoning is more accurate or cheaper."
                                    if manifest["status"] != "complete" else "Predeclared cost and coverage rule; no claim of statistical significance."),
                "limitation": "One retrospective card; a cost/reliability decision, not a demonstrated prospective accuracy advantage. Billing failures and withheld picks are unscored, never counted as wrong predictions."}
    output = directory / "experiments" / (manifest_path.stem + ".decision.json")
    atomic_json(output, decision)
    lines = ["REASONING AND SELF-REVIEW: REPAIRED SYSTEM COMPARISON", "",
             f"Experiment status: {manifest['status']}; {manifest.get('stop_reason') or 'all planned runs finished'}",
             "All 10 original matchups retained. Outcomes were loaded only after the forecasts were finalized.",
             "Reasoning uses separate runs; self-review is compared with its exact initial draft.",
             "First-stage research can differ between reasoning modes; the model, prompts, database, cutoff and query cap are fixed.",
             "SQL evidence is strictly before the event; model prior knowledge cannot be ruled out.", "",
             f"Archived old system: winner {metric_text(historical['winner'])}; exact {metric_text(historical['exact'])}; Brier {historical['brier_score']:.4f}.",
             "Old run's API usage was not recorded; its dollar cost cannot be reconstructed.", ""]
    for variant, data in variants.items():
        s, u = data["summary"], data["usage"]
        lines.extend([variant, f"Published {s['published']}/{s['attempted']}; failed {s['failed_predictions']}; "
                      f"winner {metric_text(s['winner'])}; exact {metric_text(s['exact'])}",
                      f"Outcome categories: {data['failure_categories']}",
                      f"Method {metric_text(s['method'])}; round {metric_text(s['round'])}; Brier {s['brier_score']}",
                      f"API calls {u['request_count']}; input {u['input_tokens']}; output {u['output_tokens']}; "
                      f"reasoning {u['reasoning_tokens']} (included in output); cached input {u['cached_input_tokens']}",
                      f"Estimated standard USD {u['estimated_usd']}; usage complete: {u['usage_complete']}; known estimate ${u['known_estimated_usd']:.4f}",
                      f"Report: {data['report']}", ""])
    lines.append("MATCHED-FIGHT COMPARISONS (left -> right)")
    for name, comparison in paired.items():
        a, b = comparison["left"], comparison["right"]
        lines.extend([f"{name}: {comparison['fight_count']} identical matchups",
                      f"Published: {a['summary']['published']} -> {b['summary']['published']}; "
                      f"winner: {metric_text(a['summary']['winner'])} -> {metric_text(b['summary']['winner'])}",
                      f"Brier: {a['summary']['brier_score']} -> {b['summary']['brier_score']}; "
                      f"estimated USD: {a['usage']['estimated_usd']} -> {b['usage']['estimated_usd']}", ""])
    lines.extend([f"Decision: {selected}; recommended defaults: {settings}",
                  decision["selection_basis"],
                  f"Predeclared policy: {policy}", f"Medium vs none: {reasoning}",
                  f"Review vs same medium draft: {review}", f"Combined vs none: {combined}", "",
                  "The medium_direct arm shares generation with medium_review and is not billed again in this experiment.",
                  f"Known experimental estimate (none_direct + medium_review, no double counting): ${decision['experiment_known_estimated_usd']:.4f}; full estimate: {decision['experiment_estimated_usd']}",
                  "Costs include failed attempts with reported usage. Estimates use standard gpt-5.4 API rates and observed caching; they are not invoices.",
                  "Caching and output length differ between runs; a reasoning setting alone does not determine total cost.",
                  "Pricing: https://developers.openai.com/api/docs/models/gpt-5.4 (checked 2026-10-11).",
                  decision["limitation"]])
    atomic_bytes(output.with_suffix(".txt"), ("\n".join(lines) + "\n").encode("utf-8"))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results-file", type=Path, required=True)
    args = parser.parse_args()
    print(assess(args.manifest.resolve(), args.results_file.resolve()))
