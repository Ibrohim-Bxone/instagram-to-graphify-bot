"""SQLite persistent store for vendor watch events, source metadata, and outbox."""

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import sqlite3


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open SQLite connection and ensure schema is initialized."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources_meta (
                source_id TEXT PRIMARY KEY,
                etag TEXT DEFAULT '',
                last_modified TEXT DEFAULT '',
                last_fetched_at TEXT DEFAULT '',
                last_status INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                is_baseline_done INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                source_id TEXT,
                entry_id TEXT,
                vendor TEXT,
                product TEXT,
                title TEXT,
                link TEXT,
                published_at TEXT,
                content_hash TEXT,
                revision INTEGER DEFAULT 1,
                raw_content TEXT,
                triage_type TEXT DEFAULT '',
                priority TEXT DEFAULT '',
                cluster_id TEXT DEFAULT '',
                status TEXT DEFAULT 'unseen',
                first_seen_at TEXT,
                last_seen_at TEXT
            );

            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                priority TEXT,
                rendered_text TEXT,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_attempt_at TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id);
            CREATE INDEX IF NOT EXISTS idx_events_published ON events(published_at);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
            """
        )


def get_source_meta(conn: sqlite3.Connection, source_id: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM sources_meta WHERE source_id = ?", (source_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def mark_source_success(
    conn: sqlite3.Connection,
    source_id: str,
    etag: str = "",
    last_modified: str = "",
    is_baseline_done: bool | None = None,
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        existing = get_source_meta(conn, source_id)
        if existing:
            new_baseline = (
                int(is_baseline_done)
                if is_baseline_done is not None
                else existing["is_baseline_done"]
            )
            new_etag = etag if etag else existing["etag"]
            new_lm = last_modified if last_modified else existing["last_modified"]
            conn.execute(
                """
                UPDATE sources_meta
                SET etag = ?, last_modified = ?, last_fetched_at = ?,
                    last_status = 200, fail_count = 0, is_baseline_done = ?
                WHERE source_id = ?
                """,
                (new_etag, new_lm, now_iso, new_baseline, source_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO sources_meta
                (source_id, etag, last_modified, last_fetched_at, last_status, fail_count, is_baseline_done)
                VALUES (?, ?, ?, ?, 200, 0, ?)
                """,
                (source_id, etag, last_modified, now_iso, int(bool(is_baseline_done))),
            )


def mark_source_failure(
    conn: sqlite3.Connection, source_id: str, status_code: int = 0
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        existing = get_source_meta(conn, source_id)
        if existing:
            conn.execute(
                """
                UPDATE sources_meta
                SET last_fetched_at = ?, last_status = ?, fail_count = fail_count + 1
                WHERE source_id = ?
                """,
                (now_iso, status_code, source_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO sources_meta
                (source_id, etag, last_modified, last_fetched_at, last_status, fail_count, is_baseline_done)
                VALUES (?, '', '', ?, ?, 1, 0)
                """,
                (source_id, now_iso, status_code),
            )


def make_content_hash(title: str, content: str) -> str:
    combined = f"{title.strip()}||{content.strip()}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def record_baseline(
    conn: sqlite3.Connection,
    source: dict,
    entries: list,
    etag: str = "",
    last_modified: str = "",
) -> None:
    """Record initial baseline entries as seen, so they are not treated as new events."""
    now_iso = datetime.now().isoformat()
    source_id = source["id"]
    vendor = source.get("vendor", "")
    product = source.get("product", "")

    with conn:
        for entry in entries:
            event_id = f"{source_id}:{entry.entry_id}"
            chash = make_content_hash(entry.title, entry.raw_content)
            conn.execute(
                """
                INSERT OR IGNORE INTO events
                (id, source_id, entry_id, vendor, product, title, link, published_at,
                 content_hash, revision, raw_content, status, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'baseline', ?, ?)
                """,
                (
                    event_id,
                    source_id,
                    entry.entry_id,
                    vendor,
                    product,
                    entry.title,
                    entry.link,
                    entry.published_at,
                    chash,
                    entry.raw_content,
                    now_iso,
                    now_iso,
                ),
            )

        mark_source_success(
            conn,
            source_id,
            etag=etag,
            last_modified=last_modified,
            is_baseline_done=True,
        )


def record_unseen_entries(
    conn: sqlite3.Connection,
    source: dict,
    entries: list,
    etag: str = "",
    last_modified: str = "",
) -> list[dict]:
    """Record entries and return only newly discovered or revised entries."""
    now_iso = datetime.now().isoformat()
    source_id = source["id"]
    vendor = source.get("vendor", "")
    product = source.get("product", "")
    new_events = []

    with conn:
        for entry in entries:
            event_id = f"{source_id}:{entry.entry_id}"
            chash = make_content_hash(entry.title, entry.raw_content)

            cur = conn.cursor()
            cur.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = cur.fetchone()

            if row is None:
                # Completely new event
                conn.execute(
                    """
                    INSERT INTO events
                    (id, source_id, entry_id, vendor, product, title, link, published_at,
                     content_hash, revision, raw_content, status, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'unseen', ?, ?)
                    """,
                    (
                        event_id,
                        source_id,
                        entry.entry_id,
                        vendor,
                        product,
                        entry.title,
                        entry.link,
                        entry.published_at,
                        chash,
                        entry.raw_content,
                        now_iso,
                        now_iso,
                    ),
                )
                new_events.append({
                    "id": event_id,
                    "source_id": source_id,
                    "entry_id": entry.entry_id,
                    "vendor": vendor,
                    "product": product,
                    "title": entry.title,
                    "link": entry.link,
                    "published_at": entry.published_at,
                    "summary": entry.summary,
                    "raw_content": entry.raw_content,
                    "content_hash": chash,
                    "revision": 1,
                    "links": [entry.link] if entry.link else [],
                })
            else:
                existing = dict(row)
                if existing["content_hash"] != chash:
                    # Content updated (new revision)
                    new_rev = existing["revision"] + 1
                    conn.execute(
                        """
                        UPDATE events
                        SET title = ?, link = ?, published_at = ?,
                            content_hash = ?, revision = ?, raw_content = ?,
                            last_seen_at = ?, status = 'revised'
                        WHERE id = ?
                        """,
                        (
                            entry.title,
                            entry.link,
                            entry.published_at,
                            chash,
                            new_rev,
                            entry.raw_content,
                            now_iso,
                            event_id,
                        ),
                    )
                    new_events.append({
                        "id": event_id,
                        "source_id": source_id,
                        "entry_id": entry.entry_id,
                        "vendor": vendor,
                        "product": product,
                        "title": entry.title,
                        "link": entry.link,
                        "published_at": entry.published_at,
                        "summary": entry.summary,
                        "raw_content": entry.raw_content,
                        "content_hash": chash,
                        "revision": new_rev,
                        "links": [entry.link] if entry.link else [],
                    })
                else:
                    # Seen before, unchanged
                    conn.execute(
                        "UPDATE events SET last_seen_at = ? WHERE id = ?",
                        (now_iso, event_id),
                    )

        mark_source_success(
            conn,
            source_id,
            etag=etag,
            last_modified=last_modified,
            is_baseline_done=True,
        )

    return new_events


def get_recent_baseline_entries(
    conn: sqlite3.Connection, days: int = 7
) -> list[dict]:
    """Retrieve entries published in the last N days for baseline digest."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM events
        WHERE published_at >= ?
        ORDER BY published_at DESC
        """,
        (cutoff,),
    )
    return [dict(r) for r in cur.fetchall()]


def add_to_outbox(
    conn: sqlite3.Connection,
    event_id: str,
    priority: str,
    rendered_text: str,
) -> int:
    now_iso = datetime.now().isoformat()
    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO outbox (event_id, priority, rendered_text, status, attempts, created_at)
            VALUES (?, ?, ?, 'pending', 0, ?)
            """,
            (event_id, priority, rendered_text, now_iso),
        )
        return cur.lastrowid


def get_pending_outbox(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM outbox
        WHERE status IN ('pending', 'failed')
        ORDER BY id ASC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def update_outbox_status(
    conn: sqlite3.Connection,
    outbox_id: int,
    status: str,
    error_message: str = "",
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute(
            """
            UPDATE outbox
            SET status = ?, attempts = attempts + 1, last_attempt_at = ?, error_message = ?
            WHERE id = ?
            """,
            (status, now_iso, error_message, outbox_id),
        )


def count_today_high_priority(conn: sqlite3.Connection) -> int:
    today_start = datetime.now().date().isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) AS cnt FROM outbox
        WHERE priority = 'high' AND status = 'sent' AND created_at >= ?
        """,
        (today_start,),
    )
    row = cur.fetchone()
    return int(row["cnt"]) if row else 0
