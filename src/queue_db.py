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
    updated_at   REAL NOT NULL,
    author_id    TEXT DEFAULT '',
    author_name  TEXT DEFAULT ''
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
        cols = [r["name"] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
        if "author_id" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN author_id TEXT DEFAULT ''")
        if "author_name" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN author_name TEXT DEFAULT ''")


def enqueue(job_id: str, shortcode: str, source_type: str, source: str,
            chat_id: int, message_id: int, author_id: str = "", author_name: str = "") -> bool:
    """Insert a job. Returns False if this id is already known (duplicate send)."""
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO jobs "
            "(id, shortcode, source_type, source, chat_id, message_id, created_at, updated_at, author_id, author_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, shortcode, source_type, source, chat_id, message_id, now, now, str(author_id or ""), str(author_name or "")),
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


_TIMEOUT_TEXT_MARKERS = (
    "readtimeout",
    "connecttimeout",
    "timeouterror",
    "read timed out",
    "connection timed out",
    "operation timed out",
    "request timeout",
)


def is_permanent_media_error(exc_or_text: Exception | str) -> bool:
    """A hang that repeats identically on every attempt (ffmpeg on a corrupt file).

    is_timeout_error() returning False only says "not a transient timeout" -
    fail() would then fall through to the ordinary MAX_ATTEMPTS path and retry
    it three times anyway. This says "do not retry at all", and it also catches
    the wrapped case, where pipeline's own except clause never sees it.
    """
    import subprocess
    if isinstance(exc_or_text, subprocess.TimeoutExpired):
        return True
    if isinstance(getattr(exc_or_text, "__cause__", None), subprocess.TimeoutExpired):
        return True
    return "timeoutexpired" in str(exc_or_text).lower()


def is_timeout_error(exc_or_text: Exception | str) -> bool:
    """Is this a TRANSIENT timeout worth retrying?

    subprocess.TimeoutExpired is deliberately excluded. ffmpeg only hits its
    timeout on input it cannot decode, so a retry re-runs the identical
    600-second hang: three attempts would block the single worker for half
    an hour where one honest failure would do. Its message contains
    "timed out", so the text check below has to exclude it too - fail()
    is called with the formatted string, not the exception.
    """
    import subprocess

    import requests
    if isinstance(exc_or_text, subprocess.TimeoutExpired):
        return False
    if isinstance(exc_or_text, (requests.exceptions.Timeout, TimeoutError)):
        return True
    cause = getattr(exc_or_text, "__cause__", None)
    if isinstance(cause, subprocess.TimeoutExpired):
        return False
    if isinstance(cause, (requests.exceptions.Timeout, TimeoutError)):
        return True
    text = str(exc_or_text).lower()
    if "timeoutexpired" in text:
        return False
    # Matching the bare word "timeout" swept in anything that merely mentions
    # it - a URL like /post-timeout-issue, or "invalid option timeout" - and
    # sent deterministic failures round the retry loop. These markers are the
    # exception class names and the phrases the libraries actually emit.
    return any(marker in text for marker in _TIMEOUT_TEXT_MARKERS)


def fail(job_id: str, error: str, retry: bool = True) -> str:
    """Mark failed, or requeue if attempts remain. Returns the resulting status."""
    with _conn() as con:
        row = con.execute("SELECT attempts, status FROM jobs WHERE id=?", (job_id,)).fetchone()
        attempts = row["attempts"] if row else MAX_ATTEMPTS
        current_status = row["status"] if row else "failed"
        if current_status == "failed":
            should_retry = False
        elif is_permanent_media_error(error):
            should_retry = False
        elif is_timeout_error(error):
            should_retry = retry and (attempts <= config.TIMEOUT_MAX_RETRIES)
        else:
            should_retry = retry and (attempts < MAX_ATTEMPTS)
        status = "queued" if should_retry else "failed"
        con.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                    (status, error[:2000], time.time(), job_id))
    return status


def requeue_running() -> list[dict]:
    """Called at startup: a job left 'running' means the bot died mid-pipeline.

    Returns a list of dicts with chat_id, message_id, shortcode, and error for
    jobs that were transitioned to 'failed' because attempts >= MAX_ATTEMPTS.
    """
    with _conn() as con:
        # Without the attempts guard a job that hangs the worker gets requeued
        # on every restart and hangs it again, forever.
        err_msg = "bot qayta ishga tushdi, urinishlar tugadi"
        rows = con.execute(
            "SELECT id, chat_id, message_id, shortcode, error FROM jobs "
            "WHERE status='running' AND attempts >= ?",
            (MAX_ATTEMPTS,),
        ).fetchall()
        failed = []
        for r in rows:
            d = dict(r)
            if not d.get("error"):
                d["error"] = err_msg
            failed.append(d)

        now = time.time()
        con.execute(
            "UPDATE jobs SET "
            "  status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END, "
            "  stage  = CASE WHEN attempts >= ? THEN stage ELSE 'queued' END, "
            "  error  = CASE WHEN attempts >= ? AND (error IS NULL OR error = '') THEN ? ELSE error END, "
            "  updated_at = ? "
            "WHERE status='running'",
            (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, err_msg, now),
        )
        return failed


def pending_count() -> int:
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','running')").fetchone()
    return row["c"]


def recent(limit: int = 10, chat_id: int | None = None) -> list:
    """Recent jobs. With chat_id, only that chat's own - /status used to show
    every member the links everyone else was processing."""
    sql = "SELECT id, shortcode, status, stage, error FROM jobs"
    params: list = []
    if chat_id is not None:
        sql += " WHERE chat_id = ?"
        params.append(chat_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
