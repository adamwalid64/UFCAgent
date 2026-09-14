from __future__ import annotations

import argparse
import logging
from pathlib import Path

from sqlalchemy.engine import make_url

from .db import init_db
from .pipeline import run_pipeline
from .scraper import RateLimitedPlaywrightScraper


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def _targets_canonical_database(database_url: str) -> bool:
    """Return whether a direct-write URL resolves to this repo's live SQLite DB."""

    try:
        url = make_url(database_url)
    except Exception:
        return False
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return False
    return Path(url.database).resolve() == Path("ufc_fights.db").resolve()


def main() -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Backfill or incrementally scrape UFC historical fight data into SQLite/SQLAlchemy.")
    parser.add_argument("--full", action="store_true", help="Reconcile every completed UFCStats event and fight.")
    # Incremental is the default. Keep the old flag as a no-op so existing
    # scripts do not break, but omit it from help to keep normal usage simple.
    parser.add_argument("--incremental", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--max-events", type=int, default=None, help="Limit how many events are processed per run.")
    parser.add_argument("--db-url", default="sqlite:///./ufc_fights.db", help="SQLAlchemy database URL; defaults to a local SQLite file.")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum seconds between page requests (default: 1.0).")
    parser.add_argument("--max-attempts", type=int, default=3, help="Maximum page-load attempts for each source URL (default: 3).")
    parser.add_argument("--refresh-events", type=int, default=8, help="In incremental mode, fully recheck this many recent cards in addition to every missing card (default: 8).")
    parser.add_argument("--backfill-profiles", action="store_true", help="Resume enrichment of every fighter profile that has not been scraped yet.")
    parser.add_argument("--skip-profiles", action="store_true", help="Do not enrich profiles for fighters encountered during this run.")
    parser.add_argument("--profile-max-age-days", type=int, default=90, help="Refresh profiles on reconciled cards after this many days (default: 90).")
    parser.add_argument("--skip-metadata-backfill", action="store_true", help="Do not repair missing division/method/card-order fields from event pages.")
    parser.add_argument("--profile-limit", type=int, default=None, help="Limit legacy fighter profiles enriched in this run.")
    parser.add_argument("--metadata-limit", type=int, default=None, help="Limit legacy event metadata pages enriched in this run.")
    parser.add_argument(
        "--allow-source-deletions",
        action="store_true",
        help="Allow a reviewed source correction to remove stored fights; disabled by default.",
    )
    parser.add_argument(
        "--unsafe-direct-write",
        action="store_true",
        help="Allow this low-level CLI to write directly to the canonical database (bypasses staging/validation).",
    )
    parser.add_argument("--debug", action="store_true", help="Include selector, browser-session, and traceback diagnostics.")
    args = parser.parse_args()

    if args.full and args.incremental:
        parser.error("--full and --incremental cannot be used together")
    if _targets_canonical_database(args.db_url) and not args.unsafe_direct_write:
        parser.error(
            "direct writes to ufc_fights.db are disabled; use "
            "'python -m pipeline.weekly --db ufc_fights.db' for the validated workflow "
            "or pass --unsafe-direct-write only for deliberate recovery work"
        )

    init_db(args.db_url)
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    scraper = RateLimitedPlaywrightScraper(
        delay_seconds=args.delay,
        headless=True,
        max_attempts=args.max_attempts,
    )
    mode = "full" if args.full else "incremental"
    logging.info("Starting %s scrape run against %s", mode, args.db_url)
    _, _, errors = run_pipeline(
        scraper=scraper,
        full=args.full,
        max_events=args.max_events,
        refresh_events=args.refresh_events,
        scrape_profiles=not args.skip_profiles,
        backfill_profiles=args.backfill_profiles,
        backfill_metadata=not args.skip_metadata_backfill,
        profile_limit=args.profile_limit,
        metadata_limit=args.metadata_limit,
        allow_source_deletions=args.allow_source_deletions,
        profile_max_age_days=args.profile_max_age_days,
    )
    if errors:
        logging.warning("Scrape completed with %s page-level errors:", len(errors))
        for error in errors:
            logging.warning(" - %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
