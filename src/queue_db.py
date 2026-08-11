"""SQLite job queue.

Jobs survive a bot restart: a 5-15 minute pipeline that only lives in memory
loses everything on a crash, and the user has already sent the video by then.
"""

import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

STATUSES = ("queued", "running", "done", "failed")
STAGES = ("queued", "downloading", "transcribing", "vision", "extracting", "saving", "done")
MAX_ATTEMPTS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    shortcode    TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    source       TEXT NOT NULL,
    chat_id      INTEGER,
    message_id   INTEGER,
    status       TEXT NOT NULL DEFAULT 'queued',
    stage        TEXT NOT NULL DEFAULT 'queued',
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    result       TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(config.QUEUE_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)


def enqueue(job_id: str, shortcode: str, source_type: str, source: str,
            chat_id: int, message_id: int) -> bool:
    """Insert a job. Returns False if this id is already known (duplicate send)."""
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO jobs "
            "(id, shortcode, source_type, source, chat_id, message_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, shortcode, source_type, source, chat_id, message_id, now, now),
        )
        return cur.rowcount > 0


def get(job_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def claim_next() -> dict | None:
    """Take the oldest queued job and mark it running, atomically."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        con.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
        return dict(row)


def set_stage(job_id: str, stage: str) -> None:
    with _conn() as con:
        con.execute("UPDATE jobs SET stage=?, updated_at=? WHERE id=?",
                    (stage, time.time(), job_id))


def finish(job_id: str, result: dict) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE jobs SET status='done', stage='done', error=NULL, result=?, updated_at=? "
            "WHERE id=?",
            (json.dumps(result, ensure_ascii=False), time.time(), job_id),
        )


def fail(job_id: str, error: str) -> str:
    """Mark failed, or requeue if attempts remain. Returns the resulting status."""
    with _conn() as con:
        row = con.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        attempts = row["attempts"] if row else MAX_ATTEMPTS
        status = "queued" if attempts < MAX_ATTEMPTS else "failed"
        con.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                    (status, error[:2000], time.time(), job_id))
    return status


def requeue_running() -> int:
    """Called at startup: a job left 'running' means the bot died mid-pipeline."""
    with _conn() as con:
        cur = con.execute(
            "UPDATE jobs SET status='queued', stage='queued', updated_at=? WHERE status='running'",
            (time.time(),),
        )
        return cur.rowcount


def pending_count() -> int:
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','running')").fetchone()
    return row["c"]


def recent(limit: int = 10) -> list:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, shortcode, status, stage, error FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
