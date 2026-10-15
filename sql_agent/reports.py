"""Readable prediction reports saved incrementally under a designated folder."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .database import ROOT
from .evaluation import require_finalized


DEFAULT_OUTPUT_DIR = ROOT / "predictions"


def usage_text(result: dict) -> str:
    usage = result.get("model_usage")
    if not usage:
        return ""
    cost = usage.get("estimated_usd")
    return (f"\nModel usage: {usage['request_count']} calls; {usage['input_tokens']} input tokens; "
            f"{usage['output_tokens']} output tokens ({usage['reasoning_tokens']} reasoning tokens included)."
            + (f" Estimated standard API cost: ${cost:.4f}." if cost is not None else
               f" Full cost unavailable; known usage estimate: ${usage.get('known_estimated_usd', 0):.4f}.")
            + (" Usage is incomplete." if not usage['usage_complete'] else "") + "\n")


def format_prediction(result: dict) -> str:
    title = f"{result['fighter_a']} vs {result['fighter_b']}"
    if result.get("weight_class"):
        title += f" | {result['weight_class']}"
    if "error" in result:
        return f"\n{title}\nPrediction failed: {result['error']}\n" + usage_text(result)
    ending = f"{result['predicted_method']}, round {result['predicted_round']}"
    lines = ["", title, f"Pick: {result['predicted_winner']}",
             f"Confidence: {result['win_probability']:.1%}", f"Predicted ending: {ending}",
             f"Why: {result['summary']}"]
    lines.extend(f"  - {factor}" for factor in result["key_factors"])
    for key, label in (("opponent_quality_analysis", "Level of opposition"),
                       ("style_history_analysis", "History against relevant styles")):
        if key in result:
            lines.append(f"\n{label}:")
            for side in ("a", "b"):
                finding = result[key][side]
                lines.append(f"  {result[f'fighter_{side}']}: {finding['analysis']}")
    if result.get("counterargument"):
        lines.append(f"\nCase for the other fighter: {result['counterargument']}")
    if result.get("confidence_justification"):
        lines.append(f"Confidence assessment: {result['confidence_justification']}")
    evidence = result.get("matchup_evidence", {})
    references = []
    for side in ("a", "b"):
        cited = {fid for key in ("opponent_quality_analysis", "style_history_analysis")
                 for fid in result.get(key, {}).get(side, {}).get("fight_ids", [])}
        for bout in evidence.get(side, {}).get("bouts", []):
            if bout["fight_id"] in cited:
                record = bout["opponent_prior_record"]
                references.append(f"  [{bout['fight_id']}] {bout['date']} {result[f'fighter_{side}']} vs {bout['opponent_name']}: "
                                  f"{bout['result']}, {bout['method']}; opponent entering {record['win']}-{record['loss']}-{record['draw']}, "
                                  f"derived Elo {bout['opponent_prior_elo']:.0f}, {bout['opponent_tier']} relative tier"
                                  + (" (provisional)" if bout["strength_provisional"] else ""))
    if references:
        lines.extend(["\nCited historical evidence:", *references])
    sources = result.get("external_coverage", {}).get("sources", [])
    if sources:
        lines.append("\nAnalyst commentary sources (third-party opinion, paraphrased):")
        for source in sources:
            name = source.get("writer", source.get("channel", source.get("name", "Source")))
            lines.append(f"  {name}: {source['url']} | layer: {source['layer']}"
                         + (" (cached)" if source['cache_hit'] else ""))
    for source in result.get("external_coverage", {}).get("signals", []):
        lines.append(f"  {source['name']} | {source['type']} | credibility weight {source['credibility_weight']:g} | "
                     + (f"Explicit pick: {source['pick']}" if source["pick"] else "No explicit pick (pick=null)"))
        lines.extend(f"    {signal['analysis']}" for signal in source["signals"])
    market = result.get("market_context")
    if market:
        lines.append("\nCentral market comparison (excluded from analyst sentiment):")
        for comparison in market["comparisons"]:
            details = [f"model {comparison['uncalibrated_model_probability']:.1%} (uncalibrated)"]
            if "market_probability_no_vig" in comparison:
                details.append(f"market without margin {comparison['market_probability_no_vig']:.1%}")
            if "public_pick_share" in comparison:
                details.append(f"community picks {comparison['public_pick_share']:.1%}")
            lines.append(f"  {comparison['fighter']}: " + "; ".join(details))
        lines.extend(f"  Note: {caveat}" for caveat in market["caveats"])
    if result.get("evaluation"):
        review = result["evaluation"]
        lines.append(f"\nFinal evaluation: {review['status']} after {review['review_rounds']} review(s), "
                     f"{review['revision_count']} revision(s). {review['summary']}")
    if result.get("model_config"):
        config = result["model_config"]
        lines.append(f"Models: analysis {config['analysis_model']}; final review {config['review_model'] or 'off'}; "
                     f"reasoning effort {config.get('reasoning_effort') or 'model default'}.")
        if "max_model_calls" in config:
            lines.append(f"Cost controls: research {config['research_mode']}; at most {config['max_model_calls']} "
                         f"model request(s) per fight; {config['max_output_tokens']} generated tokens per request.")
    lines.extend(f"Note: {caveat}" for caveat in result["caveats"])
    return "\n".join(lines) + "\n" + usage_text(result)


class PredictionReport:
    """One timestamped report per run; each finished bout is saved immediately."""

    def __init__(self, output_dir: str | Path, title: str, cutoff: str, *, card: dict | None = None,
                 analysis_mode: str = "recorded_prediction", provenance: dict | None = None):
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        generated = datetime.now(timezone.utc)
        slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:80] or "prediction"
        self.path = directory / f"{generated:%Y%m%dT%H%M%SZ}-{slug}-{uuid.uuid4().hex[:8]}.txt"
        lines = [title, f"Generated: {generated.isoformat()}",
                 f"Evidence cutoff: strictly before {cutoff}",
                 "Confidence percentages are uncalibrated model estimates."]
        if card:
            lines.extend([f"Event date: {card['event_date']}",
                          f"Schedule fetched: {card['last_scraped_at']}"])
            if card.get("event_url"):
                lines.append(f"Source: {card['event_url']}")
            if card.get("live_remaining"):
                lines.append(f"Posted results excluded: {card.get('completed_fights', 0)}")
            for fight in card.get("excluded_fights", []):
                lines.append(f"Manually excluded: {fight['fighter_a_name']} vs {fight['fighter_b_name']}")
            lines.extend(f"Note: {caveat}" for caveat in card["caveats"])
        with self.path.open("x", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        self.ledger_path = self.path.with_suffix(".ledger.jsonl")
        with self.ledger_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "run", "schema_version": 1, "title": title,
                                     "generated_at": generated.isoformat(), "as_of_date": cutoff,
                                     "analysis_mode": analysis_mode, "provenance": provenance or {},
                                     "card": card}, ensure_ascii=False, allow_nan=False) + "\n")
        if analysis_mode == "retrospective_replay":
            self.append("Analysis mode: retrospective replay generated after the event; not a prospective test.\n"
                        "The SQL cutoff excludes target-day results; model prior knowledge cannot be ruled out.\n")

    def _ledger(self, record: dict) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def append(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def add_prediction(self, result: dict) -> None:
        require_finalized(result)
        self.append(format_prediction(result))
        self._ledger({"kind": "prediction", "recorded_at": datetime.now(timezone.utc).isoformat(),
                      "prediction": result})

    def finish(self, result: dict, *, save_json: bool = False) -> Path | None:
        for prediction in result.get("predictions", [result]):
            require_finalized(prediction)
        for fight in result.get("skipped_fights", []):
            self.append(f"\nSkipped: {fight['fighter_a']} vs {fight['fighter_b']}: {fight['reason']}\n")
        if "predictions" in result:
            self.append(f"\nRun complete: {len(result['predictions'])} fight reports, "
                        f"{result.get('failed_fights', 0)} failed predictions.\n")
        else:
            self.append("\nRun complete: 1 fight report.\n")
        self._ledger({"kind": "complete", "recorded_at": datetime.now(timezone.utc).isoformat(),
                      "failed_fights": result.get("failed_fights", 0),
                      "skipped_fights": result.get("skipped_fights", [])})
        if save_json:
            path = self.path.with_suffix(".json")
            payload = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            return path
        return None
