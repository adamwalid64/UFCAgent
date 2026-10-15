from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from dotenv import load_dotenv

from .cards import load_next_card, load_remaining_card, predict_card
from .database import MAX_QUERIES, ROOT, normalize_date
from .graph import SQLAnalysisAgent, DEFAULT_REVIEW_MODE, DEFAULT_REASONING_EFFORT, DEFAULT_RESEARCH_MODE
from .reports import DEFAULT_OUTPUT_DIR, PredictionReport, format_prediction


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Read-only, point-in-time UFC matchup analysis")
    parser.add_argument("fighter_a", nargs="?", help="First fighter for a single-fight test; omit both for the next card")
    parser.add_argument("fighter_b", nargs="?", help="Second fighter (exact name or UFCStats ID)")
    parser.add_argument("--as-of-date", help="YYYY-MM-DD; only earlier fights are visible (default: today)")
    parser.add_argument("--db", default=ROOT / "ufc_fights.db")
    parser.add_argument("--prompt", default=ROOT / "prompts/sql_agent_system.md")
    parser.add_argument("--model", default=None, help="Override OPENAI_MODEL")
    parser.add_argument("--review-model", default=os.getenv("OPENAI_REVIEW_MODEL"), help="Separate reviewer model (default: gpt-5.4 in a fresh context)")
    parser.add_argument("--review", choices=("on", "off"), default=os.getenv("SQL_AGENT_REVIEW_MODE", DEFAULT_REVIEW_MODE), help="Paid model self-review (default: off); deterministic validation always runs")
    parser.add_argument("--research", choices=("on", "off"), default=os.getenv("SQL_AGENT_RESEARCH_MODE", DEFAULT_RESEARCH_MODE), help="Model-driven SQL research (default: on); off selects compact direct drafting")
    parser.add_argument("--max-model-calls", type=int, help="Per-fight request cap including retries: default 24 with research; 8 for review only; 1 for direct drafting")
    parser.add_argument("--max-output-tokens", type=int, help="Per-request generated-token ceiling, including reasoning (default: 12000; actual usage is billed)")
    parser.add_argument("--review-prompt", default=ROOT / "prompts/sql_agent_review.md", help="Final review rubric")
    parser.add_argument("--max-revisions", type=int, choices=range(4), default=2, help="Maximum correction passes after validation fails; optional model review uses the same limit")
    parser.add_argument("--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh"), default=os.getenv("OPENAI_REASONING_EFFORT"), help=f"Internal reasoning effort for compatible models; default: {DEFAULT_REASONING_EFFORT} with gpt-5.4")
    parser.add_argument("--max-queries", type=int, default=int(os.getenv("MAX_QUERIES", MAX_QUERIES)))
    parser.add_argument("--row-limit", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=5.0, help="Seconds per SQL query / path search")
    parser.add_argument("--model-timeout", type=float, default=120.0, help="Seconds per model request, including reasoning and review")
    parser.add_argument("--rounds", type=int, choices=(3, 5), help="Scheduled distance for a single fight (default: assume 3)")
    parser.add_argument("--json", action="store_true", help="Output full structured results and SQL audit instead of terminal summaries")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Folder for timestamped prediction reports (default: repo's predictions folder)")
    parser.add_argument("--market-context", help="Local odds/community-picks JSON; read only by the final central node")
    parser.add_argument("--event", help="Predict only remaining fights on this UFCStats event (name, URL, or source ID); fetches current status")
    parser.add_argument("--event-name", help="Card name for finding full-card analyst videos in a single-fight test")
    parser.add_argument("--exclude-fight", action="append", default=[], help="With --event, skip a fight by exact fighter name or fight ID; repeat as needed for bouts already underway")
    args = parser.parse_args(argv)
    if bool(args.fighter_a) != bool(args.fighter_b):
        parser.error("Provide both fighter names, or omit both to predict the next card")
    if not args.fighter_a and args.rounds is not None:
        parser.error("--rounds is for single-fight tests; card bouts use their own scheduled distance")
    if args.event and args.fighter_a:
        parser.error("Use --event or two fighter names, not both")
    if args.event_name and not args.fighter_a:
        parser.error("--event-name is for single-fight tests; card runs already supply the event name")
    if args.exclude_fight and not args.event:
        parser.error("--exclude-fight requires --event")

    def progress(message: str):
        print(message, file=sys.stderr, flush=True)

    report = None
    try:
        card = (load_remaining_card(args.event, exclude_fights=args.exclude_fight) if args.event
                else load_next_card(args.db) if not args.fighter_a else None)
        cutoff = normalize_date(args.as_of_date or (card["event_date"] if card else None))
        if card and cutoff > card["event_date"]:
            raise ValueError("Card analysis cutoff cannot be after the card date")
        title = card["event_name"] if card else f"{args.fighter_a} vs {args.fighter_b}"
        report = PredictionReport(args.output_dir, title, cutoff, card=card)
        progress(f"Prediction report: {report.path}")

        def publish_prediction(prediction):
            report.add_prediction(prediction)
            if not args.json:
                print(format_prediction(prediction), flush=True)

        if card and args.event and not card["fights"]:
            result = {**card, "predictions": [], "failed_fights": 0}
            report.append("\nNo remaining fights to predict.\n")
            report.finish(result, save_json=args.json)
            if args.json:
                print(json.dumps(result))
            else:
                print(f"{card['event_name']}: no remaining fights to predict.")
            return 0
        agent = SQLAnalysisAgent(args.db, model=args.model, prompt_path=args.prompt,
                                 max_queries=args.max_queries, row_limit=args.row_limit, timeout=args.timeout,
                                 review_model=args.review_model, review_prompt_path=args.review_prompt,
                                 max_revisions=args.max_revisions, reasoning_effort=args.reasoning_effort,
                                 model_timeout=args.model_timeout, review_mode=args.review, research_mode=args.research,
                                 max_model_calls=args.max_model_calls, max_output_tokens=args.max_output_tokens,
                                 market_context_path=args.market_context)
        if card:
            if not args.json:
                print(f"{card['event_name']} | {card['event_date']} | {len(card['fights'])} fights", flush=True)
                if args.event:
                    print(f"Skipped {card['completed_fights']} fights with posted results and {len(card['excluded_fights'])} manual exclusions.", flush=True)
                for caveat in card["caveats"]:
                    print(f"Note: {caveat}", flush=True)
            result = predict_card(agent, card, as_of_date=cutoff, on_progress=progress,
                                  refresh_remaining=(lambda: load_remaining_card(card["event_url"])) if args.event else None,
                                  on_result=publish_prediction)
        else:
            progress(f"Analyzing {args.fighter_a} vs {args.fighter_b}...")
            try:
                result = agent.predict(args.fighter_a, args.fighter_b, cutoff,
                                       scheduled_rounds=args.rounds or 3,
                                       rounds_basis="user supplied" if args.rounds else "assumed standard distance",
                                       event_name=args.event_name,
                                       on_progress=progress)
            except Exception as exc:
                result = {"fighter_a": args.fighter_a, "fighter_b": args.fighter_b, "as_of_date": cutoff,
                          "error": str(exc), "error_type": type(exc).__name__,
                          "model_usage": getattr(exc, "model_usage", None),
                          "review_history": getattr(exc, "review_history", [])}
            publish_prediction(result)
        json_path = report.finish(result, save_json=args.json)
        if json_path:
            progress(f"JSON report: {json_path}")
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 1 if result.get("failed_fights", 0) or "error" in result else 0
    except Exception as exc:
        if report is not None:
            try:
                report.append(f"\nRun incomplete: {exc}\n")
            except OSError:
                pass
        print(f"SQL agent: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if report is not None:
            try:
                report.append("\nRun interrupted; completed fight reports were retained.\n")
            except OSError:
                pass
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
