"""Read-only validation gate for scraped UFC databases.

The validator is intentionally separate from ingestion and promotion.  A weekly
sync should build a staging database and a cumulative source manifest, then run::

    python -m pipeline.validate --db ufc_staging.db --baseline-db ufc_fights.db \
        --run-manifest runs/<run-id>/manifest.json --promotion-gate \
        --temporal-policy bout-start --report runs/<run-id>/validation.json \
        --audit-log runs/audit.jsonl

Only an exit status of zero permits the caller to atomically promote staging.
The manifest format used by the completeness gate is documented by
``_validate_manifest`` below.  Validation never changes the database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


EXPECTED_COLUMNS: dict[str, set[str]] = {
    "events": {
        "event_id", "event_name", "event_date", "event_location", "event_url",
        "source_fight_count", "last_scraped_at", "created_at", "updated_at",
    },
    "fighters": {
        "fighter_id", "fighter_name", "fighter_nickname", "date_of_birth",
        "height_inches", "reach_inches", "stance", "profile_url", "fights",
        "wins", "losses", "draws", "no_contests", "current_win_streak",
        "current_loss_streak", "significant_strikes_landed",
        "significant_strikes_attempted", "takedowns_landed",
        "takedowns_attempted", "control_time_seconds", "last_fight_date",
        "profile_scraped_at", "created_at", "updated_at",
    },
    "fights": {
        "fight_id", "event_id", "fighter_a_id", "fighter_a_name", "weight_class",
        "card_order", "event_method_code", "stats_available", "fighter_a_age",
        "fighter_a_height_inches", "fighter_a_reach_inches", "fighter_a_stance",
        "fighter_a_profile_imputed",
        "fighter_a_wins", "fighter_a_losses", "fighter_a_draws",
        "fighter_a_current_win_streak", "fighter_a_current_loss_streak",
        "fighter_a_significant_strikes_landed",
        "fighter_a_significant_strikes_attempted", "fighter_a_takedowns_landed",
        "fighter_a_takedowns_attempted", "fighter_a_control_time_seconds",
        "fighter_a_total_strikes_landed", "fighter_a_total_strikes_attempted",
        "fighter_a_knockdowns", "fighter_a_submission_attempts",
        "fighter_a_reversals", "fighter_a_significant_head_strikes_landed",
        "fighter_a_significant_head_strikes_attempted",
        "fighter_a_significant_body_strikes_landed",
        "fighter_a_significant_body_strikes_attempted",
        "fighter_a_significant_leg_strikes_landed",
        "fighter_a_significant_leg_strikes_attempted",
        "fighter_a_significant_distance_strikes_landed",
        "fighter_a_significant_distance_strikes_attempted",
        "fighter_a_significant_clinch_strikes_landed",
        "fighter_a_significant_clinch_strikes_attempted",
        "fighter_a_significant_ground_strikes_landed",
        "fighter_a_significant_ground_strikes_attempted",
        "fighter_a_days_since_last_fight", "fighter_b_id",
        "fighter_b_name", "fighter_b_age", "fighter_b_height_inches",
        "fighter_b_reach_inches", "fighter_b_stance", "fighter_b_profile_imputed",
        "fighter_b_wins",
        "fighter_b_losses", "fighter_b_draws", "fighter_b_current_win_streak",
        "fighter_b_current_loss_streak", "fighter_b_significant_strikes_landed",
        "fighter_b_significant_strikes_attempted", "fighter_b_takedowns_landed",
        "fighter_b_takedowns_attempted", "fighter_b_control_time_seconds",
        "fighter_b_total_strikes_landed", "fighter_b_total_strikes_attempted",
        "fighter_b_knockdowns", "fighter_b_submission_attempts",
        "fighter_b_reversals", "fighter_b_significant_head_strikes_landed",
        "fighter_b_significant_head_strikes_attempted",
        "fighter_b_significant_body_strikes_landed",
        "fighter_b_significant_body_strikes_attempted",
        "fighter_b_significant_leg_strikes_landed",
        "fighter_b_significant_leg_strikes_attempted",
        "fighter_b_significant_distance_strikes_landed",
        "fighter_b_significant_distance_strikes_attempted",
        "fighter_b_significant_clinch_strikes_landed",
        "fighter_b_significant_clinch_strikes_attempted",
        "fighter_b_significant_ground_strikes_landed",
        "fighter_b_significant_ground_strikes_attempted",
        "fighter_b_days_since_last_fight",
        "winner_fighter_id", "winner_name", "method_of_victory", "method_detail",
        "result", "referee", "time_format", "ending_round", "ending_time",
        "head_to_head_fight_count", "source_event_url", "source_fight_url",
        "last_scraped_at", "created_at", "updated_at",
    },
    "fight_rounds": {
        "round_stat_id", "fight_id", "round_number", "fighter_id", "opponent_id",
        "knockdowns", "significant_strikes_landed",
        "significant_strikes_attempted", "total_strikes_landed",
        "total_strikes_attempted", "takedowns_landed", "takedowns_attempted",
        "submission_attempts", "reversals", "control_time_seconds",
        "significant_head_strikes_landed", "significant_head_strikes_attempted",
        "significant_body_strikes_landed", "significant_body_strikes_attempted",
        "significant_leg_strikes_landed", "significant_leg_strikes_attempted",
        "significant_distance_strikes_landed", "significant_distance_strikes_attempted",
        "significant_clinch_strikes_landed", "significant_clinch_strikes_attempted",
        "significant_ground_strikes_landed", "significant_ground_strikes_attempted",
    },
}

REQUIRED_FOREIGN_KEYS: dict[str, set[tuple[str, str, str]]] = {
    "fights": {
        ("event_id", "events", "event_id"),
        ("fighter_a_id", "fighters", "fighter_id"),
        ("fighter_b_id", "fighters", "fighter_id"),
        ("winner_fighter_id", "fighters", "fighter_id"),
    },
    "fight_rounds": {
        ("fight_id", "fights", "fight_id"),
        ("fighter_id", "fighters", "fighter_id"),
        ("opponent_id", "fighters", "fighter_id"),
    },
}

ROUND_METRICS = (
    "knockdowns",
    "significant_strikes_landed",
    "significant_strikes_attempted",
    "total_strikes_landed",
    "total_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "submission_attempts",
    "reversals",
    "control_time_seconds",
    "significant_head_strikes_landed",
    "significant_head_strikes_attempted",
    "significant_body_strikes_landed",
    "significant_body_strikes_attempted",
    "significant_leg_strikes_landed",
    "significant_leg_strikes_attempted",
    "significant_distance_strikes_landed",
    "significant_distance_strikes_attempted",
    "significant_clinch_strikes_landed",
    "significant_clinch_strikes_attempted",
    "significant_ground_strikes_landed",
    "significant_ground_strikes_attempted",
)

FIGHTER_BOUT_HISTORY_VIEW = "fighter_bout_history"
HISTORY_PREFIGHT_FIELDS = (
    "age",
    "height_inches",
    "reach_inches",
    "stance",
    "profile_imputed",
    "wins",
    "losses",
    "draws",
    "current_win_streak",
    "current_loss_streak",
    "days_since_last_fight",
)
HISTORY_VIEW_REQUIRED_COLUMNS = frozenset(
    {
        "fighter_bout_sequence",
        "event_bout_sequence",
        "source_card_order",
        "fight_id",
        "event_id",
        "event_name",
        "event_date",
        "event_location",
        "weight_class",
        "fighter_slot",
        "fighter_id",
        "fighter_name",
        "opponent_id",
        "opponent_name",
        "prefight_head_to_head_fight_count",
        "bout_outcome",
        "bout_result",
        "bout_winner_fighter_id",
        "bout_winner_name",
        "bout_method_of_victory",
        "bout_stats_available",
        "source_event_url",
        "source_fight_url",
        "bout_last_scraped_at",
    }
    | {f"prefight_{field}" for field in HISTORY_PREFIGHT_FIELDS}
    | {f"opponent_prefight_{field}" for field in HISTORY_PREFIGHT_FIELDS}
    | {f"bout_{field}" for field in ROUND_METRICS}
    | {f"opponent_bout_{field}" for field in ROUND_METRICS}
)

POST_FIGHT_SUFFIXES = {
    "winner_fighter_id", "winner_name", "method_of_victory", "method_detail",
    "event_method_code", "stats_available", "result", "referee", "time_format",
    "ending_round", "ending_time",
}
POST_FIGHT_SIDE_SUFFIXES = {
    "significant_strikes_landed", "significant_strikes_attempted",
    "total_strikes_landed", "total_strikes_attempted", "takedowns_landed",
    "takedowns_attempted", "control_time_seconds", "knockdowns",
    "submission_attempts", "reversals",
    "significant_head_strikes_landed", "significant_head_strikes_attempted",
    "significant_body_strikes_landed", "significant_body_strikes_attempted",
    "significant_leg_strikes_landed", "significant_leg_strikes_attempted",
    "significant_distance_strikes_landed", "significant_distance_strikes_attempted",
    "significant_clinch_strikes_landed", "significant_clinch_strikes_attempted",
    "significant_ground_strikes_landed", "significant_ground_strikes_attempted",
}
FINAL_FIGHTER_AGGREGATES = {
    "fights", "wins", "losses", "draws", "no_contests", "current_win_streak",
    "current_loss_streak", "significant_strikes_landed",
    "significant_strikes_attempted", "takedowns_landed", "takedowns_attempted",
    "control_time_seconds", "last_fight_date",
}

ANATOMICAL_SPLITS = ("head", "body", "leg")
POSITIONAL_SPLITS = ("distance", "clinch", "ground")
ALL_SPLITS = ANATOMICAL_SPLITS + POSITIONAL_SPLITS


@dataclass
class CheckResult:
    check_id: str
    status: str
    severity: str
    message: str
    count: int = 0
    samples: list[Any] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    database: str
    validated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: str | None = None
    database_sha256: str | None = None
    database_bytes: int | None = None
    checks: list[CheckResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def pass_check(self, check_id: str, message: str, **details: Any) -> None:
        self.checks.append(CheckResult(check_id, "PASS", "hard", message, details=details))

    def fail(
        self,
        check_id: str,
        message: str,
        count: int = 1,
        samples: Iterable[Any] = (),
        **details: Any,
    ) -> None:
        self.checks.append(
            CheckResult(check_id, "FAIL", "hard", message, count, list(samples)[:5], details)
        )

    def warn(
        self,
        check_id: str,
        message: str,
        count: int = 1,
        samples: Iterable[Any] = (),
        **details: Any,
    ) -> None:
        self.checks.append(
            CheckResult(check_id, "WARN", "warning", message, count, list(samples)[:5], details)
        )

    @property
    def hard_failure_count(self) -> int:
        return sum(check.status == "FAIL" for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.status == "WARN" for check in self.checks)

    @property
    def ok(self) -> bool:
        return self.hard_failure_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "validated_at": self.validated_at,
            "run_id": self.run_id,
            "database_sha256": self.database_sha256,
            "database_bytes": self.database_bytes,
            "ok": self.ok,
            "hard_failure_count": self.hard_failure_count,
            "warning_count": self.warning_count,
            "metrics": self.metrics,
            "checks": [asdict(check) for check in self.checks],
        }


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchmany(5)]


def _count(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    return int(connection.execute(sql, params).fetchone()[0])


def _record_count_check(
    report: ValidationReport,
    connection: sqlite3.Connection,
    check_id: str,
    sql: str,
    failure_message: str,
    sample_sql: str | None = None,
) -> int:
    count = _count(connection, sql)
    if count:
        report.fail(check_id, failure_message, count, _rows(connection, sample_sql or sql))
    else:
        report.pass_check(check_id, failure_message.replace(" found", " absent"))
    return count


def _validate_schema(connection: sqlite3.Connection, report: ValidationReport) -> bool:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing_tables = sorted(EXPECTED_COLUMNS.keys() - tables)
    if missing_tables:
        report.fail("schema.tables", "Required tables are missing", len(missing_tables), missing_tables)
        return False
    report.pass_check("schema.tables", "All required tables are present")

    missing_columns: dict[str, list[str]] = {}
    for table, expected in EXPECTED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        missing = sorted(expected - actual)
        if missing:
            missing_columns[table] = missing
    if missing_columns:
        report.fail(
            "schema.columns",
            "Required columns are missing",
            sum(map(len, missing_columns.values())),
            [missing_columns],
        )
        return False
    report.pass_check("schema.columns", "All required columns are present")

    missing_foreign_keys: list[tuple[str, str, str, str]] = []
    for table, expected in REQUIRED_FOREIGN_KEYS.items():
        actual = {(row[3], row[2], row[4]) for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')}
        missing_foreign_keys.extend((table, *key) for key in sorted(expected - actual))
    if missing_foreign_keys:
        report.fail(
            "schema.foreign_keys",
            "Required foreign-key declarations are missing",
            len(missing_foreign_keys),
            missing_foreign_keys,
        )
    else:
        report.pass_check("schema.foreign_keys", "Required foreign-key declarations are present")

    unique_round_key = False
    for index in connection.execute("PRAGMA index_list('fight_rounds')"):
        if not index[2]:
            continue
        columns = tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{index[1]}")'))
        if columns == ("fight_id", "round_number", "fighter_id"):
            unique_round_key = True
            break
    if unique_round_key:
        report.pass_check("schema.round_unique_key", "Round natural key is unique")
    else:
        report.fail(
            "schema.round_unique_key",
            "Missing unique key on (fight_id, round_number, fighter_id)",
        )
    return not missing_columns


def _validate_fighter_bout_history(
    connection: sqlite3.Connection,
    report: ValidationReport,
) -> None:
    view = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?",
        (FIGHTER_BOUT_HISTORY_VIEW,),
    ).fetchone()
    if view is None:
        report.fail(
            "schema.fighter_bout_history",
            "Required fighter_bout_history view is missing",
        )
        return
    report.pass_check(
        "schema.fighter_bout_history",
        "Required fighter_bout_history view is present",
    )

    try:
        actual_columns = {
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{FIGHTER_BOUT_HISTORY_VIEW}")'
            )
        }
    except sqlite3.Error as exc:
        report.fail(
            "schema.fighter_bout_history_columns",
            f"fighter_bout_history cannot be inspected: {exc}",
        )
        return
    missing_columns = sorted(HISTORY_VIEW_REQUIRED_COLUMNS - actual_columns)
    if missing_columns:
        report.fail(
            "schema.fighter_bout_history_columns",
            "fighter_bout_history is missing required key or time-domain columns",
            len(missing_columns),
            missing_columns,
        )
        return
    report.pass_check(
        "schema.fighter_bout_history_columns",
        "fighter_bout_history exposes all required key and time-domain columns",
    )

    history_rows = _count(
        connection, f'SELECT COUNT(*) FROM "{FIGHTER_BOUT_HISTORY_VIEW}"'
    )
    report.metrics["fighter_bout_history_rows"] = history_rows

    cardinality_cte = f"""
        WITH perspective_counts AS (
            SELECT fight_id, COUNT(*) AS perspective_rows
            FROM {FIGHTER_BOUT_HISTORY_VIEW}
            GROUP BY fight_id
        ),
        all_ids AS (
            SELECT fight_id FROM fights
            UNION
            SELECT fight_id FROM perspective_counts
        ),
        perspective_cardinality AS (
            SELECT
                ids.fight_id,
                CASE WHEN f.fight_id IS NULL THEN 0 ELSE 1 END AS canonical_rows,
                COALESCE(pc.perspective_rows, 0) AS perspective_rows
            FROM all_ids AS ids
            LEFT JOIN fights AS f ON f.fight_id=ids.fight_id
            LEFT JOIN perspective_counts AS pc ON pc.fight_id=ids.fight_id
        )
    """
    _record_count_check(
        report,
        connection,
        "fighter_bout_history.cardinality",
        cardinality_cte
        + """
        SELECT COUNT(*) FROM perspective_cardinality
        WHERE canonical_rows<>1 OR perspective_rows<>2
        """,
        "Perspective-cardinality violations found",
        cardinality_cte
        + """
        SELECT fight_id,canonical_rows,perspective_rows
        FROM perspective_cardinality
        WHERE canonical_rows<>1 OR perspective_rows<>2
        LIMIT 5
        """,
    )

    participant_cte = f"""
        WITH participant_matches AS (
            SELECT
                f.fight_id,
                f.fighter_a_id,
                f.fighter_b_id,
                SUM(CASE WHEN h.fighter_slot='a'
                              AND h.fighter_id=f.fighter_a_id
                              AND h.opponent_id=f.fighter_b_id
                         THEN 1 ELSE 0 END) AS fighter_a_rows,
                SUM(CASE WHEN h.fighter_slot='b'
                              AND h.fighter_id=f.fighter_b_id
                              AND h.opponent_id=f.fighter_a_id
                         THEN 1 ELSE 0 END) AS fighter_b_rows
            FROM fights AS f
            LEFT JOIN {FIGHTER_BOUT_HISTORY_VIEW} AS h ON h.fight_id=f.fight_id
            GROUP BY f.fight_id,f.fighter_a_id,f.fighter_b_id
        )
    """
    _record_count_check(
        report,
        connection,
        "fighter_bout_history.participants",
        participant_cte
        + """
        SELECT COUNT(*) FROM participant_matches
        WHERE fighter_a_rows<>1 OR fighter_b_rows<>1
        """,
        "Fight perspectives with missing or incorrect participants found",
        participant_cte
        + """
        SELECT fight_id,fighter_a_id,fighter_b_id,fighter_a_rows,fighter_b_rows
        FROM participant_matches
        WHERE fighter_a_rows<>1 OR fighter_b_rows<>1
        LIMIT 5
        """,
    )

    expected_outcome = """
        CASE
            WHEN lower(coalesce(f.result, ''))='draw' THEN 'draw'
            WHEN lower(coalesce(f.result, '')) IN ('no_contest', 'no contest', 'nc')
                THEN 'no_contest'
            WHEN f.winner_fighter_id=h.fighter_id THEN 'win'
            WHEN f.winner_fighter_id IN (f.fighter_a_id, f.fighter_b_id) THEN 'loss'
            ELSE NULL
        END
    """
    outcome_where = f"""
        h.bout_result IS NOT f.result
        OR h.bout_winner_fighter_id IS NOT f.winner_fighter_id
        OR h.bout_outcome IS NOT ({expected_outcome})
    """
    _record_count_check(
        report,
        connection,
        "fighter_bout_history.outcomes",
        f"""
        SELECT COUNT(*)
        FROM {FIGHTER_BOUT_HISTORY_VIEW} AS h
        JOIN fights AS f ON f.fight_id=h.fight_id
        WHERE {outcome_where}
        """,
        "Fighter-perspective outcome disagreements found",
        f"""
        SELECT
            h.fight_id,h.fighter_id,h.bout_outcome,
            ({expected_outcome}) AS expected_outcome,
            h.bout_result,f.result,
            h.bout_winner_fighter_id,f.winner_fighter_id
        FROM {FIGHTER_BOUT_HISTORY_VIEW} AS h
        JOIN fights AS f ON f.fight_id=h.fight_id
        WHERE {outcome_where}
        LIMIT 5
        """,
    )


def _validate_integrity(connection: sqlite3.Connection, report: ValidationReport) -> None:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity == ["ok"]:
        report.pass_check("sqlite.integrity", "SQLite integrity_check passed")
    else:
        report.fail("sqlite.integrity", "SQLite integrity_check failed", len(integrity), integrity)

    violations = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
    if violations:
        report.fail(
            "sqlite.foreign_key_check",
            "Foreign-key violations found",
            len(violations),
            violations,
        )
    else:
        report.pass_check("sqlite.foreign_key_check", "No foreign-key violations found")


def _validate_core_rows(connection: sqlite3.Connection, report: ValidationReport) -> None:
    for table in EXPECTED_COLUMNS:
        value = _count(connection, f'SELECT COUNT(*) FROM "{table}"')
        report.metrics[f"{table}_rows"] = value
        if value == 0:
            report.fail(f"coverage.{table}_nonempty", f"{table} is empty")
        else:
            report.pass_check(f"coverage.{table}_nonempty", f"{table} has {value:,} rows")

    min_date, max_date = connection.execute(
        "SELECT MIN(event_date), MAX(event_date) FROM events"
    ).fetchone()
    report.metrics.update({"min_event_date": min_date, "max_event_date": max_date})

    checks = (
        (
            "identity.event_urls",
            "SELECT COUNT(*) FROM events WHERE event_url IS NULL OR TRIM(event_url)=''",
            "Events with missing source URLs found",
            "SELECT event_id,event_name,event_date FROM events WHERE event_url IS NULL OR TRIM(event_url)='' LIMIT 5",
        ),
        (
            "identity.duplicate_event_urls",
            "SELECT COUNT(*) FROM (SELECT event_url FROM events GROUP BY event_url HAVING COUNT(*)>1)",
            "Duplicate event source URLs found",
            "SELECT event_url,COUNT(*) AS occurrences FROM events GROUP BY event_url HAVING COUNT(*)>1 LIMIT 5",
        ),
        (
            "identity.fight_urls",
            "SELECT COUNT(*) FROM fights WHERE source_fight_url IS NULL OR TRIM(source_fight_url)=''",
            "Fights with missing source URLs found",
            "SELECT fight_id,fighter_a_name,fighter_b_name FROM fights WHERE source_fight_url IS NULL OR TRIM(source_fight_url)='' LIMIT 5",
        ),
        (
            "identity.duplicate_fight_urls",
            "SELECT COUNT(*) FROM (SELECT source_fight_url FROM fights GROUP BY source_fight_url HAVING COUNT(*)>1)",
            "Duplicate fight source URLs found",
            "SELECT source_fight_url,COUNT(*) AS occurrences FROM fights GROUP BY source_fight_url HAVING COUNT(*)>1 LIMIT 5",
        ),
        (
            "coverage.events_have_fights",
            "SELECT COUNT(*) FROM events e WHERE NOT EXISTS (SELECT 1 FROM fights f WHERE f.event_id=e.event_id)",
            "Events without stored fights found",
            "SELECT event_id,event_name,event_date,event_url FROM events e WHERE NOT EXISTS (SELECT 1 FROM fights f WHERE f.event_id=e.event_id) LIMIT 5",
        ),
    )
    for args in checks:
        _record_count_check(report, connection, *args)

    _record_count_check(
        report,
        connection,
        "coverage.event_fight_counts",
        """
        SELECT COUNT(*) FROM events e
        WHERE e.source_fight_count IS NOT NULL
          AND e.source_fight_count<>(SELECT COUNT(*) FROM fights f WHERE f.event_id=e.event_id)
        """,
        "Stored event source-fight counts disagree with loaded fights",
        """
        SELECT e.event_id,e.event_name,e.source_fight_count,
               (SELECT COUNT(*) FROM fights f WHERE f.event_id=e.event_id) AS stored_fights
        FROM events e
        WHERE e.source_fight_count IS NOT NULL
          AND e.source_fight_count<>(SELECT COUNT(*) FROM fights f WHERE f.event_id=e.event_id)
        LIMIT 5
        """,
    )
    missing_source_counts = _count(
        connection, "SELECT COUNT(*) FROM events WHERE source_fight_count IS NULL"
    )
    if missing_source_counts:
        report.warn(
            "coverage.event_source_counts_missing",
            "Events lack source fight-count audit metadata",
            missing_source_counts,
            _rows(
                connection,
                "SELECT event_id,event_name,event_date FROM events WHERE source_fight_count IS NULL LIMIT 5",
            ),
        )
    else:
        report.pass_check("coverage.event_source_counts_missing", "All events retain source fight counts")

    _record_count_check(
        report,
        connection,
        "coverage.stats_available",
        """
        SELECT COUNT(*) FROM fights f
        WHERE (f.stats_available=1 AND NOT EXISTS (
                  SELECT 1 FROM fight_rounds r WHERE r.fight_id=f.fight_id
              ))
           OR (f.stats_available=0 AND EXISTS (
                  SELECT 1 FROM fight_rounds r WHERE r.fight_id=f.fight_id
              ))
        """,
        "stats_available flags disagree with stored round data",
        """
        SELECT f.fight_id,f.stats_available,
               EXISTS(SELECT 1 FROM fight_rounds r WHERE r.fight_id=f.fight_id) AS has_rounds
        FROM fights f
        WHERE (f.stats_available=1 AND NOT EXISTS (
                  SELECT 1 FROM fight_rounds r WHERE r.fight_id=f.fight_id
              ))
           OR (f.stats_available=0 AND EXISTS (
                  SELECT 1 FROM fight_rounds r WHERE r.fight_id=f.fight_id
              ))
        LIMIT 5
        """,
    )
    _record_count_check(
        report,
        connection,
        "coverage.card_order",
        """
        SELECT COUNT(*) FROM (
            SELECT event_id,COUNT(*) AS fights,COUNT(card_order) AS populated,
                   COUNT(DISTINCT card_order) AS distinct_orders,
                   MIN(card_order) AS first_order,MAX(card_order) AS last_order
            FROM fights GROUP BY event_id
            HAVING populated<>fights OR distinct_orders<>fights
                OR first_order<>1 OR last_order<>fights
        )
        """,
        "Card order must be the complete unique range 1..fight_count for every event",
        """
        SELECT event_id,COUNT(*) AS fights,COUNT(card_order) AS populated,
               COUNT(DISTINCT card_order) AS distinct_orders,
               MIN(card_order) AS first_order,MAX(card_order) AS last_order
        FROM fights GROUP BY event_id
        HAVING populated<>fights OR distinct_orders<>fights
            OR first_order<>1 OR last_order<>fights
        LIMIT 5
        """,
    )

    bad_result = """
        SELECT COUNT(*) FROM fights
        WHERE result IS NULL OR result NOT IN ('completed','draw','no_contest')
    """
    _record_count_check(
        report,
        connection,
        "domain.result",
        bad_result,
        "Invalid or missing fight results found",
        "SELECT fight_id,result FROM fights WHERE result IS NULL OR result NOT IN ('completed','draw','no_contest') LIMIT 5",
    )

    bad_winners = """
        SELECT COUNT(*) FROM fights
        WHERE (result='completed' AND (
                  winner_name IS NULL OR winner_name NOT IN (fighter_a_name,fighter_b_name)
                  OR winner_fighter_id IS NULL
                  OR winner_fighter_id NOT IN (fighter_a_id,fighter_b_id)
              ))
           OR (result IN ('draw','no_contest') AND
               (winner_name IS NOT NULL OR winner_fighter_id IS NOT NULL))
    """
    _record_count_check(
        report,
        connection,
        "semantics.winner",
        bad_winners,
        "Winner identity/result contradictions found",
        """
        SELECT fight_id,result,fighter_a_id,fighter_a_name,fighter_b_id,fighter_b_name,
               winner_fighter_id,winner_name
        FROM fights
        WHERE (result='completed' AND (
                  winner_name IS NULL OR winner_name NOT IN (fighter_a_name,fighter_b_name)
                  OR winner_fighter_id IS NULL
                  OR winner_fighter_id NOT IN (fighter_a_id,fighter_b_id)
              ))
           OR (result IN ('draw','no_contest') AND
               (winner_name IS NOT NULL OR winner_fighter_id IS NOT NULL))
        LIMIT 5
        """,
    )

    _record_count_check(
        report,
        connection,
        "semantics.participants",
        "SELECT COUNT(*) FROM fights WHERE fighter_a_id=fighter_b_id OR TRIM(fighter_a_name)='' OR TRIM(fighter_b_name)=''",
        "Invalid fight participants found",
        "SELECT fight_id,fighter_a_id,fighter_b_id,fighter_a_name,fighter_b_name FROM fights WHERE fighter_a_id=fighter_b_id OR TRIM(fighter_a_name)='' OR TRIM(fighter_b_name)='' LIMIT 5",
    )


def _validate_rounds(
    connection: sqlite3.Connection,
    report: ValidationReport,
    round_required_since: date,
) -> None:
    _record_count_check(
        report,
        connection,
        "rounds.two_sided",
        """
        SELECT COUNT(*) FROM (
            SELECT fight_id,round_number,COUNT(*) AS rows,
                   COUNT(DISTINCT fighter_id) AS fighters
            FROM fight_rounds GROUP BY fight_id,round_number
            HAVING rows<>2 OR fighters<>2
        )
        """,
        "Fight-rounds without exactly two distinct fighter rows found",
        """
        SELECT fight_id,round_number,COUNT(*) AS rows,
               COUNT(DISTINCT fighter_id) AS fighters
        FROM fight_rounds GROUP BY fight_id,round_number
        HAVING rows<>2 OR fighters<>2 LIMIT 5
        """,
    )
    _record_count_check(
        report,
        connection,
        "rounds.participant_mapping",
        """
        SELECT COUNT(*) FROM fight_rounds r JOIN fights f ON f.fight_id=r.fight_id
        WHERE NOT ((r.fighter_id=f.fighter_a_id AND r.opponent_id=f.fighter_b_id)
                OR (r.fighter_id=f.fighter_b_id AND r.opponent_id=f.fighter_a_id))
        """,
        "Round fighter/opponent mappings inconsistent with fight participants found",
        """
        SELECT r.fight_id,r.round_number,r.fighter_id,r.opponent_id,
               f.fighter_a_id,f.fighter_b_id
        FROM fight_rounds r JOIN fights f ON f.fight_id=r.fight_id
        WHERE NOT ((r.fighter_id=f.fighter_a_id AND r.opponent_id=f.fighter_b_id)
                OR (r.fighter_id=f.fighter_b_id AND r.opponent_id=f.fighter_a_id))
        LIMIT 5
        """,
    )
    _record_count_check(
        report,
        connection,
        "rounds.sequence",
        """
        SELECT COUNT(*) FROM (
            SELECT fight_id,MIN(round_number) AS first_round,
                   MAX(round_number) AS last_round,COUNT(DISTINCT round_number) AS rounds
            FROM fight_rounds GROUP BY fight_id
            HAVING first_round<>1 OR rounds<>last_round
        )
        """,
        "Non-contiguous or non-positive round sequences found",
        """
        SELECT fight_id,MIN(round_number) AS first_round,
               MAX(round_number) AS last_round,COUNT(DISTINCT round_number) AS rounds
        FROM fight_rounds GROUP BY fight_id
        HAVING first_round<>1 OR rounds<>last_round LIMIT 5
        """,
    )
    _record_count_check(
        report,
        connection,
        "rounds.ending_round",
        """
        SELECT COUNT(*) FROM (
            SELECT f.fight_id,f.ending_round,MAX(r.round_number) AS last_round
            FROM fights f JOIN fight_rounds r ON r.fight_id=f.fight_id
            GROUP BY f.fight_id
            HAVING f.ending_round IS NULL OR f.ending_round<>last_round
        )
        """,
        "Ending-round values disagree with stored round sequences",
        """
        SELECT f.fight_id,f.ending_round,MAX(r.round_number) AS last_round
        FROM fights f JOIN fight_rounds r ON r.fight_id=f.fight_id
        GROUP BY f.fight_id
        HAVING f.ending_round IS NULL OR f.ending_round<>last_round LIMIT 5
        """,
    )

    bad_round_stats = """
        SELECT COUNT(*) FROM fight_rounds
        WHERE knockdowns<0
           OR significant_strikes_landed<0
           OR significant_strikes_attempted<significant_strikes_landed
           OR total_strikes_landed<significant_strikes_landed
           OR total_strikes_attempted<significant_strikes_attempted
           OR total_strikes_attempted<total_strikes_landed
           OR takedowns_landed<0 OR takedowns_attempted<takedowns_landed
           OR submission_attempts<0 OR reversals<0 OR control_time_seconds<0
    """
    _record_count_check(
        report,
        connection,
        "rounds.stat_domains",
        bad_round_stats,
        "Impossible round-stat values found",
        """
        SELECT * FROM fight_rounds
        WHERE knockdowns<0
           OR significant_strikes_landed<0
           OR significant_strikes_attempted<significant_strikes_landed
           OR total_strikes_landed<significant_strikes_landed
           OR total_strikes_attempted<significant_strikes_attempted
           OR total_strikes_attempted<total_strikes_landed
           OR takedowns_landed<0 OR takedowns_attempted<takedowns_landed
           OR submission_attempts<0 OR reversals<0 OR control_time_seconds<0
        LIMIT 5
        """,
    )

    missing_sql = """
        SELECT f.fight_id,e.event_name,e.event_date,f.fighter_a_name,f.fighter_b_name
        FROM fights f JOIN events e ON e.event_id=f.event_id
        WHERE NOT EXISTS (SELECT 1 FROM fight_rounds r WHERE r.fight_id=f.fight_id)
    """
    missing = [dict(row) for row in connection.execute(missing_sql)]
    modern = [row for row in missing if date.fromisoformat(row["event_date"]) >= round_required_since]
    historical = [row for row in missing if date.fromisoformat(row["event_date"]) < round_required_since]
    report.metrics["fights_without_rounds"] = len(missing)
    if modern:
        report.fail(
            "rounds.modern_coverage",
            f"Fights since {round_required_since.isoformat()} are missing round data",
            len(modern),
            modern,
        )
    else:
        report.pass_check(
            "rounds.modern_coverage",
            f"All fights since {round_required_since.isoformat()} have round data",
        )
    if historical:
        report.warn(
            "rounds.historical_source_gaps",
            "Older fights lack UFCStats round tables; retained as a source-availability warning",
            len(historical),
            historical,
        )
    else:
        report.pass_check("rounds.historical_source_gaps", "No older round-data gaps found")

    _validate_control_time_bounds(connection, report)


def _parse_clock(value: str | None) -> int | None:
    match = re.fullmatch(r"\s*(\d+):(\d{2})\s*", value or "")
    if not match or int(match.group(2)) >= 60:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _round_lengths(time_format: str | None) -> list[int] | None:
    match = re.search(r"\((\d+(?:-\d+)*)\)", time_format or "")
    if not match:
        return None
    return [int(value) * 60 for value in match.group(1).split("-")]


def _validate_control_time_bounds(connection: sqlite3.Connection, report: ValidationReport) -> None:
    rows = connection.execute(
        """
        SELECT f.fight_id,f.time_format,f.ending_round,f.ending_time,
               r.round_number,r.fighter_id,r.control_time_seconds
        FROM fights f JOIN fight_rounds r ON r.fight_id=f.fight_id
        ORDER BY f.fight_id,r.round_number,r.fighter_id
        """
    )
    grouped: dict[tuple[str, int], list[int | None]] = defaultdict(list)
    metadata: dict[str, tuple[str | None, int | None, str | None]] = {}
    for row in rows:
        grouped[(row["fight_id"], row["round_number"])].append(row["control_time_seconds"])
        metadata[row["fight_id"]] = (row["time_format"], row["ending_round"], row["ending_time"])

    invalid: list[dict[str, Any]] = []
    unbounded_fights: set[str] = set()
    for (fight_id, round_number), controls in grouped.items():
        time_format, ending_round, ending_time = metadata[fight_id]
        lengths = _round_lengths(time_format)
        if lengths is None or round_number > len(lengths):
            unbounded_fights.add(fight_id)
            continue
        elapsed = lengths[round_number - 1]
        if round_number == ending_round:
            parsed_end = _parse_clock(ending_time)
            if parsed_end is None:
                invalid.append({"fight_id": fight_id, "reason": "invalid ending_time"})
                continue
            elapsed = parsed_end
        known = [value for value in controls if value is not None]
        if any(value > elapsed for value in known) or (len(known) == 2 and sum(known) > elapsed):
            invalid.append(
                {
                    "fight_id": fight_id,
                    "round_number": round_number,
                    "elapsed_seconds": elapsed,
                    "control_seconds": controls,
                }
            )
    if invalid:
        report.fail(
            "rounds.control_time_bounds",
            "Control time exceeds elapsed round time or ending time is invalid",
            len(invalid),
            invalid,
        )
    else:
        report.pass_check("rounds.control_time_bounds", "Control times fit within known round durations")
    if unbounded_fights:
        report.warn(
            "rounds.unbounded_time_formats",
            "Control-time bounds cannot be checked for source formats without round lengths",
            len(unbounded_fights),
            sorted(unbounded_fights),
        )


def _validate_fight_domains(connection: sqlite3.Connection, report: ValidationReport) -> None:
    clauses: list[str] = []
    for side in ("a", "b"):
        prefix = f"fighter_{side}_"
        clauses.extend(
            [
                f"COALESCE({prefix}knockdowns,0)<0",
                f"COALESCE({prefix}significant_strikes_landed,0)<0",
                f"COALESCE({prefix}significant_strikes_attempted,0)<COALESCE({prefix}significant_strikes_landed,0)",
                f"COALESCE({prefix}total_strikes_landed,0)<COALESCE({prefix}significant_strikes_landed,0)",
                f"COALESCE({prefix}total_strikes_attempted,0)<COALESCE({prefix}significant_strikes_attempted,0)",
                f"COALESCE({prefix}total_strikes_attempted,0)<COALESCE({prefix}total_strikes_landed,0)",
                f"COALESCE({prefix}takedowns_landed,0)<0",
                f"COALESCE({prefix}takedowns_attempted,0)<COALESCE({prefix}takedowns_landed,0)",
                f"COALESCE({prefix}submission_attempts,0)<0",
                f"COALESCE({prefix}reversals,0)<0",
                f"COALESCE({prefix}control_time_seconds,0)<0",
            ]
        )
    where = " OR ".join(clauses)
    _record_count_check(
        report,
        connection,
        "fights.stat_domains",
        f"SELECT COUNT(*) FROM fights WHERE {where}",
        "Impossible fight-total values found",
        f"SELECT fight_id FROM fights WHERE {where} LIMIT 5",
    )

    invalid_times: list[dict[str, Any]] = []
    for row in connection.execute("SELECT fight_id,ending_round,ending_time FROM fights"):
        if row["ending_round"] is None or row["ending_round"] < 1 or _parse_clock(row["ending_time"]) is None:
            invalid_times.append(dict(row))
    if invalid_times:
        report.fail(
            "fights.ending_values",
            "Invalid ending round/time values found",
            len(invalid_times),
            invalid_times,
        )
    else:
        report.pass_check("fights.ending_values", "Ending round/time values are valid")


def _split_identity_clause(prefix: str) -> str:
    columns = [
        f"{prefix}significant_{position}_strikes_{kind}"
        for position in ALL_SPLITS
        for kind in ("landed", "attempted")
    ]
    all_null = " AND ".join(f"{column} IS NULL" for column in columns)
    all_present = " AND ".join(f"{column} IS NOT NULL" for column in columns)
    identities: list[str] = [
        f"{prefix}significant_strikes_landed IS NULL",
        f"{prefix}significant_strikes_attempted IS NULL",
    ]
    identities.extend(
        condition
        for position in ALL_SPLITS
        for condition in (
            f"{prefix}significant_{position}_strikes_landed<0",
            f"{prefix}significant_{position}_strikes_attempted<"
            f"{prefix}significant_{position}_strikes_landed",
        )
    )
    for positions in (ANATOMICAL_SPLITS, POSITIONAL_SPLITS):
        for kind in ("landed", "attempted"):
            split_sum = "+".join(
                f"{prefix}significant_{position}_strikes_{kind}" for position in positions
            )
            identities.append(f"({split_sum})<>{prefix}significant_strikes_{kind}")
    # An entirely NULL set is a supported legacy/source-unavailable state. Any
    # partial set is corrupt, as is a complete set which fails either partition.
    return f"NOT ({all_null}) AND (NOT ({all_present}) OR {' OR '.join(identities)})"


def _validate_split_identities(connection: sqlite3.Connection, report: ValidationReport) -> None:
    fight_sql = f"""
        SELECT fight_id,'a' AS side FROM fights WHERE {_split_identity_clause('fighter_a_')}
        UNION ALL
        SELECT fight_id,'b' AS side FROM fights WHERE {_split_identity_clause('fighter_b_')}
    """
    bad_fights = [dict(row) for row in connection.execute(fight_sql)]
    if bad_fights:
        report.fail(
            "splits.fight_identities",
            "Fight strike splits are partial or do not reconcile to significant-strike totals",
            len(bad_fights),
            bad_fights,
        )
    else:
        report.pass_check(
            "splits.fight_identities",
            "Populated fight strike splits reconcile across anatomy and position",
        )

    round_sql = f"""
        SELECT fight_id,round_number,fighter_id
        FROM fight_rounds WHERE {_split_identity_clause('')}
    """
    bad_rounds = [dict(row) for row in connection.execute(round_sql)]
    if bad_rounds:
        report.fail(
            "splits.round_identities",
            "Round strike splits are partial or do not reconcile to significant-strike totals",
            len(bad_rounds),
            bad_rounds,
        )
    else:
        report.pass_check(
            "splits.round_identities",
            "Populated round strike splits reconcile across anatomy and position",
        )


def _validate_totals(connection: sqlite3.Connection, report: ValidationReport) -> None:
    aggregate_parts = []
    for metric in ROUND_METRICS:
        if metric == "control_time_seconds":
            aggregate_parts.append(
                "CASE WHEN COUNT(control_time_seconds)=0 THEN NULL "
                "ELSE SUM(control_time_seconds) END AS control_time_seconds"
            )
        else:
            aggregate_parts.append(f"SUM({metric}) AS {metric}")
    aggregate_sql = ",".join(aggregate_parts)
    comparisons = []
    for side, alias in (("a", "ra"), ("b", "rb")):
        comparisons.extend(
            f"NOT (f.fighter_{side}_{metric} IS {alias}.{metric})" for metric in ROUND_METRICS
        )
    sql = f"""
        WITH totals AS (
            SELECT fight_id,fighter_id,{aggregate_sql}
            FROM fight_rounds GROUP BY fight_id,fighter_id
        )
        SELECT f.fight_id
        FROM fights f
        JOIN totals ra ON ra.fight_id=f.fight_id AND ra.fighter_id=f.fighter_a_id
        JOIN totals rb ON rb.fight_id=f.fight_id AND rb.fighter_id=f.fighter_b_id
        WHERE {' OR '.join(comparisons)}
    """
    mismatches = [dict(row) for row in connection.execute(sql)]
    if mismatches:
        report.fail(
            "totals.fight_vs_rounds",
            "Fight totals do not equal the sum of per-round values",
            len(mismatches),
            mismatches,
        )
    else:
        report.pass_check("totals.fight_vs_rounds", "Fight totals equal per-round sums")


def _new_state() -> dict[str, Any]:
    return {
        "fights": 0,
        "w": 0,
        "l": 0,
        "d": 0,
        "nc": 0,
        "ws": 0,
        "ls": 0,
        "last": None,
        "significant_strikes_landed": 0,
        "significant_strikes_attempted": 0,
        "takedowns_landed": 0,
        "takedowns_attempted": 0,
        "control_time_seconds": 0,
    }


def _outcome(result: str, winner_name: str | None, fighter_name: str) -> str:
    if result == "draw":
        return "d"
    if result == "no_contest":
        return "nc"
    return "w" if winner_name == fighter_name else "l"


def _apply_fight(
    state: dict[str, Any],
    outcome: str,
    event_date: date,
    stats: dict[str, Any],
) -> None:
    state["fights"] += 1
    if outcome == "w":
        state["w"] += 1
        state["ws"] += 1
        state["ls"] = 0
    elif outcome == "l":
        state["l"] += 1
        state["ls"] += 1
        state["ws"] = 0
    elif outcome == "d":
        state["d"] += 1
        state["ws"] = 0
        state["ls"] = 0
    else:
        state["nc"] += 1
    for metric in (
        "significant_strikes_landed", "significant_strikes_attempted",
        "takedowns_landed", "takedowns_attempted", "control_time_seconds",
    ):
        state[metric] += stats.get(metric) or 0
    state["last"] = event_date


def _prefight_values(row: sqlite3.Row, side: str) -> tuple[Any, ...]:
    return tuple(
        row[f"fighter_{side}_{name}"]
        for name in (
            "wins", "losses", "draws", "current_win_streak",
            "current_loss_streak", "days_since_last_fight",
        )
    )


def _expected_prefight(state: dict[str, Any], event_date: date) -> tuple[Any, ...]:
    rest = None if state["last"] is None else (event_date - state["last"]).days
    return state["w"], state["l"], state["d"], state["ws"], state["ls"], rest


def _side_stats(row: sqlite3.Row, side: str) -> dict[str, Any]:
    return {
        metric: row[f"fighter_{side}_{metric}"]
        for metric in (
            "significant_strikes_landed", "significant_strikes_attempted",
            "takedowns_landed", "takedowns_attempted", "control_time_seconds",
        )
    }


def _fight_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT f.rowid AS insertion_order,f.*,e.event_date
            FROM fights f JOIN events e ON e.event_id=f.event_id
            ORDER BY e.event_date,f.rowid
            """
        )
    )


def _compare_prefight(
    row: sqlite3.Row,
    side: str,
    state: dict[str, Any],
    event_date: date,
    field_counts: dict[str, int],
) -> bool:
    labels = ("wins", "losses", "draws", "win_streak", "loss_streak", "rest_days")
    actual = _prefight_values(row, side)
    expected = _expected_prefight(state, event_date)
    bad = False
    for label, actual_value, expected_value in zip(labels, actual, expected):
        if actual_value != expected_value:
            field_counts[label] += 1
            bad = True
    return bad


def _replay_rows(
    rows: Sequence[sqlite3.Row],
) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, int]]:
    states: dict[str, dict[str, Any]] = defaultdict(_new_state)
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    bad_fights: set[str] = set()
    field_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        event_date = date.fromisoformat(row["event_date"])
        pair = tuple(sorted((row["fighter_a_id"], row["fighter_b_id"])))
        for side in ("a", "b"):
            fighter_id = row[f"fighter_{side}_id"]
            if _compare_prefight(row, side, states[fighter_id], event_date, field_counts):
                bad_fights.add(row["fight_id"])
        if row["head_to_head_fight_count"] != pair_counts[pair]:
            field_counts["head_to_head"] += 1
            bad_fights.add(row["fight_id"])
        for side in ("a", "b"):
            fighter_id = row[f"fighter_{side}_id"]
            fighter_name = row[f"fighter_{side}_name"]
            _apply_fight(
                states[fighter_id],
                _outcome(row["result"], row["winner_name"], fighter_name),
                event_date,
                _side_stats(row, side),
            )
        pair_counts[pair] += 1
    return states, bad_fights, dict(field_counts)


def _events_in_bout_order(rows: Sequence[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    by_event: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_event[row["event_id"]].append(row)

    def card_order(row: sqlite3.Row) -> tuple[int, int]:
        try:
            # UFCStats uses 1 for the main event. Larger numbers therefore
            # happened earlier, and must be replayed first.
            value = int(row["card_order"])
        except (TypeError, ValueError):
            value = -1
        return -value, int(row["insertion_order"])

    return [
        sorted(by_event[event_id], key=card_order)
        for event_id in sorted(
            by_event,
            key=lambda value: (
                by_event[value][0]["event_date"],
                value,
            ),
        )
    ]


def _same_day_event_conflicts(event_rows: Sequence[Sequence[sqlite3.Row]]) -> list[dict[str, Any]]:
    appearances: dict[tuple[str, str], set[str]] = defaultdict(set)
    for rows in event_rows:
        for row in rows:
            for side in ("a", "b"):
                appearances[(row["event_date"], row[f"fighter_{side}_id"])].add(row["event_id"])
    return [
        {"event_date": event_date, "fighter_id": fighter_id, "event_ids": sorted(event_ids)}
        for (event_date, fighter_id), event_ids in appearances.items()
        if len(event_ids) > 1
    ]


def _validate_temporal(
    connection: sqlite3.Connection,
    report: ValidationReport,
    temporal_policy: str,
) -> None:
    rows = _fight_rows(connection)
    insertion_rows = sorted(rows, key=lambda row: row["insertion_order"])
    inversions: list[dict[str, Any]] = []
    previous: date | None = None
    for row in insertion_rows:
        current = date.fromisoformat(row["event_date"])
        if previous is not None and current < previous:
            inversions.append(
                {
                    "fight_id": row["fight_id"],
                    "event_date": current.isoformat(),
                    "previous_date": previous.isoformat(),
                }
            )
        previous = current
    if inversions:
        report.warn(
            "chronology.insertion_order",
            "Row insertion order moves backward in time, as can occur during historical gap repair; it is not used for bout replay",
            len(inversions),
            inversions,
        )
    else:
        report.pass_check("chronology.insertion_order", "Row insertion order happens to be chronological but is not authoritative")

    events = _events_in_bout_order(rows)
    bout_rows = [row for event in events for row in event]
    bout_states, bout_bad, bout_fields = _replay_rows(bout_rows)

    conflicts = _same_day_event_conflicts(events)
    if conflicts and temporal_policy != "off":
        report.fail(
            "chronology.same_day_events",
            "A fighter appears in multiple events on one date, but event times are unavailable",
            len(conflicts),
            conflicts,
        )
    elif conflicts:
        report.warn(
            "chronology.same_day_events",
            "Same-day cross-event chronology conflicts were ignored by policy",
            len(conflicts),
            conflicts,
        )
    else:
        report.pass_check("chronology.same_day_events", "No fighter needs cross-event ordering on the same date")

    if temporal_policy == "bout-start":
        if bout_bad:
            report.fail(
                "chronology.bout_start_replay",
                "Stored pre-fight features do not match event_date + card_order DESC bout chronology",
                len(bout_bad),
                sorted(bout_bad),
                mismatches_by_field=bout_fields,
            )
        else:
            report.pass_check(
                "chronology.bout_start_replay",
                "Pre-fight features use only information available before each bout",
            )
    elif temporal_policy == "ingestion":
        _, ingestion_bad, ingestion_fields = _replay_rows(rows)
        if ingestion_bad:
            report.fail(
                "chronology.ingestion_replay",
                "Stored pre-fight features do not match legacy row-insertion replay",
                len(ingestion_bad),
                sorted(ingestion_bad),
                mismatches_by_field=ingestion_fields,
            )
        else:
            report.pass_check("chronology.ingestion_replay", "Pre-fight features replay in legacy insertion order")
    elif temporal_policy == "event-start":
        _validate_event_start_semantics(events, report)
    else:
        report.warn(
            "chronology.bout_start_replay",
            "Bout-start temporal validation was explicitly disabled",
        )

    # Final aggregates and streaks also depend on true within-card order, so
    # they are always checked against authoritative bout chronology.
    _validate_fighter_aggregates(connection, report, bout_states)


def _validate_fighter_aggregates(
    connection: sqlite3.Connection,
    report: ValidationReport,
    states: dict[str, dict[str, Any]],
) -> None:
    bad: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in connection.execute("SELECT * FROM fighters"):
        fighter_id = row["fighter_id"]
        seen.add(fighter_id)
        state = states.get(fighter_id)
        if state is None:
            bad.append({"fighter_id": fighter_id, "reason": "fighter has no fights"})
            continue
        expected = {
            "fights": state["fights"], "wins": state["w"], "losses": state["l"],
            "draws": state["d"], "no_contests": state["nc"],
            "current_win_streak": state["ws"], "current_loss_streak": state["ls"],
            "significant_strikes_landed": state["significant_strikes_landed"],
            "significant_strikes_attempted": state["significant_strikes_attempted"],
            "takedowns_landed": state["takedowns_landed"],
            "takedowns_attempted": state["takedowns_attempted"],
            "control_time_seconds": state["control_time_seconds"],
            "last_fight_date": state["last"].isoformat() if state["last"] else None,
        }
        differences = {
            key: {"stored": row[key], "expected": value}
            for key, value in expected.items()
            if row[key] != value
        }
        if differences:
            bad.append({"fighter_id": fighter_id, "differences": differences})
    for fighter_id in states.keys() - seen:
        bad.append({"fighter_id": fighter_id, "reason": "fight participant missing from fighters"})
    if bad:
        report.fail(
            "aggregates.fighters",
            "Fighter aggregates do not equal the complete fight history",
            len(bad),
            bad,
        )
    else:
        report.pass_check("aggregates.fighters", "Fighter aggregates match fight history")


def _validate_event_start_semantics(
    ordered_events: Sequence[Sequence[sqlite3.Row]],
    report: ValidationReport,
) -> None:
    states: dict[str, dict[str, Any]] = defaultdict(_new_state)
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    bad_fights: set[str] = set()
    field_counts: dict[str, int] = defaultdict(int)
    for event_rows in ordered_events:
        for row in event_rows:
            event_date = date.fromisoformat(row["event_date"])
            pair = tuple(sorted((row["fighter_a_id"], row["fighter_b_id"])))
            for side in ("a", "b"):
                if _compare_prefight(
                    row, side, states[row[f"fighter_{side}_id"]], event_date, field_counts
                ):
                    bad_fights.add(row["fight_id"])
            if row["head_to_head_fight_count"] != pair_counts[pair]:
                field_counts["head_to_head"] += 1
                bad_fights.add(row["fight_id"])
        # Apply the event only after all of its rows were compared with the
        # same event-start snapshot. Within-event updates then use true bout
        # order so state for future events remains semantically correct.
        for row in event_rows:
            event_date = date.fromisoformat(row["event_date"])
            for side in ("a", "b"):
                fighter_id = row[f"fighter_{side}_id"]
                fighter_name = row[f"fighter_{side}_name"]
                _apply_fight(
                    states[fighter_id],
                    _outcome(row["result"], row["winner_name"], fighter_name),
                    event_date,
                    _side_stats(row, side),
                )
            pair_counts[tuple(sorted((row["fighter_a_id"], row["fighter_b_id"])))] += 1

    if bad_fights:
        report.fail(
            "chronology.event_start_features",
            "Stored features include same-event results and are unavailable at event start",
            len(bad_fights),
            sorted(bad_fights),
            mismatches_by_field=dict(field_counts),
        )
    else:
        report.pass_check(
            "chronology.event_start_features",
            "Pre-fight features use only information available before each event",
        )


def _missing_count(connection: sqlite3.Connection, table: str, column: str) -> int:
    return _count(
        connection,
        f'''SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NULL
            OR (TYPEOF("{column}")='text' AND TRIM("{column}")='')''',
    )


def _validate_metadata(
    connection: sqlite3.Connection,
    report: ValidationReport,
    required_metadata: set[str],
) -> None:
    groups = {
        "fight_physical": (
            "fights",
            ("fighter_a_age", "fighter_a_height_inches", "fighter_a_reach_inches", "fighter_a_stance",
             "fighter_b_age", "fighter_b_height_inches", "fighter_b_reach_inches", "fighter_b_stance"),
        ),
        "fighter_profiles": (
            "fighters",
            ("fighter_nickname", "date_of_birth", "height_inches", "reach_inches", "stance"),
        ),
        "fight_method": ("fights", ("method_of_victory",)),
        "fight_source_event": ("fights", ("source_event_url",)),
        "fight_control_time": (
            "fights", ("fighter_a_control_time_seconds", "fighter_b_control_time_seconds"),
        ),
        "round_control_time": ("fight_rounds", ("control_time_seconds",)),
        "fight_card_metadata": (
            "fights", ("weight_class", "card_order", "event_method_code", "last_scraped_at"),
        ),
        "fighter_profile_audit": ("fighters", ("profile_scraped_at",)),
        "event_scrape_audit": ("events", ("last_scraped_at",)),
        "fight_strike_splits": (
            "fights",
            tuple(
                f"fighter_{side}_significant_{position}_strikes_{kind}"
                for side in ("a", "b")
                for position in ("head", "body", "leg", "distance", "clinch", "ground")
                for kind in ("landed", "attempted")
            ),
        ),
        "round_strike_splits": (
            "fight_rounds",
            tuple(
                f"significant_{position}_strikes_{kind}"
                for position in ("head", "body", "leg", "distance", "clinch", "ground")
                for kind in ("landed", "attempted")
            ),
        ),
    }
    for group, (table, columns) in groups.items():
        missing = {column: _missing_count(connection, table, column) for column in columns}
        affected = sum(missing.values())
        if not affected:
            report.pass_check(f"metadata.{group}", f"{group} metadata is populated")
            continue
        required_missing = {
            column: count
            for column, count in missing.items()
            if count
            and (f"{table}.{column}" in required_metadata or column in required_metadata)
        }
        message = f"Source metadata is missing ({', '.join(f'{key}={value}' for key, value in missing.items())})"
        if required_missing:
            report.fail(
                f"metadata.{group}.required",
                "Specifically required metadata is missing",
                sum(required_missing.values()),
                required_missing=required_missing,
            )
        report.warn(f"metadata.{group}", message, affected, missing_by_column=missing)

    fight_columns = {row[1] for row in connection.execute("PRAGMA table_info('fights')")}
    if "weight_class" not in fight_columns:
        if "weight_class" in required_metadata or "fights.weight_class" in required_metadata:
            report.fail("metadata.weight_class", "Required weight-class field is absent from the schema")
        else:
            report.warn(
                "metadata.weight_class",
                "Weight class is absent from the schema and unavailable as a model feature",
            )


def _is_post_fight_feature(value: str) -> bool:
    normalized = value.strip().lower()
    table, _, column = normalized.rpartition(".")
    if not column:
        column = table
        table = ""
    if table.endswith("fight_rounds"):
        return True
    if column.startswith(("bout_", "opponent_bout_")):
        return True
    if column in POST_FIGHT_SUFFIXES:
        return True
    if any(
        column == f"fighter_{side}_{suffix}"
        for side in ("a", "b")
        for suffix in POST_FIGHT_SIDE_SUFFIXES
    ):
        return True
    return table.endswith("fighters") and column in FINAL_FIGHTER_AGGREGATES


def _validate_feature_manifest(report: ValidationReport, feature_columns: Sequence[str]) -> None:
    if not feature_columns:
        report.warn(
            "prediction.feature_manifest",
            "No feature manifest supplied; exclusion of post-fight target leakage was not verified",
        )
        return
    leaked = sorted({column for column in feature_columns if _is_post_fight_feature(column)})
    if leaked:
        report.fail(
            "prediction.feature_manifest",
            "Feature manifest contains post-fight or final-career information",
            len(leaked),
            leaked,
        )
    else:
        report.pass_check("prediction.feature_manifest", "Feature manifest excludes known leakage columns")


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _manifest_event_urls(listing: dict[str, Any]) -> list[str]:
    if isinstance(listing.get("event_urls"), list):
        return [str(value) for value in listing["event_urls"]]
    events = listing.get("events")
    if isinstance(events, list):
        return [str(value["event_url"]) for value in events if isinstance(value, dict) and value.get("event_url")]
    return []


def _validate_manifest(
    connection: sqlite3.Connection,
    report: ValidationReport,
    manifest: dict[str, Any] | None,
    promotion_gate: bool,
    max_manifest_age_hours: float | None,
) -> list[str]:
    """Validate a durable sync manifest.

    Promotion manifests are cumulative and have this shape::

        {
          "run_id": "uuid", "status": "completed", "code_version": "git-sha",
          "started_at": "ISO-8601", "finished_at": "ISO-8601",
          "errors": [{"resolved": true, ...}],
          "listing": {
            "url": ".../completed?page=all", "fetched_at": "ISO-8601",
            "event_urls": ["..."], "latest_event_date": "YYYY-MM-DD"
          },
          "event_fights": {"event-url": ["fight-url", ...]},
          "page_counts": {
            "listing_pages_expected": 1, "listing_pages_succeeded": 1,
            "event_pages_expected": 2, "event_pages_succeeded": 2,
            "fight_pages_expected": 25, "fight_pages_succeeded": 25,
            "failed_pages": 0
          },
          "feature_columns": ["fights.fighter_a_wins", ...]
        }

    ``event_fights`` is cumulative: unchanged cards are carried forward from the
    last accepted manifest while the weekly overlap window is refreshed.
    """
    if manifest is None:
        if promotion_gate:
            report.fail("audit.manifest", "Promotion requires a source/run manifest")
        else:
            report.warn("audit.manifest", "No source/run manifest supplied; full source completeness is unproven")
        return []
    if not isinstance(manifest, dict):
        report.fail("audit.manifest", "Run manifest must be a JSON object")
        return []

    run_id = manifest.get("run_id")
    report.run_id = str(run_id) if run_id else None
    required_identity = ("run_id", "status", "code_version", "started_at", "finished_at", "errors")
    missing_identity = [key for key in required_identity if key not in manifest]
    if missing_identity:
        report.fail(
            "audit.identity",
            "Run audit fields are missing",
            len(missing_identity),
            missing_identity,
        )
    elif manifest.get("status") != "completed":
        report.fail("audit.identity", "Ingestion run did not finish with status=completed")
    else:
        started = _parse_iso_datetime(manifest.get("started_at"))
        finished = _parse_iso_datetime(manifest.get("finished_at"))
        if started is None or finished is None or finished < started:
            report.fail("audit.identity", "Run audit timestamps are invalid")
        else:
            report.pass_check("audit.identity", "Run identity and timestamps are complete")

    errors = manifest.get("errors")
    unresolved = []
    resolved = []
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, dict) and error.get("resolved") is True:
                resolved.append(error)
            else:
                unresolved.append(error)
    else:
        unresolved.append({"reason": "errors is not a list"})
    if unresolved:
        report.fail("audit.unresolved_errors", "Ingestion has unresolved page/run errors", len(unresolved), unresolved)
    else:
        report.pass_check("audit.unresolved_errors", "No unresolved ingestion errors")
    if resolved:
        report.warn("audit.resolved_retries", "Ingestion succeeded after recorded retries", len(resolved), resolved)

    counts = manifest.get("page_counts")
    page_errors: list[dict[str, Any]] = []
    if not isinstance(counts, dict):
        page_errors.append({"reason": "page_counts is missing"})
    else:
        for prefix in ("listing", "event", "fight"):
            expected = counts.get(f"{prefix}_pages_expected")
            succeeded = counts.get(f"{prefix}_pages_succeeded")
            if not isinstance(expected, int) or not isinstance(succeeded, int) or expected != succeeded:
                page_errors.append({"page_type": prefix, "expected": expected, "succeeded": succeeded})
        if counts.get("failed_pages") != 0:
            page_errors.append({"failed_pages": counts.get("failed_pages")})
    if page_errors:
        report.fail("audit.page_accounting", "Page attempt/success accounting is incomplete", len(page_errors), page_errors)
    else:
        report.pass_check("audit.page_accounting", "Every expected page is accounted for")

    listing = manifest.get("listing")
    if not isinstance(listing, dict):
        report.fail("coverage.source_listing", "Manifest source listing is missing")
        return list(manifest.get("feature_columns") or [])
    event_urls = _manifest_event_urls(listing)
    if not listing.get("url") or not event_urls or len(event_urls) != len(set(event_urls)):
        report.fail("coverage.source_listing", "Source listing URL/event identities are empty or duplicated")
    else:
        local_events = {row[0] for row in connection.execute("SELECT event_url FROM events")}
        source_events = set(event_urls)
        missing = sorted(source_events - local_events)
        extra = sorted(local_events - source_events)
        if missing or extra:
            report.fail(
                "coverage.source_listing",
                "Staging event URLs do not exactly match the source listing manifest",
                len(missing) + len(extra),
                [{"missing": missing[:5], "extra": extra[:5]}],
            )
        else:
            report.pass_check(
                "coverage.source_listing",
                "Staging event URLs exactly match the source listing manifest",
                event_count=len(source_events),
            )

    fetched_at = _parse_iso_datetime(listing.get("fetched_at"))
    if fetched_at is None:
        report.fail("coverage.source_freshness", "Source-listing fetched_at is missing or invalid")
    elif max_manifest_age_hours is not None:
        age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
        if age_hours < -1 or age_hours > max_manifest_age_hours:
            report.fail(
                "coverage.source_freshness",
                "Source manifest is stale or dated in the future",
                age_hours=round(age_hours, 3),
                max_age_hours=max_manifest_age_hours,
            )
        else:
            report.pass_check("coverage.source_freshness", "Source manifest is fresh", age_hours=round(age_hours, 3))
    else:
        report.pass_check("coverage.source_freshness", "Source manifest has a valid fetch timestamp")

    latest = listing.get("latest_event_date")
    local_latest = connection.execute("SELECT MAX(event_date) FROM events").fetchone()[0]
    if latest and latest != local_latest:
        report.fail(
            "coverage.latest_event",
            "Latest staging event date differs from the source manifest",
            samples=[{"source": latest, "staging": local_latest}],
        )
    else:
        report.pass_check("coverage.latest_event", "Latest staging event matches source manifest")

    cards = manifest.get("event_fights")
    card_errors: list[dict[str, Any]] = []
    if not isinstance(cards, dict):
        card_errors.append({"reason": "event_fights cumulative manifest is missing"})
    elif set(cards) != set(event_urls):
        card_errors.append(
            {
                "missing_card_manifests": sorted(set(event_urls) - set(cards))[:5],
                "extra_card_manifests": sorted(set(cards) - set(event_urls))[:5],
            }
        )
    if isinstance(cards, dict):
        local_by_event: dict[str, set[str]] = defaultdict(set)
        for event_url, fight_url in connection.execute(
            """
            SELECT e.event_url,f.source_fight_url
            FROM events e LEFT JOIN fights f ON f.event_id=e.event_id
            """
        ):
            if fight_url:
                local_by_event[event_url].add(fight_url)
        for event_url, fight_urls in cards.items():
            if not isinstance(fight_urls, list) or len(fight_urls) != len(set(fight_urls)):
                card_errors.append({"event_url": event_url, "reason": "fight URL list invalid/duplicated"})
                continue
            expected = set(map(str, fight_urls))
            actual = local_by_event.get(event_url, set())
            if expected != actual:
                card_errors.append(
                    {
                        "event_url": event_url,
                        "missing": sorted(expected - actual)[:5],
                        "extra": sorted(actual - expected)[:5],
                    }
                )
            if len(card_errors) >= 20:
                break
    if card_errors:
        report.fail(
            "coverage.fight_manifests",
            "Stored fights do not exactly match cumulative event-card manifests",
            len(card_errors),
            card_errors,
        )
    else:
        report.pass_check("coverage.fight_manifests", "Every event card exactly matches its fight manifest")
    return list(manifest.get("feature_columns") or [])


def _validate_baseline(
    staging: sqlite3.Connection,
    staging_path: Path,
    baseline_path: Path | None,
    report: ValidationReport,
    promotion_gate: bool,
    initial_load: bool,
) -> None:
    if baseline_path is None:
        if promotion_gate and not initial_load:
            report.fail("promotion.baseline", "Promotion requires --baseline-db or explicit --initial-load")
        elif initial_load:
            report.pass_check("promotion.baseline", "Initial load explicitly has no baseline")
        return
    if staging_path.resolve() == baseline_path.resolve():
        report.fail("promotion.staging_isolation", "Staging and production baseline paths are identical")
        return
    if not baseline_path.is_file():
        report.fail("promotion.baseline", "Baseline database does not exist", samples=[str(baseline_path)])
        return
    try:
        baseline = _connect_read_only(baseline_path)
    except sqlite3.Error as exc:
        report.fail("promotion.baseline", f"Cannot read baseline database: {exc}")
        return
    try:
        old_events = {row[0] for row in baseline.execute("SELECT event_url FROM events")}
        new_events = {row[0] for row in staging.execute("SELECT event_url FROM events")}
        old_fights = {row[0] for row in baseline.execute("SELECT source_fight_url FROM fights")}
        new_fights = {row[0] for row in staging.execute("SELECT source_fight_url FROM fights")}
        lost_events = sorted(old_events - new_events)
        lost_fights = sorted(old_fights - new_fights)
        if lost_events or lost_fights:
            report.fail(
                "promotion.no_regression",
                "Staging unexpectedly removes baseline event/fight identities",
                len(lost_events) + len(lost_fights),
                [{"lost_events": lost_events[:5], "lost_fights": lost_fights[:5]}],
            )
        else:
            report.pass_check(
                "promotion.no_regression",
                "Staging retains all baseline event/fight identities",
                new_events=len(new_events - old_events),
                new_fights=len(new_fights - old_fights),
            )
        old_latest = baseline.execute("SELECT MAX(event_date) FROM events").fetchone()[0]
        new_latest = staging.execute("SELECT MAX(event_date) FROM events").fetchone()[0]
        if old_latest and (not new_latest or new_latest < old_latest):
            report.fail(
                "promotion.date_regression",
                "Staging latest event date is older than baseline",
                samples=[{"baseline": old_latest, "staging": new_latest}],
            )
        else:
            report.pass_check("promotion.date_regression", "Staging event-date coverage does not regress")
        report.metrics["baseline_database_sha256"] = _sha256(baseline_path)
    except sqlite3.Error as exc:
        report.fail("promotion.baseline", f"Baseline schema/comparison failed: {exc}")
    finally:
        baseline.close()


def _validate_promotion_artifacts(
    path: Path,
    report: ValidationReport,
    promotion_gate: bool,
    audit_target_declared: bool,
) -> None:
    if not promotion_gate:
        return
    sidecars = [
        str(candidate)
        for candidate in (Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists() and candidate.stat().st_size > 0
    ]
    if sidecars:
        report.fail(
            "promotion.closed_database",
            "Staging database has active journal/WAL sidecars and is not ready for atomic promotion",
            len(sidecars),
            sidecars,
        )
    else:
        report.pass_check("promotion.closed_database", "Staging database has no active sidecars")
    if audit_target_declared:
        report.pass_check("promotion.audit_output", "Durable validation audit output is configured")
    else:
        report.fail("promotion.audit_output", "Promotion requires --report or --audit-log")


def validate_database(
    database: str | os.PathLike[str],
    *,
    baseline_database: str | os.PathLike[str] | None = None,
    manifest: dict[str, Any] | None = None,
    promotion_gate: bool = False,
    initial_load: bool = False,
    temporal_policy: str = "bout-start",
    round_required_since: date = date(2000, 1, 1),
    max_manifest_age_hours: float | None = None,
    required_metadata: Iterable[str] = (),
    feature_columns: Sequence[str] = (),
    audit_target_declared: bool = False,
) -> ValidationReport:
    """Validate a database without mutating it and return a structured report."""
    path = Path(database)
    report = ValidationReport(str(path.resolve()))
    if not path.is_file():
        report.fail("database.exists", "Database file does not exist", samples=[str(path)])
        return report
    report.database_bytes = path.stat().st_size
    try:
        connection = _connect_read_only(path)
    except sqlite3.Error as exc:
        report.fail("database.open", f"Database cannot be opened read-only: {exc}")
        return report
    try:
        report.pass_check("database.open", "Database opened read-only")
        _validate_integrity(connection, report)
        schema_ok = _validate_schema(connection, report)
        if schema_ok:
            _validate_fighter_bout_history(connection, report)
            _validate_core_rows(connection, report)
            _validate_fight_domains(connection, report)
            _validate_split_identities(connection, report)
            _validate_rounds(connection, report, round_required_since)
            _validate_totals(connection, report)
            _validate_temporal(connection, report, temporal_policy)
            _validate_metadata(connection, report, set(required_metadata))
            manifest_features = _validate_manifest(
                connection, report, manifest, promotion_gate, max_manifest_age_hours
            )
            _validate_feature_manifest(report, list(feature_columns) or manifest_features)
            _validate_baseline(
                connection,
                path,
                Path(baseline_database) if baseline_database is not None else None,
                report,
                promotion_gate,
                initial_load,
            )
        _validate_promotion_artifacts(path, report, promotion_gate, audit_target_declared)
    except sqlite3.Error as exc:
        report.fail("database.query", f"Validation query failed: {exc}")
    finally:
        connection.close()
    # Hash only after closing the read connection so the report identifies the
    # exact artifact which was gated.
    report.database_sha256 = _sha256(path)
    return report


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _append_audit(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        key: payload[key]
        for key in (
            "database", "validated_at", "run_id", "database_sha256", "database_bytes",
            "ok", "hard_failure_count", "warning_count", "metrics",
        )
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _print_report(report: ValidationReport) -> None:
    for check in report.checks:
        suffix = f" ({check.count})" if check.count else ""
        print(f"{check.status:4} {check.check_id}: {check.message}{suffix}")
        for sample in check.samples[:3]:
            print(f"     sample: {json.dumps(sample, sort_keys=True, default=str)}")
    print(
        f"SUMMARY hard_failures={report.hard_failure_count} "
        f"warnings={report.warning_count} database={report.database}"
    )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("manifest root must be a JSON object")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only UFC database validation and weekly staging-promotion gate."
    )
    parser.add_argument("--db", required=True, help="Staging/database SQLite file to validate.")
    parser.add_argument("--baseline-db", help="Currently promoted database used for no-regression checks.")
    parser.add_argument("--run-manifest", help="Cumulative source/run manifest JSON from this sync.")
    parser.add_argument("--promotion-gate", action="store_true", help="Require audit, manifest, and staging safety checks.")
    parser.add_argument("--initial-load", action="store_true", help="Allow a promotion gate without a baseline database.")
    parser.add_argument(
        "--temporal-policy",
        choices=("bout-start", "event-start", "ingestion", "off"),
        default="bout-start",
        help="Prediction availability policy; bout-start replays card_order DESC and is the safe default.",
    )
    parser.add_argument(
        "--round-required-since",
        default="2000-01-01",
        help="Missing round data on/after this date is a hard failure (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--max-manifest-age-hours",
        type=float,
        default=48.0,
        help="Maximum source listing age when a manifest is supplied.",
    )
    parser.add_argument(
        "--require-metadata",
        action="append",
        default=[],
        help="Metadata column which must be populated; repeat as needed.",
    )
    parser.add_argument(
        "--feature-column",
        action="append",
        default=[],
        help="Qualified model feature to audit for target leakage; repeat as needed.",
    )
    parser.add_argument("--report", help="Atomically write the complete JSON validation report.")
    parser.add_argument("--audit-log", help="Append one durable JSON-lines audit summary.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        threshold_date = date.fromisoformat(args.round_required_since)
    except ValueError:
        parser.error("--round-required-since must be YYYY-MM-DD")
    try:
        manifest = _load_json(Path(args.run_manifest)) if args.run_manifest else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read --run-manifest: {exc}")

    report = validate_database(
        args.db,
        baseline_database=args.baseline_db,
        manifest=manifest,
        promotion_gate=args.promotion_gate,
        initial_load=args.initial_load,
        temporal_policy=args.temporal_policy,
        round_required_since=threshold_date,
        max_manifest_age_hours=args.max_manifest_age_hours if manifest else None,
        required_metadata=args.require_metadata,
        feature_columns=args.feature_column,
        audit_target_declared=bool(args.report or args.audit_log),
    )
    payload = report.to_dict()
    try:
        if args.report:
            _atomic_json_write(Path(args.report), payload)
        if args.audit_log:
            _append_audit(Path(args.audit_log), payload)
    except OSError as exc:
        print(f"FAIL audit.write: could not persist validation audit: {exc}", file=sys.stderr)
        return 2
    _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
