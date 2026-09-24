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


class JobDeferred(Exception):
    """Raised when a job should be deferred (e.g. quota limit)."""

    def __init__(self, retry_after_sec: int = 600, message: str = ""):
        self.retry_after_sec = int(retry_after_sec)
        super().__init__(message or f"Job deferred for {self.retry_after_sec}s")


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
    defer_count  INTEGER NOT NULL DEFAULT 0,
    not_before   REAL NOT NULL DEFAULT 0,
    error        TEXT,
    result       TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    author_id    TEXT DEFAULT '',
    author_name  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_subscribers (
    job_id      TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    created_at  REAL NOT NULL,
    PRIMARY KEY (job_id, chat_id)
);

CREATE TABLE IF NOT EXISTS user_states (
    user_id     INTEGER PRIMARY KEY,
    is_active   INTEGER NOT NULL DEFAULT 1,
    reason      TEXT,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
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
        if "defer_count" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN defer_count INTEGER DEFAULT 0")
        if "not_before" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN not_before REAL DEFAULT 0")


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


def get_by_shortcode(shortcode: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM jobs WHERE shortcode = ? ORDER BY created_at DESC LIMIT 1",
            (shortcode,),
        ).fetchone()
        return dict(row) if row else None


def requeue_job(job_id: str) -> bool:
    """Requeue a failed job with attempts=0 and error=NULL."""
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            "UPDATE jobs SET status='queued', stage='queued', attempts=0, error=NULL, updated_at=? "
            "WHERE id=?",
            (now, job_id),
        )
        return cur.rowcount > 0


def claim_next() -> dict | None:
    """Take the oldest queued job that is ready (not_before <= now) and mark it running, atomically."""
    now = time.time()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM jobs WHERE status = 'queued' AND not_before <= ? ORDER BY created_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return None
        con.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        job = dict(row)
        job["status"] = "running"
        job["attempts"] += 1
        return job


def defer(job_id: str, sec: int) -> None:
    """Defer job: status -> queued, defer_count+1, not_before=now+sec, attempts does not increase."""
    now = time.time()
    with _conn() as con:
        con.execute(
            "UPDATE jobs SET "
            "  status='queued', "
            "  defer_count=defer_count+1, "
            "  not_before=?, "
            "  attempts=MAX(0, attempts-1), "
            "  updated_at=? "
            "WHERE id=?",
            (now + sec, now, job_id),
        )


def add_subscriber(job_id: str, chat_id: int, message_id: int | None = None) -> bool:
    """Register a user to be notified when this job finishes."""
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            "INSERT OR REPLACE INTO job_subscribers (job_id, chat_id, message_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (job_id, chat_id, message_id, now),
        )
        return cur.rowcount > 0


def get_subscribers(job_id: str) -> list[dict]:
    """Get all subscribers for a job."""
    with _conn() as con:
        rows = con.execute(
            "SELECT job_id, chat_id, message_id, created_at FROM job_subscribers "
            "WHERE job_id = ? ORDER BY created_at ASC",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_subscribers(job_id: str) -> None:
    """Remove subscribers for a job after delivering results."""
    with _conn() as con:
        con.execute("DELETE FROM job_subscribers WHERE job_id = ?", (job_id,))


def mark_user_inactive(user_id: int, reason: str = "") -> None:
    now = time.time()
    with _conn() as con:
        con.execute(
            "INSERT INTO user_states (user_id, is_active, reason, updated_at) "
            "VALUES (?, 0, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_active=0, reason=excluded.reason, updated_at=excluded.updated_at",
            (user_id, reason, now),
        )


def mark_user_active(user_id: int) -> None:
    now = time.time()
    with _conn() as con:
        con.execute(
            "INSERT INTO user_states (user_id, is_active, reason, updated_at) "
            "VALUES (?, 1, NULL, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_active=1, reason=NULL, updated_at=excluded.updated_at",
            (user_id, now),
        )


def is_user_active(user_id: int) -> bool:
    with _conn() as con:
        row = con.execute("SELECT is_active FROM user_states WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return True
        return bool(row["is_active"])


def get_inactive_users() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT user_id, reason, updated_at FROM user_states WHERE is_active = 0 ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_setting(key: str, default: str | None = None) -> str | None:
    with _conn() as con:
        row = con.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


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
