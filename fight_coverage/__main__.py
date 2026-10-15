"""Offline-safe configuration check, MCP inspection, and public coverage preview."""
import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, ROOT
from .discovery import utc
from .mcp_client import MCPClient
from .service import CoveragePipeline
from .store import Store


def event_date_argument(value):
    value = value.strip()
    try:
        utc(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "Use the actual event date in YYYY-MM-DD format (or an ISO 8601 timestamp); "
            "replace the YYYY-MM-DD placeholder."
        ) from None
    return value


def fighter_argument(value):
    value = value.strip()
    if not value or value.casefold() in {"fighter a", "fighter b"}:
        raise argparse.ArgumentTypeError(
            "Replace Fighter A / Fighter B with the fighters' real names."
        )
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("prune-cache")
    forget = commands.add_parser("forget-video")
    forget.add_argument("video_id")
    inspect = commands.add_parser("inspect-mcp")
    inspect.add_argument("--notes", type=Path, default=ROOT / "YOUTUBE_MCP_NOTES.md")
    preview = commands.add_parser("preview")
    preview.add_argument("fighter_a", type=fighter_argument, help="First fighter's real name")
    preview.add_argument("fighter_b", type=fighter_argument, help="Second fighter's real name")
    preview.add_argument("--event-date", required=True, type=event_date_argument,
                         help="Actual event date (YYYY-MM-DD) or ISO 8601 cutoff timestamp")
    preview.add_argument("--event-name", default="")
    preview.add_argument("--event-alias", action="append", default=[],
                         help="Alternate card name or verified event number; repeat for multiple aliases")
    preview.add_argument("--refresh-discovery", action="store_true",
                         help="Check YouTube for new uploads now; reuse cached transcripts")
    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
        if args.command == "check":
            import os
            print(f"Coverage enabled: {cfg.enabled}; registered channels: {len(cfg.channels)}")
            print(f"YouTube API key present: {bool(os.getenv(cfg.api_key_env))}; MCP configured: {cfg.mcp is not None}")
            print(f"Whisper enabled: {cfg.whisper_enabled}; cache: {cfg.cache_dir}")
            print(f"Date-only evidence cutoff: midnight in {cfg.event_timezone}")
            for channel in cfg.channels:
                print(f"  {channel.name}: {channel.channel_id or channel.handle}; type={channel.type}; active={channel.active}; "
                      f"weight={channel.credibility_weight:g}; transcript permission recorded: {channel.transcript_access_authorized}")
        elif args.command == "inspect-mcp":
            if cfg.mcp is None:
                raise ValueError("No MCP server configured; see YOUTUBE_MCP_NOTES.md")
            inventory = MCPClient(cfg.mcp, cfg.request_timeout).inspect()
            # Record schemas, not server URLs, launch arguments, credentials, or transcript data.
            inventory = [{key: item.get(key) for key in ("name", "description", "inputSchema", "outputSchema")}
                         for item in inventory]
            marker = "<!-- BEGIN MCP INVENTORY -->"
            previous = args.notes.read_text(encoding="utf-8") if args.notes.exists() else "# YouTube MCP notes\n"
            previous = re.sub(re.escape(marker) + r".*?<!-- END MCP INVENTORY -->", "", previous, flags=re.S)
            section = (f"\n{marker}\nActual tools/list inventory inspected {datetime.now(timezone.utc).isoformat()}:\n\n"
                       + "```json\n" + json.dumps(inventory, indent=2) + "\n```\n<!-- END MCP INVENTORY -->\n")
            args.notes.write_text(previous.rstrip() + "\n" + section, encoding="utf-8")
            print(f"Recorded {len(inventory)} actual tool(s) in {args.notes}")
        elif args.command == "preview":
            result = CoveragePipeline(cfg, refresh_discovery=args.refresh_discovery).get_fight_coverage(
                dict(fighter_a=args.fighter_a, fighter_b=args.fighter_b, event_name=args.event_name,
                     event_aliases=args.event_alias), args.event_date)
            print(json.dumps(result.public(), indent=2))
        else:
            store = Store(cfg.cache_dir, cfg.retention_days)
            if args.command == "forget-video":
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", args.video_id):
                    raise ValueError("Invalid video ID")
                store.forget(args.video_id)
            print("Cache maintenance complete; no network or model calls.")
    except Exception as exc:
        # External providers can include secrets in exception strings.
        print(f"Coverage command failed ({type(exc).__name__}); check configuration and MCP notes.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
