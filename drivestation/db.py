from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    slot TEXT NOT NULL,
    manufacturer TEXT,
    model TEXT,
    serial TEXT,
    capacity_bytes INTEGER,
    drive_type TEXT,
    health_percent INTEGER,
    health_verdict TEXT,
    health_warnings TEXT,
    wipe_method TEXT,
    wipe_started_at TEXT,
    wipe_finished_at TEXT,
    result TEXT,
    error TEXT,
    batch TEXT,
    used_bytes_before INTEGER,
    usage_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_serial ON jobs(serial);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobLog:
    """SQLite job log. Every wipe attempt gets a row, traceable by serial.

    The `batch` column exists from day one so client-lot reporting can be
    added later without a schema migration.
    """

    def __init__(self, path: str = "drivestation.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        if "used_bytes_before" not in cols:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN used_bytes_before INTEGER")
        if "usage_label" not in cols:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN usage_label TEXT")

    def close(self) -> None:
        self._conn.close()

    def start_job(self, slot: str, manufacturer: str, model: str, serial: str,
                  capacity_bytes: int, drive_type: str,
                  health_percent: Optional[int], health_verdict: str,
                  health_warnings: list[str], wipe_method: str,
                  batch: Optional[str] = None,
                  used_bytes_before: Optional[int] = None,
                  usage_label: Optional[str] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO jobs (created_at, slot, manufacturer, model,
                       serial, capacity_bytes, drive_type, health_percent,
                       health_verdict, health_warnings, wipe_method,
                       wipe_started_at, batch, used_bytes_before, usage_label)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), slot, manufacturer, model, serial, capacity_bytes,
                 drive_type, health_percent, health_verdict,
                 json.dumps(health_warnings), wipe_method, _now(), batch,
                 used_bytes_before, usage_label))
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_job(self, job_id: int, result: str,
                   error: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET wipe_finished_at=?, result=?, error=? WHERE id=?",
                (_now(), result, error, job_id))
            self._conn.commit()

    def mark_interrupted_jobs(self) -> int:
        """Called at startup. Any job with no result was cut off by a crash or
        power loss; it must never look successful."""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE jobs SET result='FAILED',
                       error='Interrupted (power loss or crash) — drive must be re-wiped'
                   WHERE result IS NULL""")
            self._conn.commit()
            return cur.rowcount

    def by_serial(self, serial: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE serial=? COLLATE NOCASE ORDER BY id DESC",
                (serial,)).fetchall()
        return [dict(r) for r in rows]

    def by_batch(self, batch: str, limit: int = 2000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE batch=? COLLATE NOCASE "
                "ORDER BY id DESC LIMIT ?",
                (batch, limit)).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
