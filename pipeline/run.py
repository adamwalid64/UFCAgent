from __future__ import annotations

import argparse
import logging

from .db import init_db
from .pipeline import run_pipeline
from .scraper import RateLimitedPlaywrightScraper


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def main() -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Backfill or incrementally scrape UFC historical fight data into SQLite/SQLAlchemy.")
    parser.add_argument("--full", action="store_true", help="Run a full backfill through the history of UFC events.")
    parser.add_argument("--incremental", action="store_true", help="Scrape only the newest events and resume on existing data.")
    parser.add_argument("--max-events", type=int, default=None, help="Limit how many events are processed per run.")
    parser.add_argument("--db-url", default="sqlite:///./ufc_fights.db", help="SQLAlchemy database URL; defaults to a local SQLite file.")
    args = parser.parse_args()

    if bool(args.full) == bool(args.incremental):
        parser.error("Exactly one mode must be selected: --full or --incremental")

    init_db(args.db_url)
    scraper = RateLimitedPlaywrightScraper(delay_seconds=1.5, headless=True)
    mode = "full" if args.full else "incremental"
    logging.info("Starting %s scrape run against %s", mode, args.db_url)
    _, _, errors = run_pipeline(scraper=scraper, full=args.full, max_events=args.max_events)
    if errors:
        logging.warning("Scrape completed with %s page-level errors:", len(errors))
        for error in errors:
            logging.warning(" - %s", error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
