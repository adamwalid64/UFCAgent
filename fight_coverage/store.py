"""Private durable caches and atomic quota accounting, shared across card runs."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock


class QuotaExceeded(RuntimeError):
    pass


class Store:
    def __init__(self, directory: Path, retention_days: int = 30, now=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "coverage.sqlite3"
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.retention_days = retention_days
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cache (
                    kind TEXT, key TEXT, payload TEXT, created REAL, expires REAL,
                    PRIMARY KEY (kind, key));
                CREATE TABLE IF NOT EXISTS fetched (video_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS quota (day TEXT PRIMARY KEY, used INTEGER, blocked INTEGER DEFAULT 0);
            """)
        self.prune()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        # Deleted content must not remain in free SQLite pages.
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def lock(self, key: str, timeout: float = 180):
        digest = hashlib.sha256(key.encode()).hexdigest()
        return FileLock(str(self.directory / (digest + ".lock")), timeout=timeout)

    def prune(self):
        with self.connect() as db:
            db.execute("DELETE FROM cache WHERE expires <= ?", (self.now().timestamp(),))
            day = self.now().astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
            db.execute("DELETE FROM quota WHERE day < ?", (day,))

    def get(self, kind: str, key: str):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM cache WHERE kind=? AND key=? AND expires>?",
                             (kind, key, self.now().timestamp())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind: str, key: str, value, ttl: float):
        stamp = self.now().timestamp()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                       (kind, key, json.dumps(value, ensure_ascii=False), stamp,
                        stamp + min(ttl, self.retention_days * 86400)))
            if kind == "transcript":
                db.execute("INSERT OR IGNORE INTO fetched VALUES (?)", (key,))

    def was_fetched(self, video_id: str):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM fetched WHERE video_id=?", (video_id,)).fetchone() is not None

    def forget(self, video_id: str):
        # Retain only the no-refetch marker; permission revocation must not trigger a new download.
        with self.connect() as db:
            db.execute("DELETE FROM cache WHERE kind IN ('transcript','failure') AND key=?", (video_id,))
            db.execute("INSERT OR IGNORE INTO fetched VALUES (?)", (video_id,))

    def quota(self, limit: int, *, block=False):
        day = self.now().astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO quota(day,used) VALUES (?,0)", (day,))
            used, blocked = db.execute("SELECT used,blocked FROM quota WHERE day=?", (day,)).fetchone()
            if block:
                db.execute("UPDATE quota SET blocked=1 WHERE day=?", (day,))
            elif blocked or used >= limit:
                raise QuotaExceeded("YouTube discovery quota exhausted for this Pacific calendar day")
            else:
                db.execute("UPDATE quota SET used=used+1 WHERE day=?", (day,))
