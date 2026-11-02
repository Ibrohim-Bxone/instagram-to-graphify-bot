"""SQLite persistent store for vendor watch events, source metadata, and outbox."""

from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import sqlite3

from .delivery_state import derive_status
from .parser import strip_html

log = logging.getLogger("vendor_watch.store")


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open SQLite connection and ensure schema is initialized."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn

def _derive_consistent_outbox_and_page_statuses(conn: sqlite3.Connection) -> None:
    """Derive consistent aggregate outbox and outbox_pages status from outbox_deliveries.

    Preserves per recipient/page delivery evidence. Group signature/membership equivalence
    alone cannot justify global/page 'sent' while any required recipient is pending or failed.
    Does NOT turn ambiguous status into retry/success.
    """
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox'")
    if not cur.fetchone():
        return
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_deliveries'")
    if not cur.fetchone():
        return
    cur.execute("PRAGMA table_info(outbox_deliveries)")
    od_cols = {r[1] for r in cur.fetchall()}
    has_page_col = "page_number" in od_cols

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_pages'")
    has_op = bool(cur.fetchone())

    # Only process outbox rows that actually have delivery records
    cur.execute("SELECT DISTINCT outbox_id FROM outbox_deliveries")
    outbox_ids = [r[0] for r in cur.fetchall()]
    if not outbox_ids:
        return

    for oid in outbox_ids:
        cur.execute("SELECT status FROM outbox WHERE id = ?", (oid,))
        row = cur.fetchone()
        if not row:
            continue
        cur_out_status = row[0]

        if has_op:
            cur.execute("SELECT page_number, status FROM outbox_pages WHERE outbox_id = ? ORDER BY page_number", (oid,))
            pages = cur.fetchall()
        else:
            pages = []

        if pages:
            for p_num, p_st in pages:
                if has_page_col:
                    cur.execute(
                        "SELECT chat_id, status FROM outbox_deliveries WHERE outbox_id = ? AND page_number = ?",
                        (oid, p_num),
                    )
                else:
                    cur.execute(
                        "SELECT chat_id, status FROM outbox_deliveries WHERE outbox_id = ?",
                        (oid,),
                    )
                delivs = cur.fetchall()
                if not delivs:
                    continue
                new_p_st = derive_status([st for _, st in delivs])
                if new_p_st != p_st:
                    conn.execute(
                        "UPDATE outbox_pages SET status = ? WHERE outbox_id = ? AND page_number = ?",
                        (new_p_st, oid, p_num),
                    )

            cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ?", (oid,))
            p_statuses = [r[0] for r in cur.fetchall()]
            new_out_st = derive_status(p_statuses)
            if new_out_st == "failed" and cur_out_status == "failed_final":
                new_out_st = "failed_final"
        else:
            cur.execute("SELECT chat_id, status FROM outbox_deliveries WHERE outbox_id = ?", (oid,))
            delivs = cur.fetchall()
            if not delivs:
                continue
            new_out_st = derive_status([st for _, st in delivs])
            if new_out_st == "failed" and cur_out_status == "failed_final":
                new_out_st = "failed_final"

        if new_out_st != cur_out_status:
            conn.execute("UPDATE outbox SET status = ? WHERE id = ?", (new_out_st, oid))


def collapse_outbox_duplicates(conn: sqlite3.Connection) -> list[dict]:
    """Migrate legacy outbox rows and collapse duplicate rows on startup.

    Keeps the earliest sent row or the newest pending row.
    Merges recipient delivery records into the kept row (never deleting sent records).
    Legacy group rows stay as sent history without re-linking members.
    Logs what was collapsed and returns list of collapsed records.
    """
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox'")
    if not cur.fetchone():
        return []

    cur.execute("PRAGMA table_info(outbox)")
    cols = {r[1] for r in cur.fetchall()}
    if "kind" in cols:
        conn.execute("UPDATE outbox SET kind = priority WHERE (kind IS NULL OR kind = '') AND priority != ''")

    # Step 1: Assign stable keys to any outbox rows that lack one
    cur.execute(
        """
        SELECT id, event_id, priority, COALESCE(NULLIF(kind, ''), priority, '') AS kind_val,
               status, attempts, created_at, revision, rendered_text, key
        FROM outbox
        ORDER BY id ASC
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return []

    # Pre-fetch existing outbox_events and outbox_pages for accurate membership & payload grouping
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_events'")
    has_oe = bool(cur.fetchone())
    outbox_events_by_id: dict[int, list[tuple[str, int]]] = {}
    oe_has_rev = False
    if has_oe:
        cur.execute("PRAGMA table_info(outbox_events)")
        oe_cols = {r[1] for r in cur.fetchall()}
        oe_has_rev = "revision" in oe_cols
        if oe_has_rev:
            cur.execute("SELECT outbox_id, event_id, revision FROM outbox_events ORDER BY outbox_id, event_id, revision")
            for oid, mid, mrev in cur.fetchall():
                outbox_events_by_id.setdefault(oid, []).append((mid, mrev))
        else:
            cur.execute("SELECT outbox_id, event_id, 1 FROM outbox_events ORDER BY outbox_id, event_id")
            for oid, mid, mrev in cur.fetchall():
                outbox_events_by_id.setdefault(oid, []).append((mid, mrev))

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_pages'")
    has_op = bool(cur.fetchone())
    outbox_pages_by_id: dict[int, list[tuple]] = {}
    if has_op:
        cur.execute("PRAGMA table_info(outbox_pages)")
        op_cols = {r[1] for r in cur.fetchall()}
        has_mids = "member_ids_json" in op_cols
        if has_mids:
            cur.execute("SELECT outbox_id, page_number, rendered_text, member_ids_json FROM outbox_pages ORDER BY outbox_id, page_number")
        else:
            cur.execute("SELECT outbox_id, page_number, rendered_text, '[]' FROM outbox_pages ORDER BY outbox_id, page_number")
        for oid, pnum, ptext, pmids in cur.fetchall():
            outbox_pages_by_id.setdefault(oid, []).append((pnum, ptext or "", pmids or ""))

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_deliveries'")
    has_od = bool(cur.fetchone())
    has_page_col = False
    if has_od:
        cur.execute("PRAGMA table_info(outbox_deliveries)")
        od_cols = {r[1] for r in cur.fetchall()}
        has_page_col = "page_number" in od_cols

    # For legacy items of the same event_id, find the earliest row's revision so that
    # duplicates of the same item share the same target_key
    legacy_earliest_rev: dict[str, int] = {}
    for r in rows:
        if not r.get("key"):
            eid = r.get("event_id") or ""
            if eid and not eid.startswith("merged_high") and not eid.startswith("digest") and eid != "digest":
                if eid not in legacy_earliest_rev:
                    legacy_earliest_rev[eid] = r.get("revision") or 1

    # Group legacy digest rows by date.
    # Defect 2: Only truly identical content AND membership can collapse. Keep different legacy payload/member sets distinct with stable unique legacy keys.
    # When multiple pending rows exist for the same pending digest with empty membership, they are un-sent drafts that collapse into the newest pending row.
    legacy_digest_rows_by_date: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("key"):
            eid = r.get("event_id") or ""
            if eid == "digest" or eid.startswith("digest:"):
                d_str = eid.removeprefix("digest:")[:10] if eid.startswith("digest:") else (r.get("created_at") or "legacy")[:10]
                r_text = (r.get("rendered_text") or "").strip()
                r_events = tuple(outbox_events_by_id.get(r["id"], []))
                r_pages = tuple(outbox_pages_by_id.get(r["id"], []))
                r["_sig"] = (r_text, r_events, r_pages)
                r["_events"] = r_events
                r["_date_str"] = d_str
                legacy_digest_rows_by_date.setdefault(d_str, []).append(r)

    # For each date, partition rows into equivalence classes that can collapse together
    assigned_digest_keys: dict[int, str] = {}
    for d_str, d_rows in legacy_digest_rows_by_date.items():
        d_groups: list[list[dict]] = []
        for r in d_rows:
            matched = False
            for grp in d_groups:
                first = grp[0]
                if r["_sig"] == first["_sig"] or (
                    r.get("status") == "pending" and first.get("status") == "pending" and not r["_events"] and not first["_events"]
                ):
                    grp.append(r)
                    matched = True
                    break
            if not matched:
                d_groups.append([r])

        existing_keyed = next((x for x in rows if x.get("key") == f"digest:{d_str}"), None)
        existing_sig = None
        if existing_keyed:
            ex_text = (existing_keyed.get("rendered_text") or "").strip()
            ex_events = tuple(outbox_events_by_id.get(existing_keyed["id"], []))
            ex_pages = tuple(outbox_pages_by_id.get(existing_keyed["id"], []))
            existing_sig = (ex_text, ex_events, ex_pages)

        def _group_priority(glist: list[dict]) -> tuple[int, str, int]:
            has_sent = any(x["status"] == "sent" for x in glist)
            earliest_created = min((x.get("created_at") or "") for x in glist)
            min_id = min(x["id"] for x in glist)
            return (0 if has_sent else 1, earliest_created, min_id)

        sorted_groups = sorted(d_groups, key=_group_priority)
        canonical_assigned = False

        for glist in sorted_groups:
            min_id = min(x["id"] for x in glist)
            first_sig = glist[0]["_sig"]
            if existing_keyed is not None:
                if first_sig == existing_sig:
                    t_k = f"digest:{d_str}"
                else:
                    t_k = f"legacy:digest:{d_str}:{min_id}"
            else:
                if not canonical_assigned:
                    t_k = f"digest:{d_str}"
                    canonical_assigned = True
                else:
                    t_k = f"legacy:digest:{d_str}:{min_id}"
            for item in glist:
                assigned_digest_keys[item["id"]] = t_k

    # Calculate target key and was_legacy in Python first (do NOT update DB yet)
    for r in rows:
        r["was_legacy"] = not bool(r.get("key"))
        eid = r.get("event_id") or ""
        prio = r.get("priority") or ""
        if r.get("key"):
            k = r["key"]
        elif eid.startswith("merged_high"):
            k = f"legacy:{eid}:{r['id']}"
        elif r["id"] in assigned_digest_keys:
            k = assigned_digest_keys[r["id"]]
        elif eid.startswith("probe:"):
            k = eid
        else:
            rev = legacy_earliest_rev.get(eid, r.get("revision") or 1)
            if prio == "high":
                k = f"high:{eid}:{rev}"
            else:
                k = f"{prio}:{eid}:{rev}"
        r["target_key"] = k

    # Step 2: Group duplicates
    groups: dict[str, list[dict]] = {}
    for r in rows:
        eid = r.get("event_id") or ""
        if eid.startswith("merged_high"):
            continue
        groups.setdefault(r["target_key"], []).append(r)

    collapsed_records = []
    with conn:
        for grp_key, group in groups.items():
            if len(group) <= 1:
                continue

            sent_rows = [r for r in group if r["status"] == "sent"]
            if sent_rows:
                sent_rows.sort(key=lambda r: (r.get("created_at") or "", r["id"]))
                kept = sent_rows[0]
            else:
                pending_rows = [r for r in group if r["status"] in ("pending", "failed")]
                if pending_rows:
                    pending_rows.sort(key=lambda r: (r.get("created_at") or "", r["id"]), reverse=True)
                    kept = pending_rows[0]
                else:
                    group.sort(key=lambda r: (r.get("created_at") or "", r["id"]), reverse=True)
                    kept = group[0]

            # Preserve all ambiguous/sending evidence from every row and page in group before deleting/re-pointing
            ambig_pages = set()
            for r in group:
                r_id = r["id"]
                r_is_ambig = r.get("status") in ("ambiguous", "sending")
                if r_is_ambig:
                    if has_od:
                        conn.execute(
                            "UPDATE outbox_deliveries SET status = 'ambiguous' WHERE outbox_id = ? AND status IN ('pending', 'sending')",
                            (r_id,),
                        )
                if has_op:
                    cur.execute(
                        "SELECT page_number, status FROM outbox_pages WHERE outbox_id = ?",
                        (r_id,),
                    )
                    for p_num, p_st in cur.fetchall():
                        if p_st in ("ambiguous", "sending") or r_is_ambig:
                            ambig_pages.add(p_num)
                            if has_page_col:
                                conn.execute(
                                    "UPDATE outbox_deliveries SET status = 'ambiguous' WHERE outbox_id = ? AND page_number = ? AND status IN ('pending', 'sending')",
                                    (r_id, p_num),
                                )

            if has_op and ambig_pages:
                for p_num in ambig_pages:
                    conn.execute(
                        "UPDATE outbox_pages SET status = 'ambiguous' WHERE outbox_id = ? AND page_number = ?",
                        (kept["id"], p_num),
                    )

            group_has_ambig = any(r.get("status") in ("ambiguous", "sending") for r in group) or bool(ambig_pages)
            if group_has_ambig and kept["status"] != "ambiguous":
                conn.execute("UPDATE outbox SET status = 'ambiguous' WHERE id = ?", (kept["id"],))
                kept["status"] = "ambiguous"

            to_remove = [r for r in group if r["id"] != kept["id"]]
            for r in to_remove:
                remove_id = r["id"]
                # Re-point delivery records to kept outbox row without deleting any delivery record
                conn.execute("UPDATE outbox_deliveries SET outbox_id = ? WHERE outbox_id = ?", (kept["id"], remove_id))

                # Preserve outbox_pages for kept row
                if has_op:
                    conn.execute(
                        """
                        DELETE FROM outbox_pages
                        WHERE outbox_id = ?
                          AND page_number IN (
                              SELECT page_number FROM outbox_pages WHERE outbox_id = ?
                          )
                        """,
                        (remove_id, kept["id"]),
                    )
                    conn.execute("UPDATE outbox_pages SET outbox_id = ? WHERE outbox_id = ?", (kept["id"], remove_id))
                    if r.get("status") in ("ambiguous", "sending"):
                        conn.execute(
                            "UPDATE outbox_pages SET status = 'ambiguous' WHERE outbox_id = ? AND status IN ('pending', 'sending')",
                            (kept["id"],),
                        )

                # Merge outbox_events into kept row transactionally without conflict with idx_outbox_events_member
                if has_oe:
                    if oe_has_rev:
                        conn.execute(
                            """
                            DELETE FROM outbox_events
                            WHERE outbox_id = ?
                              AND (event_id, revision) IN (
                                  SELECT event_id, revision FROM outbox_events WHERE outbox_id = ?
                              )
                            """,
                            (remove_id, kept["id"]),
                        )
                    else:
                        conn.execute(
                            """
                            DELETE FROM outbox_events
                            WHERE outbox_id = ?
                              AND event_id IN (
                                  SELECT event_id FROM outbox_events WHERE outbox_id = ?
                              )
                            """,
                            (remove_id, kept["id"]),
                        )
                    conn.execute("UPDATE outbox_events SET outbox_id = ? WHERE outbox_id = ?", (kept["id"], remove_id))

                conn.execute("DELETE FROM outbox WHERE id = ?", (remove_id,))
                collapsed_records.append(r)

            remove_ids = [r["id"] for r in to_remove]
            log.info(
                "Collapsed %d duplicate outbox rows for group '%s': kept id %d (status=%s), removed ids: %s",
                len(to_remove), grp_key, kept["id"], kept["status"], remove_ids,
            )

        # Now update keys on ALL remaining rows that lack a key
        removed_ids_set = {r["id"] for r in collapsed_records}
        for r in rows:
            if r["id"] not in removed_ids_set and (not r.get("key")):
                conn.execute("UPDATE outbox SET key = ? WHERE id = ?", (r["target_key"], r["id"]))
                r["key"] = r["target_key"]

        # Ensure indexes exist
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_event_kind ON outbox(event_id, kind)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_key ON outbox(key)")

        # Step 3: Derive consistent aggregate status for outbox and outbox_pages from outbox_deliveries
        _derive_consistent_outbox_and_page_statuses(conn)

    return collapsed_records


def _recover_sending_outbox(conn: sqlite3.Connection) -> int:
    """Startup crash recovery for sending rows.

    F1 & F3: Marks in-flight deliveries and pages left in 'sending' as 'ambiguous'.
    Preserves unattempted 'pending' and completed 'sent' records.
    Recovers outbox status based on remaining page/delivery work:
    - if unattempted work remains ('pending'/'failed'), keeps outbox as 'pending'
    - if all work was 'sent', marks outbox as 'sent'
    - if all work was resolved and any was 'ambiguous', marks outbox as 'ambiguous'.
    """
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox'")
    if not cur.fetchone():
        return 0

    # 1. Any delivery left in 'sending' becomes 'ambiguous' (at-most-once safety)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_deliveries'")
    if cur.fetchone():
        conn.execute("UPDATE outbox_deliveries SET status = 'ambiguous' WHERE status = 'sending'")

    # 2. Any page left in 'sending' becomes 'ambiguous'
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_pages'")
    has_pages_table = bool(cur.fetchone())
    if has_pages_table:
        conn.execute("UPDATE outbox_pages SET status = 'ambiguous' WHERE status = 'sending'")

    # 3. Recover outbox rows left in 'sending'
    cur.execute("SELECT id FROM outbox WHERE status = 'sending'")
    sending_ids = [r[0] for r in cur.fetchall()]
    for oid in sending_ids:
        has_pages = False
        if has_pages_table:
            cur.execute("SELECT COUNT(*) FROM outbox_pages WHERE outbox_id = ?", (oid,))
            has_pages = (cur.fetchone()[0] > 0)

        if has_pages:
            cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ?", (oid,))
            p_statuses = [r[0] for r in cur.fetchall()]
            if not p_statuses:
                new_st = "ambiguous"
            else:
                new_st = derive_status(p_statuses)
                if new_st == "failed":
                    cur.execute("SELECT attempts FROM outbox WHERE id = ?", (oid,))
                    att_r = cur.fetchone()
                    att = (att_r[0] if att_r else 0) or 0
                    new_st = "failed_final" if att >= 3 else "failed"
        else:
            cur.execute("SELECT status FROM outbox_deliveries WHERE outbox_id = ?", (oid,))
            d_statuses = [r[0] for r in cur.fetchall()]
            if not d_statuses:
                new_st = "ambiguous"
            else:
                new_st = derive_status(d_statuses)
                if new_st == "failed":
                    cur.execute("SELECT attempts FROM outbox WHERE id = ?", (oid,))
                    att_r = cur.fetchone()
                    att = (att_r[0] if att_r else 0) or 0
                    new_st = "failed_final" if att >= 3 else "failed"
        conn.execute("UPDATE outbox SET status = ? WHERE id = ?", (new_st, oid))

    return len(sending_ids)


def _init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        # Step 1: Create every table first
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
                key TEXT,
                event_id TEXT,
                priority TEXT,
                rendered_text TEXT,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_attempt_at TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at TEXT,
                revision INTEGER DEFAULT 1,
                kind TEXT DEFAULT '',
                sent_at TEXT DEFAULT '',
                next_attempt_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS processing_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                revision INTEGER DEFAULT 1,
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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                outbox_id INTEGER,
                page_number INTEGER DEFAULT 1,
                chat_id TEXT,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_attempt_at TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                sent_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS event_notifications (
                event_id TEXT,
                revision INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                notified_at TEXT,
                PRIMARY KEY (event_id, revision)
            );

            CREATE TABLE IF NOT EXISTS outbox_events (
                outbox_id INTEGER,
                event_id TEXT,
                revision INTEGER DEFAULT 1,
                PRIMARY KEY (outbox_id, event_id, revision)
            );

            CREATE TABLE IF NOT EXISTS outbox_pages (
                outbox_id INTEGER,
                page_number INTEGER,
                rendered_text TEXT,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_attempt_at TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                member_ids_json TEXT DEFAULT '[]',
                PRIMARY KEY (outbox_id, page_number)
            );

            CREATE TABLE IF NOT EXISTS probe_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                probe_name TEXT,
                snapshot_json TEXT,
                status TEXT DEFAULT 'success',
                created_at TEXT
            );
            """
        )

        # Step 2: Ensure any upgraded columns exist on existing tables
        cur = conn.cursor()
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

        cur.execute("PRAGMA table_info(outbox)")
        out_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in [
            ("key", "TEXT"),
            ("revision", "INTEGER DEFAULT 1"),
            ("kind", "TEXT DEFAULT ''"),
            ("attempts", "INTEGER DEFAULT 0"),
            ("last_attempt_at", "TEXT DEFAULT ''"),
            ("error_message", "TEXT DEFAULT ''"),
            ("sent_at", "TEXT DEFAULT ''"),
            ("next_attempt_at", "TEXT DEFAULT ''"),
        ]:
            if col not in out_cols:
                try:
                    conn.execute(f"ALTER TABLE outbox ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='processing_jobs'")
        pj_row = cur.fetchone()
        if pj_row and "UNIQUE" in pj_row[0].upper():
            conn.execute("ALTER TABLE processing_jobs RENAME TO processing_jobs_old")
            conn.execute(
                """
                CREATE TABLE processing_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    revision INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            cur.execute("PRAGMA table_info(processing_jobs_old)")
            old_cols = {r[1] for r in cur.fetchall()}
            if "revision" in old_cols:
                conn.execute(
                    """
                    INSERT INTO processing_jobs (id, event_id, revision, status, attempts, created_at, updated_at)
                    SELECT id, event_id, revision, status, attempts, created_at, updated_at FROM processing_jobs_old
                    """
                )
            else:
                conn.execute(
                    """
                    INSERT INTO processing_jobs (id, event_id, revision, status, attempts, created_at, updated_at)
                    SELECT id, event_id, 1, status, attempts, created_at, updated_at FROM processing_jobs_old
                    """
                )
            conn.execute("DROP TABLE processing_jobs_old")
        else:
            cur.execute("PRAGMA table_info(processing_jobs)")
            pj_cols = {row[1] for row in cur.fetchall()}
            if "revision" not in pj_cols:
                try:
                    conn.execute("ALTER TABLE processing_jobs ADD COLUMN revision INTEGER DEFAULT 1")
                except Exception:
                    pass

        cur.execute("PRAGMA table_info(outbox_pages)")
        op_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in [
            ("attempts", "INTEGER DEFAULT 0"),
            ("last_attempt_at", "TEXT DEFAULT ''"),
            ("error_message", "TEXT DEFAULT ''"),
            ("member_ids_json", "TEXT DEFAULT '[]'"),
        ]:
            if col not in op_cols:
                try:
                    conn.execute(f"ALTER TABLE outbox_pages ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur.execute("PRAGMA table_info(outbox_deliveries)")
        od_cols = {row[1] for row in cur.fetchall()}
        if "page_number" not in od_cols or "id" not in od_cols:
            conn.execute("ALTER TABLE outbox_deliveries RENAME TO outbox_deliveries_old")
            conn.execute(
                """
                CREATE TABLE outbox_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outbox_id INTEGER,
                    page_number INTEGER DEFAULT 1,
                    chat_id TEXT,
                    status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    last_attempt_at TEXT DEFAULT '',
                    error_message TEXT DEFAULT ''
                )
                """
            )
            cur.execute("PRAGMA table_info(outbox_deliveries_old)")
            old_od_cols = {r[1] for r in cur.fetchall()}
            if "page_number" in old_od_cols:
                conn.execute(
                    """
                    INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, last_attempt_at, error_message)
                    SELECT outbox_id, page_number, chat_id, status, attempts, last_attempt_at, error_message FROM outbox_deliveries_old
                    """
                )
            else:
                conn.execute(
                    """
                    INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, last_attempt_at, error_message)
                    SELECT outbox_id, 1, chat_id, status, attempts, last_attempt_at, error_message FROM outbox_deliveries_old
                    """
                )
            conn.execute("DROP TABLE outbox_deliveries_old")

        cur.execute("PRAGMA table_info(outbox_deliveries)")
        od_cols_current = {row[1] for row in cur.fetchall()}
        if "sent_at" not in od_cols_current:
            try:
                conn.execute("ALTER TABLE outbox_deliveries ADD COLUMN sent_at TEXT DEFAULT ''")
            except Exception:
                pass

        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='event_notifications'")
        en_row = cur.fetchone()
        if en_row and "PRIMARY KEY" in en_row[0].upper() and "REVISION" not in en_row[0].upper():
            conn.execute("ALTER TABLE event_notifications RENAME TO event_notifications_old")
            conn.execute(
                """
                CREATE TABLE event_notifications (
                    event_id TEXT,
                    revision INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'pending',
                    notified_at TEXT,
                    PRIMARY KEY (event_id, revision)
                )
                """
            )
            cur.execute("PRAGMA table_info(event_notifications_old)")
            old_en_cols = {r[1] for r in cur.fetchall()}
            if "revision" in old_en_cols:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO event_notifications (event_id, revision, status, notified_at)
                    SELECT event_id, revision, status, notified_at FROM event_notifications_old
                    """
                )
            else:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO event_notifications (event_id, revision, status, notified_at)
                    SELECT event_id, 1, status, notified_at FROM event_notifications_old
                    """
                )
            conn.execute("DROP TABLE event_notifications_old")

        # Step 3: Startup recovery: recover rows left in 'sending'
        _recover_sending_outbox(conn)

        # Step 4: Migrate legacy rows and collapse duplicates
        collapse_outbox_duplicates(conn)

        # Step 5: Create all indexes
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id);
            CREATE INDEX IF NOT EXISTS idx_events_published ON events(published_at);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
            CREATE INDEX IF NOT EXISTS idx_processing_jobs_status ON processing_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_status ON outbox_deliveries(status);
            CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_outbox_page ON outbox_deliveries(outbox_id, page_number);
            CREATE INDEX IF NOT EXISTS idx_outbox_events_event ON outbox_events(event_id);
            CREATE INDEX IF NOT EXISTS idx_outbox_events_outbox ON outbox_events(outbox_id);
            CREATE INDEX IF NOT EXISTS idx_probe_snapshots_name ON probe_snapshots(probe_name, id DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_key ON outbox(key);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_events_member ON outbox_events(event_id, revision);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_processing_jobs_event_rev ON processing_jobs(event_id, revision);
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
                    INSERT INTO processing_jobs (event_id, revision, status, created_at, updated_at)
                    VALUES (?, 1, 'pending', ?, ?)
                    ON CONFLICT(event_id, revision) DO UPDATE SET status = 'pending', updated_at = excluded.updated_at
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
                        INSERT INTO processing_jobs (event_id, revision, status, created_at, updated_at)
                        VALUES (?, ?, 'pending', ?, ?)
                        ON CONFLICT(event_id, revision) DO UPDATE SET status = 'pending', updated_at = excluded.updated_at
                        """,
                        (event_id, new_rev, now_iso, now_iso),
                    )
                    conn.execute(
                        """
                        INSERT INTO event_notifications (event_id, revision, status, notified_at)
                        VALUES (?, ?, 'pending', ?)
                        ON CONFLICT(event_id, revision) DO UPDATE SET status = 'pending', notified_at = excluded.notified_at
                        """,
                        (event_id, new_rev, now_iso),
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
    kind: str | None = None,
    key: str | None = None,
    pages: list | None = None,
    member_events: list[dict] | None = None,
) -> int:
    target_kind = kind if kind is not None else priority
    now_iso = datetime.now().isoformat()
    if not key:
        if priority == "high":
            if event_id.startswith("probe:"):
                key = event_id
            elif event_id.startswith("high:"):
                key = event_id
            else:
                key = f"high:{event_id}:{revision}"
        elif priority == "digest" or target_kind == "digest":
            if event_id.startswith("digest:"):
                key = event_id
            else:
                key = f"digest:{event_id}"
        elif event_id.startswith("probe:"):
            key = event_id
        else:
            key = f"{priority}:{event_id}:{revision}"

    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO outbox (key, event_id, priority, kind, rendered_text, status, attempts, created_at, revision)
            VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                rendered_text = CASE WHEN outbox.status NOT IN ('sent', 'sending') THEN excluded.rendered_text ELSE outbox.rendered_text END,
                priority = excluded.priority
            """,
            (key, event_id, priority, target_kind, rendered_text, now_iso, revision),
        )
        if cur.lastrowid:
            out_id = cur.lastrowid
        else:
            cur.execute("SELECT id FROM outbox WHERE key = ?", (key,))
            row = cur.fetchone()
            out_id = row[0] if row else 0

        if pages:
            for p_idx, page_item in enumerate(pages, 1):
                if isinstance(page_item, tuple):
                    p_msg = page_item[0]
                    p_mids = []
                    for it in page_item[1]:
                        item_ev = it[0]
                        p_mids.extend(item_ev.get("member_ids") or [item_ev["id"]])
                else:
                    p_msg = str(page_item)
                    p_mids = []
                mids_json = json.dumps(p_mids)
                conn.execute(
                    """
                    INSERT INTO outbox_pages (outbox_id, page_number, rendered_text, status, member_ids_json)
                    VALUES (?, ?, ?, 'pending', ?)
                    ON CONFLICT(outbox_id, page_number) DO UPDATE SET
                        rendered_text = CASE WHEN outbox_pages.status NOT IN ('sent', 'sending') THEN excluded.rendered_text ELSE outbox_pages.rendered_text END,
                        member_ids_json = excluded.member_ids_json
                    """,
                    (out_id, p_idx, p_msg, mids_json),
                )

        if member_events:
            for ev in member_events:
                if "member_items" in ev and ev["member_items"]:
                    for mid, mrev in ev["member_items"]:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO outbox_events (outbox_id, event_id, revision)
                            VALUES (?, ?, ?)
                            """,
                            (out_id, mid, mrev),
                        )
                elif "member_revisions" in ev and ev["member_revisions"]:
                    for mid, mrev in ev["member_revisions"].items():
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO outbox_events (outbox_id, event_id, revision)
                            VALUES (?, ?, ?)
                            """,
                            (out_id, mid, mrev),
                        )
                else:
                    mids = ev.get("member_ids") or [ev["id"]]
                    mrev = ev.get("revision", 1)
                    for mid in mids:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO outbox_events (outbox_id, event_id, revision)
                            VALUES (?, ?, ?)
                            """,
                            (out_id, mid, mrev),
                        )

        return out_id


def add_digest_to_outbox(
    conn: sqlite3.Connection,
    key: str,
    rendered_text: str,
    pages: list[tuple[str, list[tuple[dict, dict]]]],
    member_events: list[dict],
) -> int:
    return add_to_outbox(
        conn,
        event_id=key,
        priority="digest",
        rendered_text=rendered_text,
        revision=1,
        kind="digest",
        key=key,
        pages=pages,
        member_events=member_events,
    )


def get_outbox_by_key(conn: sqlite3.Connection, key: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM outbox WHERE key = ? LIMIT 1", (key,))
    row = cur.fetchone()
    return dict(row) if row else None


def is_event_already_sent(
    conn: sqlite3.Connection,
    event_id: str,
    revision: int | None = None,
    content_hash: str | None = None,
) -> bool:
    """Check if an exact (event_id, revision) was already successfully sent."""
    cur = conn.cursor()
    # 1. Outbox entry with status 'sent'
    if revision is not None:
        key = f"high:{event_id}:{revision}"
        cur.execute(
            """
            SELECT 1 FROM outbox
            WHERE (key = ? OR (event_id = ? AND revision = ?)) AND status = 'sent'
            LIMIT 1
            """,
            (key, event_id, revision),
        )
    else:
        cur.execute(
            "SELECT 1 FROM outbox WHERE event_id = ? AND status = 'sent' LIMIT 1",
            (event_id,),
        )
    if cur.fetchone():
        return True

    # 2. Linked member event in outbox_events where outbox is 'sent'
    if revision is not None:
        cur.execute(
            """
            SELECT 1 FROM outbox_events oe
            JOIN outbox o ON o.id = oe.outbox_id
            WHERE oe.event_id = ? AND oe.revision = ? AND o.status = 'sent'
            LIMIT 1
            """,
            (event_id, revision),
        )
    else:
        cur.execute(
            """
            SELECT 1 FROM outbox_events oe
            JOIN outbox o ON o.id = oe.outbox_id
            WHERE oe.event_id = ? AND o.status = 'sent'
            LIMIT 1
            """,
            (event_id,),
        )
    if cur.fetchone():
        return True

    # 3. Events table
    if content_hash:
        cur.execute(
            "SELECT 1 FROM events WHERE id = ? AND content_hash = ? AND status = 'sent' LIMIT 1",
            (event_id, content_hash),
        )
        if cur.fetchone():
            return True
    elif revision is not None:
        cur.execute(
            "SELECT 1 FROM events WHERE id = ? AND revision = ? AND status = 'sent' LIMIT 1",
            (event_id, revision),
        )
        if cur.fetchone():
            return True
    else:
        cur.execute(
            "SELECT 1 FROM events WHERE id = ? AND status = 'sent' LIMIT 1",
            (event_id,),
        )
        if cur.fetchone():
            return True

    # 4. Event notifications table
    if revision is not None:
        cur.execute(
            "SELECT 1 FROM event_notifications WHERE event_id = ? AND revision = ? AND status = 'sent' LIMIT 1",
            (event_id, revision),
        )
        if cur.fetchone():
            return True
    else:
        cur.execute(
            "SELECT 1 FROM event_notifications WHERE event_id = ? AND status = 'sent' LIMIT 1",
            (event_id,),
        )
        if cur.fetchone():
            return True

    return False


def is_event_revision_reserved(
    conn: sqlite3.Connection, event_id: str, revision: int = 1
) -> bool:
    """Check if an (event_id, revision) pair is already reserved in outbox_events."""
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM outbox_events WHERE event_id = ? AND revision = ? LIMIT 1",
        (event_id, revision),
    )
    return cur.fetchone() is not None


def is_raw_or_unenriched_text(rendered_text: str) -> bool:
    """Check if rendered message text contains raw-mode fallback content."""
    if not rendered_text:
        return False
    indicators = (
        "Batafsil ma'lumot quyidagi rasmiy havolada keltirilgan",
        "Batafsil ma&#x27;lumot quyidagi rasmiy havolada keltirilgan",
        "[Boyitilmagan]",
    )
    return any(ind in rendered_text for ind in indicators)


def downgrade_outbox_to_digest(conn: sqlite3.Connection, outbox_id: int) -> None:
    with conn:
        conn.execute(
            """
            UPDATE outbox
            SET priority = 'digest', kind = 'digest'
            WHERE id = ? AND status != 'sent'
            """,
            (outbox_id,),
        )


def increment_enrichment_attempts(
    conn: sqlite3.Connection, event_id: str, revision: int = 1
) -> int:
    """Persist and increment enrichment failure attempts per (event_id, revision)."""
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO processing_jobs (event_id, revision, status, attempts, created_at, updated_at)
            VALUES (?, ?, 'pending', 1, ?, ?)
            ON CONFLICT(event_id, revision) DO UPDATE SET attempts = processing_jobs.attempts + 1, updated_at = excluded.updated_at
            """,
            (event_id, revision, now_iso, now_iso),
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT attempts FROM processing_jobs WHERE event_id = ? AND revision = ?",
            (event_id, revision),
        )
        row = cur.fetchone()
        return row[0] if row else 1


def increment_processing_job_attempts(
    conn: sqlite3.Connection, event_id: str, revision: int = 1
) -> int:
    return increment_enrichment_attempts(conn, event_id, revision)


def get_enrichment_attempts(
    conn: sqlite3.Connection, event_id: str, revision: int = 1
) -> int:
    """Return enrichment failure attempts for (event_id, revision)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT attempts FROM processing_jobs WHERE event_id = ? AND revision = ?",
        (event_id, revision),
    )
    row = cur.fetchone()
    return row[0] if row else 0


def downgrade_event_to_digest(
    conn: sqlite3.Connection, event_id: str, revision: int = 1
) -> None:
    """Persist downgrade of a high-priority event to digest after enrichment exhaustion."""
    with conn:
        conn.execute(
            """
            UPDATE events
            SET priority = 'digest'
            WHERE id = ? AND revision = ?
            """,
            (event_id, revision),
        )


def link_outbox_events(
    conn: sqlite3.Connection, outbox_id: int, events: list[dict]
) -> None:
    """Link member events and their revisions to an outbox entry."""
    with conn:
        for ev in events:
            if "member_items" in ev and ev["member_items"]:
                for mid, mrev in ev["member_items"]:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO outbox_events (outbox_id, event_id, revision)
                        VALUES (?, ?, ?)
                        """,
                        (outbox_id, mid, mrev),
                    )
            elif "member_revisions" in ev and ev["member_revisions"]:
                for mid, mrev in ev["member_revisions"].items():
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO outbox_events (outbox_id, event_id, revision)
                        VALUES (?, ?, ?)
                        """,
                        (outbox_id, mid, mrev),
                    )
            else:
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
        if "member_items" in ev and ev["member_items"]:
            all_mids.extend(ev["member_items"])
        elif "member_revisions" in ev and ev["member_revisions"]:
            all_mids.extend(ev["member_revisions"].items())
        else:
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
    next_attempt_at: str | None = None,
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(outbox)")
        cols = {r[1] for r in cur.fetchall()}
        has_last = "last_attempt_at" in cols
        has_sent_at = "sent_at" in cols
        has_next_at = "next_attempt_at" in cols

        if status == "sending":
            if has_last:
                conn.execute(
                    "UPDATE outbox SET status = 'sending', last_attempt_at = ? WHERE id = ?",
                    (now_iso, outbox_id),
                )
            else:
                conn.execute(
                    "UPDATE outbox SET status = 'sending' WHERE id = ?",
                    (outbox_id,),
                )
            return

        # Definite failure cap at 3 attempts -> failed_final
        is_definite_failure = (status == "failed")
        if is_definite_failure:
            cur.execute("SELECT attempts FROM outbox WHERE id = ?", (outbox_id,))
            att_row = cur.fetchone()
            cur_att = (att_row[0] or 0) if att_row else 0
            new_att = cur_att + 1
            if new_att >= 3:
                status = "failed_final"
            if next_attempt_at is None:
                next_attempt_at = (datetime.now() + timedelta(minutes=15 * new_att)).isoformat()

        next_at_val = next_attempt_at if next_attempt_at is not None else ""

        if has_last and has_sent_at and has_next_at:
            conn.execute(
                """
                UPDATE outbox
                SET status = ?,
                    attempts = CASE WHEN ? THEN attempts + 1 ELSE attempts END,
                    last_attempt_at = ?,
                    sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                    next_attempt_at = CASE WHEN ? = 'failed' THEN ? ELSE '' END,
                    error_message = ?
                WHERE id = ?
                """,
                (status, is_definite_failure, now_iso, status, now_iso, status, next_at_val, error_message, outbox_id),
            )
        elif has_last:
            conn.execute(
                """
                UPDATE outbox
                SET status = ?,
                    attempts = CASE WHEN ? THEN attempts + 1 ELSE attempts END,
                    last_attempt_at = ?, error_message = ?
                WHERE id = ?
                """,
                (status, is_definite_failure, now_iso, error_message, outbox_id),
            )
        else:
            conn.execute(
                """
                UPDATE outbox
                SET status = ?,
                    attempts = CASE WHEN ? THEN attempts + 1 ELSE attempts END
                WHERE id = ?
                """,
                (status, is_definite_failure, outbox_id),
            )
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM outbox_pages WHERE outbox_id = ?", (outbox_id,))
        has_pages = (cur.fetchone()[0] > 0)
        if not has_pages:
            cur.execute("SELECT DISTINCT event_id, revision FROM outbox_events WHERE outbox_id = ?", (outbox_id,))
            linked = cur.fetchall()
            if not linked:
                cur.execute("SELECT event_id, revision FROM outbox WHERE id = ?", (outbox_id,))
                row = cur.fetchone()
                if row and not row[0].startswith("digest:") and not row[0].startswith("legacy:") and row[0] != "digest":
                    linked = [(row[0], row[1] or 1)]

            for mid, mrev in linked:
                conn.execute(
                    """
                    INSERT INTO event_notifications (event_id, revision, status, notified_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(event_id, revision) DO UPDATE SET status = excluded.status, notified_at = excluded.notified_at
                    """,
                    (mid, mrev or 1, status, now_iso),
                )
                conn.execute(
                    "UPDATE events SET status = ? WHERE id = ? AND revision = ?",
                    (status, mid, mrev or 1),
                )


def get_unsent_outbox_pages(conn: sqlite3.Connection, outbox_id: int) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM outbox_pages
        WHERE outbox_id = ? AND status IN ('pending', 'failed')
        ORDER BY page_number ASC
        """,
        (outbox_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def mark_outbox_page_status(
    conn: sqlite3.Connection, outbox_id: int, page_number: int, status: str
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(outbox_pages)")
        cols = {r[1] for r in cur.fetchall()}
        if "last_attempt_at" in cols:
            conn.execute(
                """
                UPDATE outbox_pages
                SET status = ?, last_attempt_at = ?
                WHERE outbox_id = ? AND page_number = ?
                """,
                (status, now_iso, outbox_id, page_number),
            )
        else:
            conn.execute(
                "UPDATE outbox_pages SET status = ? WHERE outbox_id = ? AND page_number = ?",
                (status, outbox_id, page_number),
            )


def mark_outbox_page_sent(
    conn: sqlite3.Connection, outbox_id: int, page_number: int
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(outbox_pages)")
        cols = {r[1] for r in cur.fetchall()}
        if "last_attempt_at" in cols:
            conn.execute(
                """
                UPDATE outbox_pages
                SET status = 'sent', last_attempt_at = ?, error_message = ''
                WHERE outbox_id = ? AND page_number = ?
                """,
                (now_iso, outbox_id, page_number),
            )
        else:
            conn.execute(
                "UPDATE outbox_pages SET status = 'sent' WHERE outbox_id = ? AND page_number = ?",
                (outbox_id, page_number),
            )
        cur.execute(
            "SELECT member_ids_json FROM outbox_pages WHERE outbox_id = ? AND page_number = ?",
            (outbox_id, page_number),
        )
        row = cur.fetchone()
        if row and row[0]:
            try:
                mids = json.loads(row[0])
                for mid in mids:
                    cur.execute(
                        "SELECT revision FROM outbox_events WHERE outbox_id = ? AND event_id = ? LIMIT 1",
                        (outbox_id, mid),
                    )
                    rev_row = cur.fetchone()
                    mrev = rev_row[0] if rev_row else 1
                    conn.execute(
                        """
                        INSERT INTO event_notifications (event_id, revision, status, notified_at)
                        VALUES (?, ?, 'sent', ?)
                        ON CONFLICT(event_id, revision) DO UPDATE SET status = 'sent', notified_at = excluded.notified_at
                        """,
                        (mid, mrev, now_iso),
                    )
                    conn.execute("UPDATE events SET status = 'sent' WHERE id = ? AND revision = ?", (mid, mrev))
            except Exception:
                pass


def mark_outbox_page_failed(
    conn: sqlite3.Connection, outbox_id: int, page_number: int, error: str = ""
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(outbox_pages)")
        cols = {r[1] for r in cur.fetchall()}
        if "last_attempt_at" in cols:
            conn.execute(
                """
                UPDATE outbox_pages
                SET status = 'failed', attempts = attempts + 1, last_attempt_at = ?, error_message = ?
                WHERE outbox_id = ? AND page_number = ?
                """,
                (now_iso, error, outbox_id, page_number),
            )
        else:
            conn.execute(
                "UPDATE outbox_pages SET status = 'failed' WHERE outbox_id = ? AND page_number = ?",
                (outbox_id, page_number),
            )
        cur = conn.cursor()
        cur.execute(
            "SELECT member_ids_json FROM outbox_pages WHERE outbox_id = ? AND page_number = ?",
            (outbox_id, page_number),
        )
        row = cur.fetchone()
        if row and row[0]:
            try:
                mids = json.loads(row[0])
                for mid in mids:
                    cur.execute(
                        "SELECT revision FROM outbox_events WHERE outbox_id = ? AND event_id = ? LIMIT 1",
                        (outbox_id, mid),
                    )
                    rev_row = cur.fetchone()
                    mrev = rev_row[0] if rev_row else 1
                    conn.execute(
                        """
                        INSERT INTO event_notifications (event_id, revision, status, notified_at)
                        VALUES (?, ?, 'failed', ?)
                        ON CONFLICT(event_id, revision) DO UPDATE SET status = 'failed', notified_at = excluded.notified_at
                        """,
                        (mid, mrev, now_iso),
                    )
                    conn.execute("UPDATE events SET status = 'failed' WHERE id = ? AND revision = ?", (mid, mrev))
            except Exception:
                pass


def are_all_outbox_pages_sent(conn: sqlite3.Connection, outbox_id: int) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM outbox_pages WHERE outbox_id = ? AND status != 'sent'",
        (outbox_id,),
    )
    row = cur.fetchone()
    return (row[0] == 0) if row else True


def mark_sending_outbox_ambiguous(conn: sqlite3.Connection) -> int:
    with conn:
        return _recover_sending_outbox(conn)


def count_ambiguous_outbox(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM outbox WHERE status = 'ambiguous'")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def count_today_high_priority(conn: sqlite3.Connection, today_date: str | None = None) -> int:
    today_start = today_date or datetime.now().date().isoformat()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(outbox)")
    cols = {r[1] for r in cur.fetchall()}
    has_sent_at = "sent_at" in cols
    if has_sent_at:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt FROM outbox
            WHERE priority = 'high' AND status = 'sent'
              AND (
                (sent_at != '' AND sent_at LIKE ?)
                OR (sent_at = '' AND last_attempt_at != '' AND last_attempt_at LIKE ?)
                OR (sent_at = '' AND last_attempt_at = '' AND (created_at LIKE ? OR key LIKE ?))
              )
            """,
            (f"{today_start}%", f"{today_start}%", f"{today_start}%", f"%:{today_start}%"),
        )
    else:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt FROM outbox
            WHERE priority = 'high' AND status = 'sent' AND (created_at >= ? OR key LIKE ?)
            """,
            (today_start, f"%:{today_start}%"),
        )
    row = cur.fetchone()
    return int(row["cnt"]) if row else 0


def was_digest_page_sent_today(conn: sqlite3.Connection, today_date: str | None = None) -> bool:
    today_start = today_date or datetime.now().date().isoformat()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_deliveries'")
    if not cur.fetchone():
        return False
    cur.execute("PRAGMA table_info(outbox_deliveries)")
    od_cols = {r[1] for r in cur.fetchall()}
    has_sent_at = "sent_at" in od_cols

    if has_sent_at:
        cur.execute(
            """
            SELECT 1 FROM outbox_deliveries od
            JOIN outbox o ON od.outbox_id = o.id
            WHERE o.priority = 'digest' AND od.status = 'sent'
              AND (
                (od.sent_at != '' AND od.sent_at LIKE ?)
                OR (od.last_attempt_at != '' AND od.last_attempt_at LIKE ?)
              )
            LIMIT 1
            """,
            (f"{today_start}%", f"{today_start}%"),
        )
    else:
        cur.execute(
            """
            SELECT 1 FROM outbox_deliveries od
            JOIN outbox o ON od.outbox_id = o.id
            WHERE o.priority = 'digest' AND od.status = 'sent'
              AND od.last_attempt_at LIKE ?
            LIMIT 1
            """,
            (f"{today_start}%",),
        )
    return bool(cur.fetchone())


def get_pending_processing_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Retrieve unfinished processing jobs joined with event details."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pj.id AS job_id, pj.event_id AS event_id, pj.revision AS job_revision,
               pj.status AS job_status, pj.attempts AS job_attempts,
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
        if d.get("job_revision") is not None:
            d["revision"] = d["job_revision"]
        results.append(d)
    return results


def mark_processing_job_done(
    conn: sqlite3.Connection, event_id: str, revision: int | None = None
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        if revision is not None:
            conn.execute(
                """
                UPDATE processing_jobs
                SET status = 'completed', updated_at = ?
                WHERE event_id = ? AND revision <= ?
                """,
                (now_iso, event_id, revision),
            )
        else:
            conn.execute(
                """
                UPDATE processing_jobs
                SET status = 'completed', updated_at = ?
                WHERE event_id = ?
                """,
                (now_iso, event_id),
            )


def mark_processing_job_failed(
    conn: sqlite3.Connection, event_id: str, error_message: str = "", revision: int | None = None
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        if revision is not None:
            conn.execute(
                """
                UPDATE processing_jobs
                SET status = 'failed', attempts = attempts + 1, updated_at = ?
                WHERE event_id = ? AND revision = ?
                """,
                (now_iso, event_id, revision),
            )
        else:
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
    conn: sqlite3.Connection, outbox_id: int, chat_ids: list[str], page_number: int = 1
) -> None:
    with conn:
        for cid in chat_ids:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM outbox_deliveries WHERE outbox_id = ? AND page_number = ? AND chat_id = ? LIMIT 1",
                (outbox_id, page_number, str(cid)),
            )
            if not cur.fetchone():
                conn.execute(
                    """
                    INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status)
                    VALUES (?, ?, ?, 'pending')
                    """,
                    (outbox_id, page_number, str(cid)),
                )


def get_pending_delivery_chat_ids(
    conn: sqlite3.Connection, outbox_id: int, page_number: int = 1
) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT chat_id FROM outbox_deliveries
        WHERE outbox_id = ? AND page_number = ? AND status IN ('pending', 'failed')
        ORDER BY chat_id ASC
        """,
        (outbox_id, page_number),
    )
    candidates = [str(r["chat_id"]) for r in cur.fetchall()]
    pending = []
    for cid in candidates:
        cur.execute(
            "SELECT 1 FROM outbox_deliveries WHERE outbox_id = ? AND page_number = ? AND chat_id = ? AND status IN ('sent', 'ambiguous') LIMIT 1",
            (outbox_id, page_number, cid),
        )
        if not cur.fetchone():
            pending.append(cid)
    return pending


def mark_delivery_sending(
    conn: sqlite3.Connection,
    outbox_id: int,
    chat_id: str,
    page_number: int = 1,
) -> None:
    now_iso = datetime.now().isoformat()
    with conn:
        conn.execute(
            """
            UPDATE outbox_deliveries
            SET status = 'sending', last_attempt_at = ?
            WHERE outbox_id = ? AND page_number = ? AND chat_id = ?
            """,
            (now_iso, outbox_id, page_number, str(chat_id)),
        )


def record_delivery_attempt(
    conn: sqlite3.Connection,
    outbox_id: int,
    chat_id: str,
    success: bool,
    error: str = "",
    page_number: int = 1,
    status: str | None = None,
) -> None:
    now_iso = datetime.now().isoformat()
    if status is None:
        status = "sent" if success else "failed"
    is_failed = (status == "failed")
    with conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(outbox_deliveries)")
        od_cols = {r[1] for r in cur.fetchall()}
        has_sent_at = "sent_at" in od_cols

        if has_sent_at:
            conn.execute(
                """
                UPDATE outbox_deliveries
                SET status = ?,
                    attempts = CASE WHEN ? THEN attempts + 1 ELSE attempts END,
                    last_attempt_at = ?,
                    sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                    error_message = ?
                WHERE outbox_id = ? AND page_number = ? AND chat_id = ?
                """,
                (status, is_failed, now_iso, status, now_iso, error, outbox_id, page_number, str(chat_id)),
            )
        else:
            conn.execute(
                """
                UPDATE outbox_deliveries
                SET status = ?,
                    attempts = CASE WHEN ? THEN attempts + 1 ELSE attempts END,
                    last_attempt_at = ?, error_message = ?
                WHERE outbox_id = ? AND page_number = ? AND chat_id = ?
                """,
                (status, is_failed, now_iso, error, outbox_id, page_number, str(chat_id)),
            )

        cur.execute("SELECT COUNT(*) FROM outbox_pages WHERE outbox_id = ?", (outbox_id,))
        has_pages = (cur.fetchone()[0] > 0)
        if not has_pages:
            cur.execute("SELECT status FROM outbox_deliveries WHERE outbox_id = ?", (outbox_id,))
            d_statuses = [r[0] for r in cur.fetchall()]
            if any(s in ("pending", "sending") for s in d_statuses):
                # Deliveries still in-flight to other recipients; preserve outbox status as 'sending'
                return

            out_status = derive_status(d_statuses)
            all_sent = (out_status == "sent")

            cur.execute("PRAGMA table_info(outbox)")
            o_cols = {r[1] for r in cur.fetchall()}
            has_o_sent_at = "sent_at" in o_cols

            if out_status == "failed":
                cur.execute("SELECT attempts FROM outbox WHERE id = ?", (outbox_id,))
                att_row = cur.fetchone()
                cur_att = att_row[0] if att_row else 0
                if cur_att + 1 >= 3:
                    out_status = "failed_final"

            if has_o_sent_at:
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = ?, last_attempt_at = ?,
                        sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                        error_message = CASE WHEN ? THEN '' ELSE ? END
                    WHERE id = ?
                    """,
                    (out_status, now_iso, out_status, now_iso, all_sent, error, outbox_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = ?, last_attempt_at = ?,
                        error_message = CASE WHEN ? THEN '' ELSE ? END
                    WHERE id = ?
                    """,
                    (out_status, now_iso, all_sent, error, outbox_id),
                )

            if all_sent:
                cur.execute("SELECT DISTINCT event_id, revision FROM outbox_events WHERE outbox_id = ?", (outbox_id,))
                linked = cur.fetchall()
                if not linked:
                    cur.execute("SELECT event_id, revision FROM outbox WHERE id = ?", (outbox_id,))
                    o_row = cur.fetchone()
                    if o_row and not o_row[0].startswith("digest:") and not o_row[0].startswith("legacy:") and o_row[0] != "digest":
                        linked = [(o_row[0], o_row[1] or 1)]
                for mid, mrev in linked:
                    conn.execute(
                        """
                        INSERT INTO event_notifications (event_id, revision, status, notified_at)
                        VALUES (?, ?, 'sent', ?)
                        ON CONFLICT(event_id, revision) DO UPDATE SET status = 'sent', notified_at = excluded.notified_at
                        """,
                        (mid, mrev, now_iso),
                    )
                    conn.execute("UPDATE events SET status = 'sent' WHERE id = ? AND revision = ?", (mid, mrev))


def mark_event_notified(
    conn: sqlite3.Connection,
    event_id: str,
    status: str = "sent",
    revision: int | None = None,
) -> None:
    now_iso = datetime.now().isoformat()
    rev = revision if revision is not None else 1
    with conn:
        conn.execute(
            """
            INSERT INTO event_notifications (event_id, revision, status, notified_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, revision) DO UPDATE SET status = excluded.status, notified_at = excluded.notified_at
            """,
            (event_id, rev, status, now_iso),
        )
        if revision is not None:
            conn.execute(
                "UPDATE events SET status = ? WHERE id = ? AND revision = ?",
                (status, event_id, revision),
            )
        else:
            conn.execute(
                "UPDATE events SET status = ? WHERE id = ?",
                (status, event_id),
            )


def get_outbox_for_event(
    conn: sqlite3.Connection, event_id: str, revision: int | None = None
) -> dict | None:
    cur = conn.cursor()
    if revision is not None:
        key = f"high:{event_id}:{revision}"
        cur.execute(
            "SELECT * FROM outbox WHERE (key = ? OR (event_id = ? AND revision = ?)) AND status = 'sent' ORDER BY id ASC LIMIT 1",
            (key, event_id, revision),
        )
        row = cur.fetchone()
        if row:
            return dict(row)

        cur.execute(
            "SELECT * FROM outbox WHERE (key = ? OR (event_id = ? AND revision = ?)) ORDER BY id DESC LIMIT 1",
            (key, event_id, revision),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    else:
        cur.execute(
            "SELECT * FROM outbox WHERE event_id = ? AND status = 'sent' ORDER BY id ASC LIMIT 1",
            (event_id,),
        )
        row = cur.fetchone()
        if row:
            return dict(row)

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
                   (SELECT en.status FROM event_notifications en WHERE en.event_id = e.id AND (en.revision = e.revision OR e.revision IS NULL) ORDER BY en.notified_at DESC LIMIT 1),
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
                    INSERT INTO processing_jobs (event_id, revision, status, created_at, updated_at)
                    VALUES (?, 1, 'pending', ?, ?)
                    ON CONFLICT(event_id, revision) DO UPDATE SET status = 'pending', updated_at = excluded.updated_at
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

