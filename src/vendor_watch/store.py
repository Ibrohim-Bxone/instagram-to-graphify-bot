"""SQLite persistent store for vendor watch events, source metadata, and outbox."""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from .parser import strip_html


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
        # Ensure existing events table has all expected columns before index creation
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
        if cur.fetchone():
            cur.execute("PRAGMA table_info(events)")
            cols = {row[1] for row in cur.fetchall()}
            for col, col_type in [
                ("source_id", "TEXT"),
                ("entry_id", "TEXT"),
                ("vendor", "TEXT"),
                ("product", "TEXT"),
                ("title", "TEXT"),
                ("link", "TEXT"),
                ("published_at", "TEXT"),
                ("content_hash", "TEXT"),
                ("revision", "INTEGER DEFAULT 1"),
                ("raw_content", "TEXT"),
                ("triage_type", "TEXT DEFAULT ''"),
                ("priority", "TEXT DEFAULT ''"),
                ("cluster_id", "TEXT DEFAULT ''"),
                ("status", "TEXT DEFAULT 'unseen'"),
                ("first_seen_at", "TEXT"),
                ("last_seen_at", "TEXT"),
            ]:
                if col not in cols:
                    try:
                        conn.execute(f"ALTER TABLE events ADD COLUMN {col} {col_type}")
                    except Exception:
                        pass

        # Ensure outbox table has revision column
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox'")
        if cur.fetchone():
            cur.execute("PRAGMA table_info(outbox)")
            out_cols = {row[1] for row in cur.fetchall()}
            if "revision" not in out_cols:
                try:
                    conn.execute("ALTER TABLE outbox ADD COLUMN revision INTEGER DEFAULT 1")
                except Exception:
                    pass

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
                created_at TEXT,
                revision INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS processing_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS outbox_deliveries (
                outbox_id INTEGER,
                chat_id TEXT,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_attempt_at TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                PRIMARY KEY (outbox_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS event_notifications (
                event_id TEXT PRIMARY KEY,
                status TEXT DEFAULT 'pending',
                notified_at TEXT
            );

            CREATE TABLE IF NOT EXISTS outbox_events (
                outbox_id INTEGER,
                event_id TEXT,
                revision INTEGER DEFAULT 1,
                PRIMARY KEY (outbox_id, event_id, revision)
            );

            CREATE TABLE IF NOT EXISTS probe_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                probe_name TEXT,
                snapshot_json TEXT,
                status TEXT DEFAULT 'success',
                created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id);
            CREATE INDEX IF NOT EXISTS idx_events_published ON events(published_at);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
            CREATE INDEX IF NOT EXISTS idx_processing_jobs_status ON processing_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_status ON outbox_deliveries(status);
            CREATE INDEX IF NOT EXISTS idx_outbox_events_event ON outbox_events(event_id);
            CREATE INDEX IF NOT EXISTS idx_outbox_events_outbox ON outbox_events(outbox_id);
            CREATE INDEX IF NOT EXISTS idx_probe_snapshots_name ON probe_snapshots(probe_name, id DESC);
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
            alt_hashes = {
                chash,
                make_content_hash(entry.title, strip_html(entry.raw_content)),
                make_content_hash(entry.title, entry.summary),
                make_content_hash(entry.title, strip_html(entry.summary)),
            }
            legacy_c = getattr(entry, "legacy_raw_content", "")
            if legacy_c:
                alt_hashes.add(make_content_hash(entry.title, legacy_c))
                alt_hashes.add(make_content_hash(entry.title, strip_html(legacy_c)))

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
                conn.execute(
                    """
                    INSERT INTO processing_jobs (event_id, status, created_at, updated_at)
                    VALUES (?, 'pending', ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET status = 'pending', updated_at = excluded.updated_at
                    """,
                    (event_id, now_iso, now_iso),
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
                stored_hash = existing.get("content_hash", "")
                if stored_hash not in alt_hashes:
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
                    conn.execute(
                        """
                        INSERT INTO processing_jobs (event_id, status, created_at, updated_at)
                        VALUES (?, 'pending', ?, ?)
                        ON CONFLICT(event_id) DO UPDATE SET status = 'pending', updated_at = excluded.updated_at
                        """,
                        (event_id, now_iso, now_iso),
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
                    if stored_hash != chash:
                        conn.execute(
                            "UPDATE events SET content_hash = ?, last_seen_at = ? WHERE id = ?",
                            (chash, now_iso, event_id),
                        )
                    else:
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
    revision: int = 1,
) -> int:
    now_iso = datetime.now().isoformat()
    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO outbox (event_id, priority, rendered_text, status, attempts, created_at, revision)
            VALUES (?, ?, ?, 'pending', 0, ?, ?)
            """,
            (event_id, priority, rendered_text, now_iso, revision),
        )
        return cur.lastrowid


def link_outbox_events(
    conn: sqlite3.Connection, outbox_id: int, events: list[dict]
) -> None:
    """Link all member events and their revisions to an outbox entry."""
    with conn:
        for ev in events:
            member_ids = ev.get("member_ids") or [ev["id"]]
            rev = ev.get("revision", 1)
            for mid in member_ids:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO outbox_events (outbox_id, event_id, revision)
                    VALUES (?, ?, ?)
                    """,
                    (outbox_id, mid, rev),
                )


def find_existing_outbox_for_events(
    conn: sqlite3.Connection, events: list[dict]
) -> dict | None:
    """Check if an outbox entry already exists that covers all member events for their current revisions."""
    all_mids: list[tuple[str, int]] = []
    for ev in events:
        mids = ev.get("member_ids") or [ev["id"]]
        rev = ev.get("revision", 1)
        for mid in mids:
            all_mids.append((mid, rev))
    if not all_mids:
        return None

    cur = conn.cursor()
    first_mid, first_rev = all_mids[0]
    cur.execute(
        """
        SELECT o.*
        FROM outbox_events oe
        JOIN outbox o ON o.id = oe.outbox_id
        WHERE oe.event_id = ? AND oe.revision = ?
        ORDER BY o.id DESC
        """,
        (first_mid, first_rev),
    )
    candidates = cur.fetchall()
    for cand in candidates:
        oid = cand["id"]
        cur.execute("SELECT event_id, revision FROM outbox_events WHERE outbox_id = ?", (oid,))
        linked = set((r[0], r[1]) for r in cur.fetchall())
        if all((m, r) in linked for m, r in all_mids):
            return dict(cand)
    return None


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
        # Update notification status and processing jobs for all linked member events
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT event_id FROM outbox_events WHERE outbox_id = ?", (outbox_id,))
        linked_mids = [r[0] for r in cur.fetchall()]
        if not linked_mids:
            cur.execute("SELECT event_id FROM outbox WHERE id = ?", (outbox_id,))
            row = cur.fetchone()
            if row and row[0] not in ("digest", "merged_high"):
                linked_mids = [row[0]]

        for mid in linked_mids:
            conn.execute(
                """
                INSERT INTO event_notifications (event_id, status, notified_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET status = excluded.status, notified_at = excluded.notified_at
                """,
                (mid, status, now_iso),
            )
            conn.execute(
                "UPDATE events SET status = ? WHERE id = ?",
                (status, mid),
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


def get_pending_processing_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Retrieve unfinished processing jobs joined with event details."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pj.id AS job_id, pj.event_id AS event_id, pj.status AS job_status, pj.attempts AS job_attempts,
               e.*
        FROM processing_jobs pj
        JOIN events e ON pj.event_id = e.id
        WHERE pj.status = 'pending'
        ORDER BY pj.id ASC
        """
    )
    results = []
    for row in cur.fetchall():
        d = dict(row)
        d["summary"] = d.get("summary") or d.get("raw_content") or ""
        d["links"] = [d["link"]] if d.get("link") else []
        results.append(d)
    return results


def mark_processing_job_done(conn: sqlite3.Connection, event_id: str) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute(
            """
            UPDATE processing_jobs
            SET status = 'completed', updated_at = ?
            WHERE event_id = ?
            """,
            (now_iso, event_id),
        )


def mark_processing_job_failed(
    conn: sqlite3.Connection, event_id: str, error_message: str = ""
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute(
            """
            UPDATE processing_jobs
            SET status = 'failed', attempts = attempts + 1, updated_at = ?
            WHERE event_id = ?
            """,
            (now_iso, event_id),
        )


def get_app_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_state WHERE key = ?", (key,))
    row = cur.fetchone()
    return str(row["value"]) if row else default


def set_app_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(value), now_iso),
        )


def init_outbox_deliveries(
    conn: sqlite3.Connection, outbox_id: int, chat_ids: list[str]
) -> None:
    with conn:
        for cid in chat_ids:
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox_deliveries (outbox_id, chat_id, status)
                VALUES (?, ?, 'pending')
                """,
                (outbox_id, str(cid)),
            )


def get_pending_delivery_chat_ids(
    conn: sqlite3.Connection, outbox_id: int
) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT chat_id FROM outbox_deliveries
        WHERE outbox_id = ? AND status != 'sent'
        ORDER BY chat_id ASC
        """,
        (outbox_id,),
    )
    return [str(r["chat_id"]) for r in cur.fetchall()]


def record_delivery_attempt(
    conn: sqlite3.Connection,
    outbox_id: int,
    chat_id: str,
    success: bool,
    error: str = "",
) -> None:
    now_iso = datetime.now().isoformat()
    status = "sent" if success else "failed"
    with conn:
        conn.execute(
            """
            UPDATE outbox_deliveries
            SET status = ?, attempts = attempts + 1, last_attempt_at = ?, error_message = ?
            WHERE outbox_id = ? AND chat_id = ?
            """,
            (status, now_iso, error, outbox_id, str(chat_id)),
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS pending_cnt FROM outbox_deliveries
            WHERE outbox_id = ? AND status != 'sent'
            """,
            (outbox_id,),
        )
        row = cur.fetchone()
        all_sent = (row["pending_cnt"] == 0) if row else False
        out_status = "sent" if all_sent else "failed"
        conn.execute(
            """
            UPDATE outbox
            SET status = ?, attempts = attempts + 1, last_attempt_at = ?,
                error_message = CASE WHEN ? THEN '' ELSE ? END
            WHERE id = ?
            """,
            (out_status, now_iso, all_sent, error, outbox_id),
        )
        if all_sent:
            cur.execute("SELECT DISTINCT event_id FROM outbox_events WHERE outbox_id = ?", (outbox_id,))
            linked_mids = [r[0] for r in cur.fetchall()]
            if not linked_mids:
                cur.execute("SELECT event_id FROM outbox WHERE id = ?", (outbox_id,))
                o_row = cur.fetchone()
                if o_row and o_row[0] not in ("digest", "merged_high"):
                    linked_mids = [o_row[0]]
            for mid in linked_mids:
                conn.execute(
                    """
                    INSERT INTO event_notifications (event_id, status, notified_at)
                    VALUES (?, 'sent', ?)
                    ON CONFLICT(event_id) DO UPDATE SET status = 'sent', notified_at = excluded.notified_at
                    """,
                    (mid, now_iso),
                )
                conn.execute("UPDATE events SET status = 'sent' WHERE id = ?", (mid,))


def mark_event_notified(
    conn: sqlite3.Connection, event_id: str, status: str = "sent"
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO event_notifications (event_id, status, notified_at)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET status = excluded.status, notified_at = excluded.notified_at
            """,
            (event_id, status, now_iso),
        )
        conn.execute(
            "UPDATE events SET status = ? WHERE id = ?",
            (status, event_id),
        )


def get_outbox_for_event(
    conn: sqlite3.Connection, event_id: str, revision: int | None = None
) -> dict | None:
    cur = conn.cursor()
    if revision is not None:
        cur.execute(
            "SELECT * FROM outbox WHERE event_id = ? AND revision = ? ORDER BY id DESC LIMIT 1",
            (event_id, revision),
        )
    else:
        cur.execute(
            "SELECT * FROM outbox WHERE event_id = ? ORDER BY id DESC LIMIT 1",
            (event_id,),
        )
    row = cur.fetchone()
    return dict(row) if row else None


def get_recent_events_for_clustering(
    conn: sqlite3.Connection, hours: int = 24
) -> list[dict]:
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*,
               COALESCE(
                   (SELECT en.status FROM event_notifications en WHERE en.event_id = e.id),
                   o.status,
                   CASE WHEN e.status IN ('sent', 'pending', 'notified') THEN e.status ELSE NULL END
               ) as outbox_status
        FROM events e
        LEFT JOIN outbox o ON o.event_id = e.id
        WHERE e.first_seen_at >= ? OR e.published_at >= ?
        ORDER BY e.first_seen_at DESC
        """,
        (cutoff, cutoff),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        d["links"] = [d["link"]] if d.get("link") else []
        rows.append(d)
    return rows


def save_probe_snapshot(
    conn: sqlite3.Connection,
    probe_name: str,
    snapshot: dict,
    status: str = "success",
) -> int:
    """Save structured probe snapshot to SQLite."""
    now_iso = datetime.now().isoformat()
    snap_json = json.dumps(snapshot, ensure_ascii=False)
    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO probe_snapshots (probe_name, snapshot_json, status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (probe_name, snap_json, status, now_iso),
        )
        return cur.lastrowid


def get_latest_probe_snapshot(
    conn: sqlite3.Connection,
    probe_name: str,
    only_successful: bool = True,
) -> dict | None:
    """Retrieve the most recent probe snapshot, optionally filtering for success."""
    cur = conn.cursor()
    if only_successful:
        cur.execute(
            """
            SELECT snapshot_json FROM probe_snapshots
            WHERE probe_name = ? AND status = 'success'
            ORDER BY id DESC LIMIT 1
            """,
            (probe_name,),
        )
    else:
        cur.execute(
            """
            SELECT snapshot_json FROM probe_snapshots
            WHERE probe_name = ?
            ORDER BY id DESC LIMIT 1
            """,
            (probe_name,),
        )
    row = cur.fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            return None
    return None


def record_probe_events(
    conn: sqlite3.Connection,
    probe_events: list[dict],
) -> list[dict]:
    """Store probe-derived account change events in SQLite events table."""
    now_iso = datetime.now().isoformat()
    new_events = []
    with conn:
        for ev in probe_events:
            event_id = ev["id"]
            source_id = ev.get("source_id", "probe")
            entry_id = ev.get("entry_id", event_id)
            vendor = ev.get("vendor", "")
            product = ev.get("product", "")
            title = ev.get("title", "")
            link = ev.get("link", "")
            published_at = ev.get("published_at", now_iso)
            raw_content = ev.get("raw_content", "")
            triage_type = ev.get("triage_type", "account_change")
            priority = ev.get("priority", "high")
            chash = make_content_hash(title, raw_content)

            cur = conn.cursor()
            cur.execute("SELECT id FROM events WHERE id = ?", (event_id,))
            if cur.fetchone() is None:
                conn.execute(
                    """
                    INSERT INTO events
                    (id, source_id, entry_id, vendor, product, title, link, published_at,
                     content_hash, revision, raw_content, triage_type, priority, status, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'unseen', ?, ?)
                    """,
                    (
                        event_id,
                        source_id,
                        entry_id,
                        vendor,
                        product,
                        title,
                        link,
                        published_at,
                        chash,
                        raw_content,
                        triage_type,
                        priority,
                        now_iso,
                        now_iso,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO processing_jobs (event_id, status, created_at, updated_at)
                    VALUES (?, 'pending', ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET status = 'pending', updated_at = excluded.updated_at
                    """,
                    (event_id, now_iso, now_iso),
                )
                ev_copy = dict(ev)
                ev_copy.setdefault("summary", raw_content)
                ev_copy.setdefault("links", [link] if link else [])
                ev_copy.setdefault("revision", 1)
                new_events.append(ev_copy)
    return new_events


def get_latest_release_versions(conn: sqlite3.Connection) -> dict[str, str]:
    """Extract highest semver release version for tracked CLI tools from events table."""
    import re
    from .probes import compare_semver

    semver_re = re.compile(r"(\d+\.\d+\.\d+)")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT source_id, title, link, entry_id
        FROM events
        WHERE source_id IN ('openai-codex', 'anthropic-claude-code', 'google-gemini-cli')
        ORDER BY published_at DESC, id DESC
        """
    )
    versions: dict[str, str] = {}
    for row in cur.fetchall():
        sid = row["source_id"]
        text = f"{row['title']} {row['link']} {row['entry_id']}"
        m = semver_re.search(text)
        if m:
            ver = m.group(1)
            if sid not in versions or compare_semver(versions[sid], ver) < 0:
                versions[sid] = ver
    return versions


def get_announced_model_candidates(conn: sqlite3.Connection) -> set[str]:
    """Extract model identifiers mentioned in events for Vertex AI availability checks."""
    import re
    cur = conn.cursor()
    cur.execute(
        """
        SELECT title, raw_content
        FROM events
        WHERE triage_type = 'new_model' OR raw_content LIKE '%gemini-%'
        """
    )
    model_re = re.compile(r"\b(gemini-[a-zA-Z0-9.-]+)\b", re.IGNORECASE)
    candidates = set()
    for row in cur.fetchall():
        for text in (row["title"], row["raw_content"]):
            if text:
                for match in model_re.findall(text):
                    candidates.add(match.lower())
    return candidates

