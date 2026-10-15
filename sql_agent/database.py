"""Read-only SQLite access with a physically isolated historical query surface."""

from __future__ import annotations

import math
import re
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAX_QUERIES = 15
TABLES = ("events", "fighters", "fights", "fight_rounds")
SURFACES = {*TABLES, "fighter_bout_history"}
IDENTITY_COLUMNS = ("fighter_id", "fighter_name", "date_of_birth")
FORBIDDEN = set("INSERT UPDATE DELETE DROP ALTER ATTACH DETACH PRAGMA CREATE REPLACE "
                "VACUUM REINDEX ANALYZE BEGIN COMMIT ROLLBACK SAVEPOINT RELEASE "
                "TRANSACTION INTO RETURNING".split())
FUNCTIONS = set("abs avg count sum total min max round coalesce ifnull nullif iif "
                "lower upper trim ltrim rtrim length substr substring replace "
                "instr like glob typeof cast printf format unicode char hex "
                "date time datetime julianday unixepoch strftime timediff "
                "row_number rank dense_rank percent_rank cume_dist ntile lag lead "
                "first_value last_value nth_value group_concat string_agg "
                "json json_array json_object json_extract json_type json_valid "
                "json_array_length json_group_array json_group_object "
                "sqrt pow power exp ln log log10 floor ceil ceiling mod sign".split())


class QueryLimitError(RuntimeError):
    pass


def normalize_date(value: str | date | None) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        value = value.isoformat()
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("as_of_date must use YYYY-MM-DD")
    return value


def validate_sql(query: str) -> None:
    """Lexical guard; SQLite's authorizer is the semantic security boundary."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Provide a SELECT or WITH query")
    # Hide comments, string literals, and quoted identifiers before token checks.
    code = re.sub(r"--[^\n]*(?:\n|$)|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
                  r"`(?:``|[^`])*`|\[[^\]]*\]", " ", query, flags=re.S)
    words = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", code.upper())
    if not words or words[0] not in {"SELECT", "WITH"}:
        raise ValueError("Only SELECT / WITH statements are allowed")
    forbidden = FORBIDDEN.intersection(words)
    if forbidden:
        raise ValueError(f"Forbidden SQL keyword: {', '.join(sorted(forbidden))}")
    if ";" in code.rstrip().rstrip(";"):
        raise ValueError("Only one SQL statement is allowed")


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _json_value(value):
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class QuerySession:
    """One analysis, one cutoff, one budget. Never exposes the source connection.

    Bootstrap reads use a mode=ro connection in one consistent transaction.
    Only eligible rows are copied into private memory. No source DB is attached
    to the model-facing connection, so main.*, nested CTEs, and missing WHERE
    clauses cannot bypass the cutoff. Bootstrap is not an agent query.
    """

    def __init__(self, db_path: str | Path = ROOT / "ufc_fights.db", *,
                 as_of_date: str | date | None = None, max_queries: int = MAX_QUERIES,
                 row_limit: int = 200, timeout: float = 5.0,
                 fighter_identities: list[dict[str, str]] | None = None):
        if max_queries < 1 or row_limit < 1 or timeout <= 0:
            raise ValueError("max_queries, row_limit and timeout must be positive")
        self.as_of_date = normalize_date(as_of_date)
        self.max_queries, self.row_limit, self.timeout = max_queries, row_limit, timeout
        self.queries_run: list[dict[str, Any]] = []
        self.attempts = 0
        self._connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.identities: dict[str, dict[str, Any]] = {}
        try:
            self._bootstrap(Path(db_path).resolve())
            for identity in fighter_identities or []:
                fid, name = identity.get("fighter_id", ""), identity.get("fighter_name", "")
                if not re.fullmatch(r"[a-f0-9]{16}", fid) or not isinstance(name, str) or not name.strip():
                    raise ValueError("Live-card fighter needs a source identity and name")
                if fid not in self.identities:
                    row = {"fighter_id": fid, "fighter_name": name.strip(), "date_of_birth": None}
                    self.identities[fid] = row
                    self._connection.execute("INSERT INTO fighters VALUES (:fighter_id, :fighter_name, :date_of_birth)", row)
            self._connection.commit()
            self._schema = self._inspect_schema()
            self._connection.execute("PRAGMA query_only = ON")
            self._connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2_000_000)
            self._connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
            self._connection.set_authorizer(self._authorize)
        except Exception:
            self.close()
            raise

    def _bootstrap(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=self.timeout)
        source.row_factory = sqlite3.Row
        deadline = time.monotonic() + max(30.0, self.timeout)
        source.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            source.execute("BEGIN")
            filters = {
                "events": "SELECT * FROM events WHERE event_date < :cutoff",
                "fighters": "SELECT fighter_id, fighter_name, date_of_birth FROM fighters",
                "fights": "SELECT f.* FROM fights f JOIN events e USING(event_id) "
                          "WHERE e.event_date < :cutoff",
                "fight_rounds": "SELECT r.* FROM fight_rounds r JOIN fights f USING(fight_id) "
                                "JOIN events e USING(event_id) WHERE e.event_date < :cutoff",
            }
            for table in TABLES:
                columns = source.execute(f"PRAGMA table_info({_quoted(table)})").fetchall()
                if not columns:
                    raise ValueError(f"Required table missing: {table}. See SCHEMA_NOTES.md")
                if table == "fighters":
                    columns = [c for c in columns if c["name"] in IDENTITY_COLUMNS]
                    if {c["name"] for c in columns} != set(IDENTITY_COLUMNS):
                        raise ValueError("fighters identity schema differs from SCHEMA_NOTES.md")
                definitions = ", ".join(f'{_quoted(c["name"])} {c["type"]}' for c in columns)
                self._connection.execute(f"CREATE TABLE {_quoted(table)} ({definitions})")
                cursor = source.execute(filters[table], {"cutoff": self.as_of_date})
                slots = ",".join("?" for _ in columns)
                self._connection.executemany(f"INSERT INTO {_quoted(table)} VALUES ({slots})", cursor)
            # Use the inspected repo view, without running migrations on the source.
            view = source.execute("SELECT sql FROM sqlite_master WHERE type='view' "
                                  "AND name='fighter_bout_history'").fetchone()
            if not view:
                raise ValueError("Missing fighter_bout_history view. See SCHEMA_NOTES.md")
            self._connection.execute(view[0])
            for table, column in (("events", "event_id"), ("fights", "fight_id"),
                                  ("fights", "event_id"), ("fight_rounds", "fight_id")):
                self._connection.execute(f"CREATE INDEX {_quoted(table + '_' + column)} "
                                         f"ON {_quoted(table)} ({_quoted(column)})")
            self.identities = {r["fighter_id"]: dict(r) for r in
                               self._connection.execute("SELECT * FROM fighters")}
            # Schedule-only debutants are identities, never historical observations.
            # Old databases without schedule tables remain valid for single fights.
            if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='upcoming_fights'").fetchone():
                for row in source.execute("SELECT fighter_a_id, fighter_a_name, fighter_b_id, "
                                          "fighter_b_name FROM upcoming_fights"):
                    for side in ("a", "b"):
                        fid = row[f"fighter_{side}_id"]
                        if fid not in self.identities:
                            identity = {"fighter_id": fid, "fighter_name": row[f"fighter_{side}_name"],
                                        "date_of_birth": None}
                            self.identities[fid] = identity
                            self._connection.execute("INSERT INTO fighters VALUES (:fighter_id, :fighter_name, :date_of_birth)", identity)
            self._connection.commit()
        finally:
            source.close()

    def _inspect_schema(self) -> dict[str, Any]:
        return {
            "as_of_date": self.as_of_date,
            "cutoff": "Every fight surface contains only event_date < as_of_date.",
            "fighters": "Identity and DOB only; profiles are not historically versioned.",
            "tables": {
                name: [{"name": r["name"], "type": r["type"] or "expression"}
                       for r in self._connection.execute(f"PRAGMA table_info({_quoted(name)})")]
                for name in sorted(SURFACES)
            },
        }

    @staticmethod
    def _authorize(action, arg1, arg2, database, trigger):
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
            return sqlite3.SQLITE_OK
        # SQLite's COUNT(*) optimization reports database=None and column="".
        if action == sqlite3.SQLITE_READ and database in {"main", None} and arg1 in SURFACES:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    @property
    def remaining(self) -> int:
        return max(0, self.max_queries - self.attempts)

    def consume_attempt(self) -> None:
        if not self.remaining:
            raise QueryLimitError(f"Query budget exhausted ({self.max_queries})")
        self.attempts += 1

    def describe_schema(self) -> dict[str, Any]:
        self.consume_attempt()
        return self._schema

    def resolve_fighter(self, fighter: str) -> dict[str, Any]:
        if not isinstance(fighter, str) or not fighter.strip():
            raise ValueError("Provide a fighter name or stable fighter_id")
        if fighter in self.identities:
            return self.identities[fighter]
        matches = [r for r in self.identities.values()
                   if r["fighter_name"].casefold() == fighter.strip().casefold()]
        if len(matches) != 1:
            raise ValueError(f"Fighter {fighter!r} is unknown or ambiguous; use a stable fighter_id")
        return matches[0]

    def check_date(self, as_of_date: str | None) -> None:
        if as_of_date is not None and normalize_date(as_of_date) != self.as_of_date:
            raise ValueError("Helper cutoff must equal the analysis session's as_of_date")

    def _execute(self, query: str, purpose: str, parameters: dict | None = None, *,
                 complete: bool = False) -> dict[str, Any]:
        self.consume_attempt()
        entry = {"sql": query, "purpose": purpose, "rows_returned": 0}
        if parameters:
            entry["parameters"] = parameters
        self.queries_run.append(entry)
        deadline = time.monotonic() + self.timeout
        self._connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            validate_sql(query)
            cursor = self._connection.execute(query, parameters or {})
            rows = cursor.fetchall() if complete else cursor.fetchmany(self.row_limit + 1)
            truncated = not complete and len(rows) > self.row_limit
            rows = rows if complete else rows[:self.row_limit]
            entry.update(rows_returned=len(rows), truncated=truncated)
            return {"rows": [{key: _json_value(value) for key, value in dict(r).items()}
                             for r in rows], "truncated": truncated}
        except (sqlite3.Error, ValueError) as exc:
            entry["error"] = str(exc)
            return {"error": str(exc), "rows": [], "truncated": False}
        finally:
            self._connection.set_progress_handler(None, 0)

    def run_sql(self, query: str, purpose: str = "Explore matchup evidence") -> dict[str, Any]:
        return self._execute(query, purpose)

    def history_rows(self, fighter_ids: list[str] | None, purpose: str) -> list[dict]:
        """Trusted analytics read complete evidence; public arbitrary SQL stays capped."""
        params = {"as_of_date": self.as_of_date}
        condition = ""
        if fighter_ids:
            params.update({f"fighter_{i}": fid for i, fid in enumerate(fighter_ids)})
            condition = " AND fighter_id IN (" + ",".join(
                f":fighter_{i}" for i in range(len(fighter_ids))) + ")"
        result = self._execute(
            "SELECT * FROM fighter_bout_history WHERE event_date < :as_of_date" + condition +
            " ORDER BY event_date, event_bout_sequence, event_id, fight_id, fighter_id",
            purpose, params, complete=True,
        )
        if "error" in result:
            raise ValueError(result["error"])
        return result["rows"]

    def close(self) -> None:
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
