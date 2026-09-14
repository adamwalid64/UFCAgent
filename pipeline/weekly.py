"""Validated, atomic UFCStats database refresh.

This is the production entry point for recurring updates.  It never scrapes
directly into the live SQLite file: a consistent staging copy is reconciled,
validated, and then atomically promoted only when every hard check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import db as database
from .db import init_db
from .locking import InterProcessLock
from .pipeline import parse_event_date, run_pipeline
from .scraper import UFC_EVENTS_URL, RateLimitedPlaywrightScraper
from .validate import ValidationReport, validate_database


log = logging.getLogger(__name__)

_REPLACE_ATTEMPTS = 10
_REPLACE_INITIAL_DELAY_SECONDS = 0.1
_REPLACE_MAX_DELAY_SECONDS = 1.0
_WINDOWS_SHARING_ERRORS = {5, 32, 33}

# This is an allow-list of fields that exist before a bout starts.  It is
# written into each run manifest so the validator can ensure the maintained
# feature contract itself does not contain outcome or future information.
SAFE_PREDICTION_FEATURE_COLUMNS = (
    "events.event_date",
    "fights.weight_class",
    "fights.fighter_a_age",
    "fights.fighter_a_height_inches",
    "fights.fighter_a_reach_inches",
    "fights.fighter_a_stance",
    "fights.fighter_a_profile_imputed",
    "fights.fighter_a_wins",
    "fights.fighter_a_losses",
    "fights.fighter_a_draws",
    "fights.fighter_a_current_win_streak",
    "fights.fighter_a_current_loss_streak",
    "fights.fighter_a_days_since_last_fight",
    "fights.fighter_b_age",
    "fights.fighter_b_height_inches",
    "fights.fighter_b_reach_inches",
    "fights.fighter_b_stance",
    "fights.fighter_b_profile_imputed",
    "fights.fighter_b_wins",
    "fights.fighter_b_losses",
    "fights.fighter_b_draws",
    "fights.fighter_b_current_win_streak",
    "fights.fighter_b_current_loss_streak",
    "fights.fighter_b_days_since_last_fight",
    "fights.head_to_head_fight_count",
)


class AuditedScraper:
    """Record page accounting while preserving the normal scraper contract."""

    def __init__(self, scraper: RateLimitedPlaywrightScraper):
        self.scraper = scraper
        self.listing_requested = False
        self.listing_succeeded = False
        self.listing_fetched_at: str | None = None
        self.listing_urls: list[str] = []
        self.event_requested: set[str] = set()
        self.event_succeeded: set[str] = set()
        self.event_fights: dict[str, list[str]] = {}
        self.fight_requested: set[str] = set()
        self.fight_succeeded: set[str] = set()
        self.profile_requested: set[str] = set()
        self.profile_succeeded: set[str] = set()

    def crawl_event_listing(self, *args, **kwargs):
        self.listing_requested = True
        summary = self.scraper.crawl_event_listing(*args, **kwargs)
        self.listing_fetched_at = datetime.now(timezone.utc).isoformat()
        self.listing_urls = list(dict.fromkeys(getattr(summary, "event_urls", []) or []))
        self.listing_succeeded = bool(self.listing_urls) and not getattr(summary, "errors", [])
        return summary

    def crawl_event_details(self, event_url: str):
        self.event_requested.add(event_url)
        payload = self.scraper.crawl_event_details(event_url)
        source_url = str(payload.get("event_url") or event_url)
        fight_urls = list(dict.fromkeys(payload.get("fight_urls") or []))
        self.event_succeeded.add(event_url)
        self.event_fights[source_url] = fight_urls
        return payload

    def crawl_fight_card(self, fight_url: str):
        self.fight_requested.add(fight_url)
        payload = self.scraper.crawl_fight_card(fight_url)
        self.fight_succeeded.add(fight_url)
        return payload

    def crawl_fighter_profile(self, fighter_url: str):
        self.profile_requested.add(fighter_url)
        payload = self.scraper.crawl_fighter_profile(fighter_url)
        self.profile_succeeded.add(fighter_url)
        return payload

    def close(self) -> None:
        self.scraper.close()


def _database_has_fights(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            return bool(connection.execute("SELECT EXISTS(SELECT 1 FROM fights)").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def _sqlite_backup(source: Path, destination: Path) -> None:
    """Create a consistent snapshot, including databases currently using WAL."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    source_connection = sqlite3.connect(
        f"file:{source.resolve().as_posix()}?mode=ro", uri=True, timeout=30
    )
    destination_connection = sqlite3.connect(destination, timeout=30)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _code_version() -> str:
    digest = hashlib.sha256()
    package = Path(__file__).resolve().parent
    for path in sorted(package.glob("*.py"), key=lambda value: value.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _database_manifest_rows(path: Path) -> tuple[dict[str, list[str]], str | None]:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        cards: dict[str, list[str]] = defaultdict(list)
        for event_url, fight_url in connection.execute(
            """
            SELECT e.event_url,f.source_fight_url
            FROM events e LEFT JOIN fights f ON f.event_id=e.event_id
            ORDER BY e.event_url,f.card_order
            """
        ):
            if event_url is not None and fight_url is not None:
                cards[str(event_url)].append(str(fight_url))
            elif event_url is not None:
                cards.setdefault(str(event_url), [])
        latest = connection.execute("SELECT MAX(event_date) FROM events").fetchone()[0]
        return dict(cards), latest
    finally:
        connection.close()


def _build_manifest(
    path: Path,
    scraper: AuditedScraper,
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    errors: list[str],
) -> dict[str, Any]:
    stored_cards, latest_event_date = _database_manifest_rows(path)
    # Refreshed cards come from the source response.  Older unchanged cards
    # are the cumulative identities carried by the staging snapshot.
    cards = dict(stored_cards)
    cards.update(scraper.event_fights)

    event_dates = [
        parse_event_date(metadata.get("event_date"))
        for metadata in scraper.scraper._event_metadata.values()
        if metadata.get("event_date")
    ]
    source_latest = max((value for value in event_dates if value is not None), default=None)
    latest = source_latest.isoformat() if isinstance(source_latest, date) else latest_event_date

    return {
        "run_id": run_id,
        "status": "completed" if not errors else "failed",
        "code_version": _code_version(),
        "started_at": started_at,
        "finished_at": finished_at,
        "errors": [{"message": message, "resolved": False} for message in errors],
        "listing": {
            "url": UFC_EVENTS_URL,
            "fetched_at": scraper.listing_fetched_at,
            "event_urls": scraper.listing_urls,
            "latest_event_date": latest,
        },
        "event_fights": cards,
        "page_counts": {
            "listing_pages_expected": 1,
            "listing_pages_succeeded": int(scraper.listing_succeeded),
            "event_pages_expected": len(scraper.event_requested),
            "event_pages_succeeded": len(scraper.event_succeeded),
            "fight_pages_expected": len(scraper.fight_requested),
            "fight_pages_succeeded": len(scraper.fight_succeeded),
            "profile_pages_expected": len(scraper.profile_requested),
            "profile_pages_succeeded": len(scraper.profile_succeeded),
            "failed_pages": 0 if not errors else len(errors),
        },
        "feature_columns": list(SAFE_PREDICTION_FEATURE_COLUMNS),
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _append_audit(path: Path, report: ValidationReport, promoted_to: Path | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "run_id": report.run_id,
        "validated_at": report.validated_at,
        "database_sha256": report.database_sha256,
        "database_bytes": report.database_bytes,
        "ok": report.ok,
        "hard_failure_count": report.hard_failure_count,
        "warning_count": report.warning_count,
        "metrics": report.metrics,
        "promoted_to": str(promoted_to.resolve()) if promoted_to is not None else None,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _previous_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.stem}.previous{database_path.suffix}")


def _volume_identity(path: Path) -> tuple[int, str]:
    """Return enough identity to reject a non-atomic cross-volume promotion."""

    resolved = path.resolve()
    # ``st_dev`` detects POSIX mount points and normal Windows drive changes.
    # The drive/share is included because some Windows network filesystems
    # report an unhelpfully constant ``st_dev`` value across UNC shares.
    drive = os.path.splitdrive(str(resolved))[0].casefold()
    return os.stat(resolved).st_dev, drive


def _is_retryable_replace_error(error: OSError) -> bool:
    """Identify transient Windows sharing/access failures from ``os.replace``."""

    return (
        isinstance(error, PermissionError)
        or getattr(error, "winerror", None) in _WINDOWS_SHARING_ERRORS
    )


def _replace_with_retry(
    source: Path,
    destination: Path,
    *,
    attempts: int = _REPLACE_ATTEMPTS,
    initial_delay_seconds: float = _REPLACE_INITIAL_DELAY_SECONDS,
    max_delay_seconds: float = _REPLACE_MAX_DELAY_SECONDS,
) -> None:
    """Bound retries for Windows readers that briefly block an atomic replace."""

    if attempts < 1:
        raise ValueError("attempts must be at least one")
    for attempt in range(1, attempts + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if attempt == attempts or not _is_retryable_replace_error(exc):
                raise
            delay = min(
                initial_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds
            )
            log.warning(
                "Atomic replace of %s is temporarily blocked; retrying (%s/%s) in %.2fs",
                destination,
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)


def _promote(staging: Path, destination: Path) -> Path | None:
    """Atomically replace destination while retaining one recoverable version."""

    previous: Path | None = None
    backup_temp: Path | None = None
    if destination.exists():
        previous = _previous_path(destination)
        backup_temp = previous.with_name(f".{previous.name}.{uuid.uuid4().hex}.tmp")
        try:
            _sqlite_backup(destination, backup_temp)
        except Exception:
            try:
                backup_temp.unlink(missing_ok=True)
            except OSError:
                log.warning(
                    "Could not remove incomplete pre-promotion snapshot %s",
                    backup_temp,
                    exc_info=True,
                )
            raise

    try:
        # This is the commit point.  Keep both the established `.previous`
        # file and the temporary snapshot untouched until it succeeds.
        _replace_with_retry(staging, destination)
    except Exception:
        if backup_temp is not None:
            try:
                backup_temp.unlink(missing_ok=True)
            except OSError:
                log.warning(
                    "Could not remove unused pre-promotion snapshot %s",
                    backup_temp,
                    exc_info=True,
                )
        raise

    if backup_temp is not None and previous is not None:
        try:
            _replace_with_retry(backup_temp, previous)
        except OSError:
            # The validated live database is already in place.  Do not turn a
            # recovery-copy housekeeping failure into a scheduler retry that
            # might obscure that successful promotion.  The uniquely named
            # temporary snapshot remains recoverable.
            log.exception(
                "Database was promoted to %s, but previous-version rotation failed; "
                "the prior live snapshot remains at %s",
                destination,
                backup_temp,
            )
            return backup_temp
    return previous


def _refresh_database_unlocked(
    database_path: Path,
    *,
    bootstrap_from: Path | None = None,
    full: bool = False,
    delay_seconds: float = 1.0,
    refresh_events: int = 8,
    backfill_profiles: bool = True,
    backfill_metadata: bool = True,
    max_events: int | None = None,
    runs_directory: Path | None = None,
) -> tuple[int, Path, ValidationReport]:
    """Refresh, gate, and promote one SQLite database.

    Returns ``(exit_code, run_directory, validation_report)``.  On any failure,
    the staging database remains in the run directory for diagnosis or resume.
    """

    database_path = database_path.resolve()
    if database_path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("--db must name a local SQLite database file")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    run_root = (runs_directory or database_path.parent / "data_runs").resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    if _volume_identity(database_path.parent) != _volume_identity(run_root):
        raise ValueError(
            "--runs-dir must be on the same filesystem volume as --db so the "
            "validated staging database can be promoted atomically"
        )
    run_directory = run_root / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    staging = run_directory / f"{database_path.stem}.staging{database_path.suffix}"

    baseline = database_path if _database_has_fights(database_path) else None
    base = baseline
    if base is None and bootstrap_from is not None:
        bootstrap = bootstrap_from.resolve()
        if not _database_has_fights(bootstrap):
            raise ValueError(f"Bootstrap database has no fights: {bootstrap}")
        base = bootstrap
    if base is not None:
        log.info("Creating consistent staging snapshot from %s", base)
        _sqlite_backup(base, staging)

    started_at = datetime.now(timezone.utc).isoformat()
    scraper = AuditedScraper(
        RateLimitedPlaywrightScraper(delay_seconds=delay_seconds, headless=True)
    )
    previous_engine = database.engine
    previous_session_local = database.SessionLocal
    staging_engine = None
    try:
        staging_engine = init_db(_sqlite_url(staging))
        processed_events, processed_fights, errors = run_pipeline(
            scraper=scraper,
            full=full,
            max_events=max_events,
            refresh_events=refresh_events,
            scrape_profiles=True,
            backfill_profiles=backfill_profiles,
            backfill_metadata=backfill_metadata,
        )
    finally:
        try:
            # ``init_db`` stores the staging engine globally.  Drain its pool
            # before validation and Windows file replacement, including when
            # initialization or scraping raises.
            if staging_engine is not None:
                staging_engine.dispose()
            elif database.engine is not previous_engine:
                database.engine.dispose()
        finally:
            # Library callers may already have a configured database.  Do not
            # strand those globals on a staging path that is about to move.
            database.engine = previous_engine
            database.SessionLocal = previous_session_local
    finished_at = datetime.now(timezone.utc).isoformat()

    manifest = _build_manifest(
        staging,
        scraper,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        errors=errors,
    )
    manifest["processed_events"] = processed_events
    manifest["processed_fights"] = processed_fights
    manifest_path = run_directory / "manifest.json"
    report_path = run_directory / "validation.json"
    _atomic_json(manifest_path, manifest)

    report = validate_database(
        staging,
        baseline_database=baseline,
        manifest=manifest,
        promotion_gate=True,
        initial_load=baseline is None,
        temporal_policy="bout-start",
        round_required_since=date(2000, 1, 1),
        max_manifest_age_hours=48,
        required_metadata=("method_of_victory", "weight_class", "card_order"),
        feature_columns=SAFE_PREDICTION_FEATURE_COLUMNS,
        audit_target_declared=True,
    )
    _atomic_json(report_path, report.to_dict())

    if not report.ok:
        _append_audit(run_root / "audit.jsonl", report, None)
        log.error(
            "Candidate failed validation (%s hard failure(s)); live database was not changed. "
            "Staging retained at %s",
            report.hard_failure_count,
            staging,
        )
        return 1, run_directory, report

    previous = _promote(staging, database_path)
    try:
        _append_audit(run_root / "audit.jsonl", report, database_path)
    except Exception:
        # Promotion is the commit point.  Validation.json already contains the
        # durable report; a failed append must not make a scheduler retry a run
        # whose database replacement actually succeeded.
        log.exception(
            "Database promotion succeeded at %s, but the post-promotion audit "
            "append failed; validation remains available at %s",
            database_path,
            report_path,
        )
    log.info(
        "Promoted validated database to %s (events=%s fights=%s, previous=%s)",
        database_path,
        report.metrics.get("events_rows"),
        report.metrics.get("fights_rows"),
        previous,
    )
    return 0, run_directory, report


def refresh_database(
    database_path: Path,
    *,
    bootstrap_from: Path | None = None,
    full: bool = False,
    delay_seconds: float = 1.0,
    refresh_events: int = 8,
    backfill_profiles: bool = True,
    backfill_metadata: bool = True,
    max_events: int | None = None,
    runs_directory: Path | None = None,
) -> tuple[int, Path, ValidationReport]:
    """Run one complete staging/validation/promotion cycle under an OS lock."""

    resolved_database = database_path.resolve()
    lock_path = resolved_database.with_name(
        f".{resolved_database.name}.weekly.lock"
    )
    # The lock covers snapshot creation through atomic promotion and audit
    # append.  A second scheduler invocation fails immediately instead of
    # racing on staging state, `.previous`, or the live destination.
    with InterProcessLock(lock_path):
        return _refresh_database_unlocked(
            resolved_database,
            bootstrap_from=bootstrap_from,
            full=full,
            delay_seconds=delay_seconds,
            refresh_events=refresh_events,
            backfill_profiles=backfill_profiles,
            backfill_metadata=backfill_metadata,
            max_events=max_events,
            runs_directory=runs_directory,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely refresh a UFC SQLite database through staging and validation."
    )
    parser.add_argument("--db", default="ufc_fights.db", help="Live SQLite database path.")
    parser.add_argument(
        "--bootstrap-from",
        help="Existing completed scrape used only when --db has no fights.",
    )
    parser.add_argument("--full", action="store_true", help="Refresh every event and fight page.")
    parser.add_argument("--max-events", type=int, default=None, help="Limit source events for a smoke run only.")
    parser.add_argument("--refresh-events", type=int, default=8, help="Recent cards rechecked each incremental run.")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum seconds between source requests.")
    parser.add_argument(
        "--skip-profile-backfill",
        action="store_true",
        help="Do not resume enrichment of legacy fighter profiles.",
    )
    parser.add_argument(
        "--skip-metadata-backfill",
        action="store_true",
        help="Do not resume event-level metadata enrichment.",
    )
    parser.add_argument("--runs-dir", help="Directory for staging files and validation audit artifacts.")
    parser.add_argument("--debug", action="store_true", help="Enable detailed diagnostics.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Stdout keeps Windows PowerShell's durable Tee-Object log from wrapping
    # every normal INFO line as a native stderr error record.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.refresh_events < 0:
        parser.error("--refresh-events cannot be negative")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    try:
        exit_code, run_directory, report = refresh_database(
            Path(args.db),
            bootstrap_from=Path(args.bootstrap_from) if args.bootstrap_from else None,
            full=args.full,
            delay_seconds=args.delay,
            refresh_events=args.refresh_events,
            backfill_profiles=not args.skip_profile_backfill,
            backfill_metadata=not args.skip_metadata_backfill,
            max_events=args.max_events,
            runs_directory=Path(args.runs_dir) if args.runs_dir else None,
        )
    except Exception:
        log.exception("Weekly refresh failed")
        return 2

    log.info(
        "Run artifacts: %s | hard_failures=%s warnings=%s",
        run_directory,
        report.hard_failure_count,
        report.warning_count,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
