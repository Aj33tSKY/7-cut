"""SQLite job store.

One table, plain rollback-journal mode (SQLite's default) — deliberately
NOT WAL. WAL requires memory-mapped shared-memory files, which are
documented as unreliable over network-backed filesystems — exactly what a
Railway (or most PaaS) persistent volume is. At this app's write volume
(a handful of status UPDATEs per second, at most, across a ~10-person team)
WAL's actual benefit — readers not blocking on a writer — isn't needed;
correctness/compatibility with the volume matters more than that.

sqlite3 connections aren't safe to share across threads by default; we open
one connection per call with check_same_thread=False and serialize writes
behind a process-wide lock.

Every function here is synchronous/blocking. Callers in async code MUST
wrap calls in asyncio.to_thread — a stuck disk operation (even a normal
latency spike on network-backed storage) must never be allowed to freeze
the event loop, since that would also freeze unrelated things like the
platform's healthcheck endpoint and look like the whole app crashed.
worker.py's dispatcher_loop is the one call site that runs unconditionally
every couple of seconds forever — if any db call were ever going to freeze
the loop, that periodic tick is what would surface it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from models import Job, JobStatus

DB_PATH = Path(__file__).resolve().parent / "data" / "jobs.db"

_write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        # journal_mode is a persistent property of the database FILE, not a
        # per-connection setting — if this file was ever opened in WAL mode
        # (true for jobs.db on volumes that already existed before this
        # change), it silently STAYS in WAL mode forever unless explicitly
        # switched back, regardless of what future connections request.
        # DELETE is SQLite's traditional default rollback journal.
        current_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        if current_mode.lower() != "delete":
            conn.execute("PRAGMA journal_mode=DELETE;")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                drive_folder_id TEXT NOT NULL,
                drive_raw_folder_id TEXT NOT NULL,
                drive_cut_folder_id TEXT NOT NULL,
                project_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT,
                stats TEXT
            )
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _write():
    with _write_lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def create_job(
    drive_folder_id: str,
    drive_raw_folder_id: str,
    drive_cut_folder_id: str,
    project_name: str,
    created_by: str,
) -> Job:
    job_id = str(uuid.uuid4())
    now = _now()
    with _write() as conn:
        conn.execute(
            "INSERT INTO jobs (id, drive_folder_id, drive_raw_folder_id, drive_cut_folder_id, "
            "project_name, status, created_by, created_at, updated_at, error, stats) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}')",
            (job_id, drive_folder_id, drive_raw_folder_id, drive_cut_folder_id,
             project_name, JobStatus.QUEUED.value, created_by, now, now),
        )
    return get_job(job_id)


def get_job(job_id: str) -> Job | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None
    finally:
        conn.close()


def list_jobs() -> list[Job]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [Job.from_row(r) for r in rows]
    finally:
        conn.close()


def list_jobs_by_status(*statuses: JobStatus) -> list[Job]:
    conn = _connect()
    try:
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            [s.value for s in statuses],
        ).fetchall()
        return [Job.from_row(r) for r in rows]
    finally:
        conn.close()


def update_status(job_id: str, status: JobStatus, error: str | None = None) -> None:
    with _write() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status.value, error, _now(), job_id),
        )


def update_stats(job_id: str, stats: dict) -> None:
    with _write() as conn:
        conn.execute(
            "UPDATE jobs SET stats = ?, updated_at = ? WHERE id = ?",
            (json.dumps(stats), _now(), job_id),
        )


def delete_job(job_id: str) -> None:
    with _write() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
