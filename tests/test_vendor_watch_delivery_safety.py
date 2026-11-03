"""Tests for delivery safety defects F1-F6 verified by Codex (_MASLAHAT-codex.md).

Covers:
- F1: Per-recipient sending state before HTTP call, crash recovery marks sending -> ambiguous, no duplicate send to B.
- F2: Inter-process file locking prevents overlapping cycles.
- F3: Multi-page digest page-level ambiguity resumes unattempted pages in next cycle.
- F4: One digest per day limit based on actual sent day (yesterday retry suppresses today's new digest).
- F5: Single daily 5-bell quota based on sent_at across new and retried items.
- F6: Ambiguous Telegram timeout is not retried; definite HTTP 500 retried up to 3 times -> failed_final; next_attempt_at delay.
- Migration: Schema upgrade on a copy of the real data/vendor_watch.db.
"""

from datetime import datetime, timedelta
import os
from pathlib import Path
import shutil
import sqlite3
import urllib.error
import pytest

from src.vendor_watch import delivery, enricher, parser, runner, store, triage


@pytest.fixture(autouse=True)
def _block_real_vertex_and_telegram(monkeypatch):
    from src import vertex

    def _blocked_vertex(*args, **kwargs):
        raise RuntimeError("Real Vertex call blocked in tests")

    def _blocked_telegram(*args, **kwargs):
        raise RuntimeError("Real Telegram call blocked in tests")

    monkeypatch.setattr(vertex, "chat_json", _blocked_vertex)
    monkeypatch.setattr(delivery, "send_to_telegram", _blocked_telegram)


def test_f1_per_recipient_sending_crash_recovery_no_duplicate_b(tmp_path: Path, monkeypatch):
    """F1: Crash while sending to B leaves B in 'sending'; startup recovers B to 'ambiguous'.

    Retry cycle must never resend to A or B (Codex scenario 'A, B, B' is eliminated).
    """
    db_path = tmp_path / "f1.db"
    conn = store.get_connection(db_path)
    out_id = store.add_to_outbox(conn, "ev-f1", "high", "High priority alert")

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-a", "chat-b"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    calls = []

    def mock_send(token, chat_id, text, timeout=15):
        calls.append(chat_id)
        if chat_id == "chat-b":
            # Simulate sudden crash/process death during B's send
            raise KeyboardInterrupt("Simulated process crash during chat-b delivery")

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send)

    # 1. First run: sends to chat-a, crashes while sending chat-b
    try:
        delivery.deliver("High priority alert", dry_run=False, conn=conn, outbox_id=out_id)
    except KeyboardInterrupt:
        pass

    assert calls == ["chat-a", "chat-b"]

    # In DB: chat-a is 'sent', chat-b was committed as 'sending' before HTTP call
    cur = conn.cursor()
    cur.execute("SELECT chat_id, status FROM outbox_deliveries WHERE outbox_id = ? ORDER BY chat_id", (out_id,))
    statuses = dict(cur.fetchall())
    assert statuses["chat-a"] == "sent"
    assert statuses["chat-b"] == "sending"

    # 2. Simulate process restart: startup recovery marks 'sending' as 'ambiguous'
    store.mark_sending_outbox_ambiguous(conn)

    cur.execute("SELECT chat_id, status FROM outbox_deliveries WHERE outbox_id = ? ORDER BY chat_id", (out_id,))
    recovered_statuses = dict(cur.fetchall())
    assert recovered_statuses["chat-a"] == "sent"
    assert recovered_statuses["chat-b"] == "ambiguous"

    # Outbox status itself should be 'ambiguous'
    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur.fetchone()[0] == "ambiguous"

    # Prevent daily digest in this run
    today_str = datetime.now().date().isoformat()
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    # 3. Next cycle: retry attempt must NOT re-send to chat-a OR chat-b
    calls.clear()
    monkeypatch.setattr(delivery, "send_to_telegram", lambda *args, **kwargs: calls.append(args[1]))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # Neither A nor B is sent again! Total deliveries across all runs: exactly 1 (to chat-a)
    assert calls == []


def test_f2_interprocess_lock_parallel_cycles_single_execution(tmp_path: Path, monkeypatch):
    """F2: Lock file prevents overlapping execution of run_cycle on the same database."""
    db_path = tmp_path / "f2.db"
    conn = store.get_connection(db_path)
    store.add_to_outbox(conn, "ev-f2", "high", "Alert for lock test")
    today_str = datetime.now().date().isoformat()
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    lock_file = db_path.resolve().with_suffix(db_path.suffix + ".lock")
    external_lock = runner.InterProcessLock(lock_file)
    assert external_lock.acquire() is True

    try:
        # While lock is held externally, run_cycle must gracefully exit without delivering
        runner.run_cycle(db_path=db_path, dry_run=False, once=True)
        assert len(delivered) == 0
    finally:
        external_lock.release()

    # Now that lock is released, run_cycle acquires lock and executes
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 1


def test_f3_multipage_digest_page_level_ambiguity_resumes_unattempted_page(tmp_path: Path, monkeypatch):
    """F3: When page 1 is sent and a crash occurs before page 2, the next cycle sends only page 2."""
    db_path = tmp_path / "f3.db"
    conn = store.get_connection(db_path)

    pages = [("Dayjest 1-sahifa matni", []), ("Dayjest 2-sahifa matni", [])]
    out_id = store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-29",
        rendered_text="Dayjest 1-sahifa matni",
        pages=pages,
        member_events=[],
    )

    # Page 1 was sent, then process crashed before page 2 could be attempted
    store.mark_outbox_page_sent(conn, out_id, page_number=1)
    cur = conn.cursor()
    cur.execute("UPDATE outbox_deliveries SET status = 'sent' WHERE outbox_id = ? AND page_number = 1", (out_id,))
    # Page 2 remains 'pending'
    cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ? ORDER BY page_number", (out_id,))
    p_statuses = [r[0] for r in cur.fetchall()]
    assert p_statuses == ["sent", "pending"]

    # Outbox status remains pending because page 2 is not yet sent
    store.update_outbox_status(conn, out_id, "pending")
    conn.close()

    # In next cycle, only page 2 must be delivered
    delivered_pages = []

    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        delivered_pages.append((outbox_id, page_number, text))
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")

    # Exactly 1 page delivered in this cycle, and it is page 2!
    assert len(delivered_pages) == 1
    assert delivered_pages[0][1] == 2
    assert "Dayjest 2-sahifa matni" in delivered_pages[0][2]

    # Now both pages are sent, outbox is sent
    conn2 = store.get_connection(db_path)
    assert store.are_all_outbox_pages_sent(conn2, out_id) is True
    cur2 = conn2.cursor()
    cur2.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur2.fetchone()[0] == "sent"
    conn2.close()


def test_f4_one_digest_per_day_yesterday_retry_suppresses_today_digest(tmp_path: Path, monkeypatch):
    """F4: If yesterday's digest retry is delivered today, today's new daily digest is suppressed."""
    db_path = tmp_path / "f4.db"
    conn = store.get_connection(db_path)

    # Yesterday's multi-page digest (2026-10-28) in pending outbox
    pages = [("Kechagi dayjest sahifasi", [])]
    store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-28",
        rendered_text="Kechagi dayjest sahifasi",
        pages=pages,
        member_events=[],
    )
    store.set_app_state(conn, "last_digest_date", "2026-10-27")

    # New item arrives today (2026-10-29) eligible for digest
    source = {"id": "src-f4", "vendor": "Google", "product": "Gemini"}
    today_entry = parser.FeedEntry(
        entry_id="gemini-update-today",
        title="Gemini 2.5 update",
        link="https://google.com/gemini-2-5",
        published_at="2026-10-29T10:00:00Z",
        summary="Gemini improvements",
        raw_content="Release notes today.",
    )
    store.record_unseen_entries(conn, source, [today_entry])
    conn.close()

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text))

    delivered = []
    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        delivered.append(text)
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)

    # Run cycle on 2026-10-29
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date="2026-10-29")

    # Only yesterday's retried digest is delivered; today's new digest is NOT sent
    assert len(delivered) == 1
    assert "Kechagi dayjest sahifasi" in delivered[0]


def test_f5_single_bell_quota_caps_retries_and_new_items(tmp_path: Path, monkeypatch):
    """F5: 5 sent items today blocks a 6th pending retry item and new items from high-priority delivery."""
    db_path = tmp_path / "f5_quota.db"
    conn = store.get_connection(db_path)
    today_str = "2026-10-29"

    # Insert 5 high-priority messages that were sent today
    for i in range(5):
        oid = store.add_to_outbox(conn, f"ev-sent-{i}", "high", f"Sent message {i}")
        store.init_outbox_deliveries(conn, oid, ["chat1"])
        store.record_delivery_attempt(conn, oid, "chat1", success=True)
        store.update_outbox_status(conn, oid, "sent")
        conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (f"{today_str}T08:00:00", oid))
        conn.execute("UPDATE outbox_deliveries SET sent_at = ? WHERE outbox_id = ?", (f"{today_str}T08:00:00", oid))

    assert store.count_today_high_priority(conn, today_date=today_str) == 5

    # 1 pending retry item in outbox
    p_oid = store.add_to_outbox(conn, "ev-pending-retry", "high", "Pending retry message")
    store.update_outbox_status(conn, p_oid, "pending")
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    # Quota is exhausted (5 sent); pending retry was skipped
    assert len(delivered) == 0


def test_f5_yesterday_created_high_item_sent_today_counts_towards_today_quota(tmp_path: Path):
    """F5: Item created yesterday but sent today counts towards today's quota (sent_at based)."""
    db_path = tmp_path / "f5_created.db"
    conn = store.get_connection(db_path)
    today_str = "2026-10-29"
    yesterday_str = "2026-10-28"

    oid = store.add_to_outbox(conn, "ev-yesterday", "high", "Yesterday alert")
    # Simulate created yesterday, sent today
    conn.execute("UPDATE outbox SET created_at = ?, sent_at = ?, status = 'sent' WHERE id = ?", (f"{yesterday_str}T22:00:00", f"{today_str}T09:00:00", oid))
    store.init_outbox_deliveries(conn, oid, ["chat1"])
    conn.execute("UPDATE outbox_deliveries SET status = 'sent', sent_at = ? WHERE outbox_id = ?", (f"{today_str}T09:00:00", oid))

    # Must count towards today (1), not yesterday (0)
    assert store.count_today_high_priority(conn, today_date=today_str) == 1
    assert store.count_today_high_priority(conn, today_date=yesterday_str) == 0
    conn.close()


def test_f6_ambiguous_telegram_timeout_not_retried(tmp_path: Path, monkeypatch):
    """F6: Network timeout during delivery marks status 'ambiguous' and is never retried."""
    db_path = tmp_path / "f6_timeout.db"
    conn = store.get_connection(db_path)
    out_id = store.add_to_outbox(conn, "ev-f6-timeout", "high", "Alert for timeout test")

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat1"])

    def mock_send_timeout(token, chat_id, text, timeout=15):
        raise TimeoutError("Telegram sendMessage timed out after 15s")

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_timeout)

    # 1. Deliver fails with timeout
    success, err = delivery.deliver("Alert for timeout test", dry_run=False, conn=conn, outbox_id=out_id)
    assert success is False
    assert "ambiguous" in err

    # Outbox status is ambiguous
    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur.fetchone()[0] == "ambiguous"

    # Set last_digest_date so daily digest is not triggered in the second cycle
    today_str = datetime.now().date().isoformat()
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    # 2. Next cycle: ambiguous items must NOT be selected or retried
    calls = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # 0 calls made; no duplicate retry
    assert calls == []


def test_f6_definite_http_rejection_retries_capped_at_three_and_failed_final(tmp_path: Path, monkeypatch):
    """F6: Definite HTTP 500 rejection retries up to 3 attempts, then transitions to failed_final."""
    db_path = tmp_path / "f6_http.db"
    conn = store.get_connection(db_path)
    out_id = store.add_to_outbox(conn, "ev-f6-http", "high", "Alert for HTTP error")

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat1"])

    def mock_send_http500(token, chat_id, text, timeout=15):
        raise urllib.error.HTTPError("https://api.telegram.org", 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_http500)
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Attempt 1
    delivery.deliver("Alert for HTTP error", dry_run=False, conn=conn, outbox_id=out_id)
    store.update_outbox_status(conn, out_id, "failed", "HTTP 500")
    cur = conn.cursor()
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st, att = cur.fetchone()
    assert st == "failed"
    assert att == 1

    # Attempt 2
    delivery.deliver("Alert for HTTP error", dry_run=False, conn=conn, outbox_id=out_id)
    store.update_outbox_status(conn, out_id, "failed", "HTTP 500")
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st, att = cur.fetchone()
    assert st == "failed"
    assert att == 2

    # Attempt 3: Cap reached -> transitions to failed_final
    delivery.deliver("Alert for HTTP error", dry_run=False, conn=conn, outbox_id=out_id)
    store.update_outbox_status(conn, out_id, "failed", "HTTP 500")
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st, att = cur.fetchone()
    assert st == "failed_final"
    assert att == 3

    # Set last_digest_date to prevent daily digest triggering in runner
    today_str = datetime.now().date().isoformat()
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    # Attempt 4 in runner: failed_final items are excluded from pending outbox
    calls = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda *args, **kwargs: calls.append(args))
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert calls == []


def test_f6_next_attempt_at_delays_retry(tmp_path: Path, monkeypatch):
    """F6: An outbox row with next_attempt_at in the future is delayed and not retried prematurely."""
    db_path = tmp_path / "f6_delay.db"
    conn = store.get_connection(db_path)
    out_id = store.add_to_outbox(conn, "ev-f6-delay", "high", "Alert for delay test")

    future_time = (datetime.now() + timedelta(hours=2)).isoformat()
    store.update_outbox_status(conn, out_id, "failed", next_attempt_at=future_time)

    cur = conn.cursor()
    cur.execute("SELECT next_attempt_at FROM outbox WHERE id = ?", (out_id,))
    assert cur.fetchone()[0] == future_time

    today_str = datetime.now().date().isoformat()
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Next cycle: future next_attempt_at -> skipped
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 0

    # Once next_attempt_at is in the past -> processed and delivered
    past_time = (datetime.now() - timedelta(minutes=5)).isoformat()
    with store.get_connection(db_path) as c:
        c.execute("UPDATE outbox SET next_attempt_at = ? WHERE id = ?", (past_time, out_id))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 1


def test_migration_schema_upgrade_on_copy_of_real_db(tmp_path: Path):
    """Schema upgrade migration non-destructively adds sent_at and next_attempt_at on real DB copy."""
    real_db = Path("data/vendor_watch.db")
    if not real_db.is_file():
        pytest.skip("data/vendor_watch.db mavjud emas, skip")

    copy_path = tmp_path / "vendor_watch_copy.db"
    shutil.copy2(real_db, copy_path)

    # Connect to copy, which executes _init_schema and any ALTER TABLE migrations
    conn = store.get_connection(copy_path)
    cur = conn.cursor()

    # Check outbox columns
    cur.execute("PRAGMA table_info(outbox)")
    o_cols = {r[1] for r in cur.fetchall()}
    assert "sent_at" in o_cols, "outbox table must have sent_at column"
    assert "next_attempt_at" in o_cols, "outbox table must have next_attempt_at column"

    # Check outbox_deliveries columns
    cur.execute("PRAGMA table_info(outbox_deliveries)")
    od_cols = {r[1] for r in cur.fetchall()}
    assert "sent_at" in od_cols, "outbox_deliveries table must have sent_at column"

    # Ensure existing records are readable
    cur.execute("SELECT COUNT(*) FROM outbox")
    outbox_count = cur.fetchone()[0]
    assert outbox_count >= 0

    cur.execute("SELECT COUNT(*) FROM events")
    events_count = cur.fetchone()[0]
    assert events_count >= 0

    conn.close()


def test_f6_unconfirmed_network_disconnects_classified_as_ambiguous(tmp_path: Path, monkeypatch):
    """F6: Unconfirmed network disconnects (RemoteDisconnected, BrokenPipeError, etc.) are ambiguous, not retried."""
    import http.client
    import ssl

    errors = [
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        BrokenPipeError("Broken pipe"),
        ssl.SSLEOFError("EOF occurred in violation of protocol"),
        http.client.IncompleteRead(b"partial"),
        urllib.error.URLError("Connection refused"),
    ]

    for err in errors:
        assert delivery.classify_telegram_error(err) == "ambiguous", f"{err} must be ambiguous"

    # Confirmed HTTP errors must be 'failed'
    assert delivery.classify_telegram_error(urllib.error.HTTPError("https://api.telegram.org", 500, "Server Error", {}, None)) == "failed"
    assert delivery.classify_telegram_error(urllib.error.HTTPError("https://api.telegram.org", 400, "Bad Request", {}, None)) == "failed"

    # In delivery: a RemoteDisconnected error sets outbox status to ambiguous and is never retried
    db_path = tmp_path / "f6_disconnect.db"
    conn = store.get_connection(db_path)
    out_id = store.add_to_outbox(conn, "ev-f6-disc", "high", "Alert for disconnect test")

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat1"])

    def mock_send_disc(token, chat_id, text, timeout=15):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_disc)

    success, err_msg = delivery.deliver("Alert for disconnect test", dry_run=False, conn=conn, outbox_id=out_id)
    assert success is False
    assert "ambiguous" in err_msg

    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur.fetchone()[0] == "ambiguous"

    today_str = datetime.now().date().isoformat()
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    calls = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert calls == []


def test_f6_multipage_digest_page_failure_marks_outbox_failed_and_caps_at_three_attempts(tmp_path: Path, monkeypatch):
    """F6: When page 1 of a multi-page digest fails with HTTP 500, outbox is 'failed' and caps at 3 attempts."""
    db_path = tmp_path / "f6_multipage_fail.db"
    conn = store.get_connection(db_path)

    pages = [("Dayjest 1-sahifa", []), ("Dayjest 2-sahifa", [])]
    out_id = store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-29",
        rendered_text="Dayjest 1-sahifa",
        pages=pages,
        member_events=[],
    )
    today_str = "2026-10-29"
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    def mock_deliver_fail(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        if page_number == 1:
            if conn is not None and outbox_id is not None:
                store.mark_outbox_page_failed(conn, outbox_id, 1, "HTTP 500 Internal Server Error")
            return False, "HTTP 500 Internal Server Error"
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver_fail)

    # Attempt 1: runner run_cycle processes pending outbox
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date=today_str)
    conn1 = store.get_connection(db_path)
    cur1 = conn1.cursor()
    cur1.execute("SELECT status, attempts, next_attempt_at FROM outbox WHERE id = ?", (out_id,))
    st1, att1, next1 = cur1.fetchone()
    assert st1 == "failed", f"Outbox status must be 'failed', got {st1}"
    assert att1 == 1
    assert next1 != "", "next_attempt_at must be populated on failure"
    conn1.close()

    # Attempt 2: After retry delay elapses
    past_time = (datetime.now() - timedelta(minutes=1)).isoformat()
    with store.get_connection(db_path) as c:
        c.execute("UPDATE outbox SET next_attempt_at = ? WHERE id = ?", (past_time, out_id))
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date=today_str)
    conn2 = store.get_connection(db_path)
    cur2 = conn2.cursor()
    cur2.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st2, att2 = cur2.fetchone()
    assert st2 == "failed"
    assert att2 == 2
    conn2.close()

    # Attempt 3: Cap reached -> transitions to failed_final
    with store.get_connection(db_path) as c:
        c.execute("UPDATE outbox SET next_attempt_at = ? WHERE id = ?", (past_time, out_id))
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date=today_str)
    conn3 = store.get_connection(db_path)
    cur3 = conn3.cursor()
    cur3.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st3, att3 = cur3.fetchone()
    assert st3 == "failed_final", f"Outbox status must transition to 'failed_final', got {st3}"
    assert att3 == 3
    conn3.close()

    # Attempt 4: failed_final is never selected
    calls = []
    monkeypatch.setattr(delivery, "deliver", lambda *args, **kwargs: (calls.append(args) or False, ""))
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date=today_str)
    assert calls == []


def test_f4_single_page_yesterday_digest_retry_suppresses_today_digest(tmp_path: Path, monkeypatch):
    """F4: Single-page yesterday digest retry delivered today sets last_digest_date=today and suppresses today's new digest."""
    db_path = tmp_path / "f4_single.db"
    conn = store.get_connection(db_path)

    # Insert single-page yesterday digest in outbox (no outbox_pages)
    out_id = store.add_to_outbox(conn, "digest:2026-10-28", "digest", "Kechagi bir sahifali dayjest matni")
    conn.execute("UPDATE outbox SET key = 'digest:2026-10-28', kind = 'digest' WHERE id = ?", (out_id,))
    store.set_app_state(conn, "last_digest_date", "2026-10-27")

    # Ingest today's entry (2026-10-29)
    source = {"id": "src-f4-s", "vendor": "Anthropic", "product": "Claude"}
    today_entry = parser.FeedEntry(
        entry_id="claude-update-today",
        title="Claude 3.7 Sonnet release",
        link="https://anthropic.com/claude-3-7",
        published_at="2026-10-29T11:00:00Z",
        summary="Claude improvements",
        raw_content="New reasoning model.",
    )
    store.record_unseen_entries(conn, source, [today_entry])
    conn.close()

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text))

    delivered = []
    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        delivered.append(text)
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)

    # Run cycle on 2026-10-29
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date="2026-10-29")

    # Only yesterday's single-page digest retry is delivered; today's new digest is NOT sent
    assert len(delivered) == 1
    assert "Kechagi bir sahifali dayjest matni" in delivered[0]

    # Verify app state last_digest_date was set to today
    conn2 = store.get_connection(db_path)
    assert store.get_app_state(conn2, "last_digest_date") == "2026-10-29"
    conn2.close()


def test_h3_multipage_sent_ambiguous_pending_resumes_only_pending(tmp_path: Path, monkeypatch):
    """H3: Multi-page digest with page1=sent, page2=ambiguous, page3=pending.

    Next cycle must deliver ONLY page 3. Page 2 and Page 1 are NEVER re-sent.
    After page 3 succeeds, the outbox status transitions to 'ambiguous'.
    """
    db_path = tmp_path / "h3_multipage.db"
    conn = store.get_connection(db_path)

    pages = [
        ("Sahifa 1 matni", []),
        ("Sahifa 2 matni", []),
        ("Sahifa 3 matni", []),
    ]
    out_id = store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-29",
        rendered_text="Sahifa 1 matni",
        pages=pages,
        member_events=[],
    )

    # Simulate: Page 1 was sent, Page 2 was ambiguous (e.g. connection timeout), Page 3 unattempted pending
    store.mark_outbox_page_sent(conn, out_id, page_number=1)
    store.mark_outbox_page_status(conn, out_id, page_number=2, status="ambiguous")
    # Page 3 remains 'pending'

    # Pre-condition check: resolve_multipage_outbox_status must resolve to 'pending'
    status_resolved = runner.resolve_multipage_outbox_status(conn, out_id)
    assert status_resolved == "pending"

    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur.fetchone()[0] == "pending"
    conn.close()

    # Next cycle runs: must only deliver page 3
    delivered_pages = []

    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        delivered_pages.append((outbox_id, page_number, text))
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")

    # Only page 3 was delivered! Neither page 1 nor page 2 was re-sent!
    assert len(delivered_pages) == 1
    assert delivered_pages[0][1] == 3
    assert "Sahifa 3 matni" in delivered_pages[0][2]

    # After page 3 succeeded, outbox status must now be 'ambiguous' because page 2 is ambiguous
    conn2 = store.get_connection(db_path)
    cur2 = conn2.cursor()
    cur2.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur2.fetchone()[0] == "ambiguous"

    # Subsequent cycle: ambiguous outbox is never selected or retried
    delivered_pages.clear()
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")
    assert delivered_pages == []
    conn2.close()


def test_h3_resolve_multipage_outbox_status_precedence_and_attempts(tmp_path: Path):
    """H3: resolve_multipage_outbox_status precedence: failed -> pending -> ambiguous -> sent."""
    db_path = tmp_path / "h3_precedence.db"
    conn = store.get_connection(db_path)

    pages = [("P1", []), ("P2", []), ("P3", [])]
    out_id = store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-29",
        rendered_text="P1",
        pages=pages,
        member_events=[],
    )

    # 1. 'failed' page exists -> outbox status is 'failed', attempts incremented, next_attempt_at set
    store.mark_outbox_page_sent(conn, out_id, page_number=1)
    store.mark_outbox_page_status(conn, out_id, page_number=2, status="failed")
    store.mark_outbox_page_status(conn, out_id, page_number=3, status="pending")

    st1 = runner.resolve_multipage_outbox_status(conn, out_id, "HTTP 502")
    assert st1 == "failed"
    cur = conn.cursor()
    cur.execute("SELECT status, attempts, next_attempt_at FROM outbox WHERE id = ?", (out_id,))
    row1 = cur.fetchone()
    assert row1[0] == "failed"
    assert row1[1] == 1
    assert row1[2] != ""

    # Second failure attempt -> attempts == 2, next_attempt_at updated
    st2 = runner.resolve_multipage_outbox_status(conn, out_id, "HTTP 502")
    assert st2 == "failed"
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    row2 = cur.fetchone()
    assert row2[0] == "failed"
    assert row2[1] == 2

    # Third failure attempt -> transitions to 'failed_final'
    st3 = runner.resolve_multipage_outbox_status(conn, out_id, "HTTP 502")
    assert st3 == "failed_final"
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    row3 = cur.fetchone()
    assert row3[0] == "failed_final"
    assert row3[1] == 3

    # 2. Reset outbox: 'pending' page exists when no 'failed' page -> 'pending'
    out_id2 = store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-30",
        rendered_text="P1",
        pages=pages,
        member_events=[],
    )
    store.mark_outbox_page_sent(conn, out_id2, page_number=1)
    store.mark_outbox_page_status(conn, out_id2, page_number=2, status="ambiguous")
    # page 3 is pending
    st_p = runner.resolve_multipage_outbox_status(conn, out_id2)
    assert st_p == "pending"

    # 3. Only 'ambiguous' and 'sent' pages exist -> 'ambiguous'
    store.mark_outbox_page_sent(conn, out_id2, page_number=3)
    st_amb = runner.resolve_multipage_outbox_status(conn, out_id2)
    assert st_amb == "ambiguous"

    # 4. All pages sent -> 'sent'
    out_id3 = store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-31",
        rendered_text="P1",
        pages=pages,
        member_events=[],
    )
    store.mark_outbox_page_sent(conn, out_id3, page_number=1)
    store.mark_outbox_page_sent(conn, out_id3, page_number=2)
    store.mark_outbox_page_sent(conn, out_id3, page_number=3)
    st_sent = runner.resolve_multipage_outbox_status(conn, out_id3)
    assert st_sent == "sent"
    conn.close()


def test_h4_definite_failure_next_attempt_at_delays_immediate_cycle_and_sends_after_time_monkeypatch(tmp_path: Path, monkeypatch):
    """H4: next_attempt_at = now + 15 min * attempt. Immediate cycle does not send; sends after time elapses."""
    db_path = tmp_path / "h4_delay.db"
    conn = store.get_connection(db_path)

    pages = [("Sahifa 1", []), ("Sahifa 2", [])]
    out_id = store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-29",
        rendered_text="Sahifa 1",
        pages=pages,
        member_events=[],
    )
    today_str = "2026-10-29"
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Step 1: Initial attempt fails with definite HTTP 500 error
    def mock_deliver_fail(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        if page_number == 1 and conn is not None and outbox_id is not None:
            store.mark_outbox_page_failed(conn, outbox_id, 1, "HTTP 500 Internal Server Error")
        return False, "HTTP 500 Internal Server Error"

    monkeypatch.setattr(delivery, "deliver", mock_deliver_fail)

    base_time = datetime(2026, 10, 29, 10, 0, 0)

    class MockDatetime(datetime):
        current_time = base_time

        @classmethod
        def now(cls, tz=None):
            return cls.current_time

    monkeypatch.setattr(runner, "datetime", MockDatetime)
    monkeypatch.setattr(store, "datetime", MockDatetime)

    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date=today_str)

    # Verify status is failed, attempts is 1, and next_attempt_at is 15 minutes after base_time
    conn1 = store.get_connection(db_path)
    cur1 = conn1.cursor()
    cur1.execute("SELECT status, attempts, next_attempt_at FROM outbox WHERE id = ?", (out_id,))
    st, att, next_at = cur1.fetchone()
    assert st == "failed"
    assert att == 1
    expected_next = (base_time + timedelta(minutes=15)).isoformat()
    assert next_at == expected_next
    conn1.close()

    # Step 2: Immediate cycle 1 minute later -> must NOT deliver
    MockDatetime.current_time = base_time + timedelta(minutes=1)
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None, page_number=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date=today_str)
    assert delivered == [], "Immediate cycle must NOT retry before next_attempt_at"

    # Step 3: Cycle after 16 minutes (past next_attempt_at) -> delivers successfully!
    MockDatetime.current_time = base_time + timedelta(minutes=16)
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date=today_str)
    assert len(delivered) > 0, "Cycle after next_attempt_at must successfully retry"

    conn3 = store.get_connection(db_path)
    cur3 = conn3.cursor()
    cur3.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur3.fetchone()[0] == "sent"
    conn3.close()


def test_h2_unconfirmed_runtime_error_network_drop_classified_ambiguous_never_retried(tmp_path: Path, monkeypatch):
    """H2: Unconfirmed network drop (RuntimeError) is classified as ambiguous and never retried."""
    db_path = tmp_path / "h2_ambiguous.db"
    conn = store.get_connection(db_path)

    out_id = store.add_to_outbox(conn, "ev-h2", "high", "Alert for unconfirmed drop")
    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-good", "chat-drop"])

    sent_to = []

    def mock_send(token, chat_id, text, timeout=15):
        if chat_id == "chat-drop":
            raise RuntimeError("Connection reset by peer: unconfirmed outcome")
        sent_to.append(chat_id)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send)

    # Attempt 1: chat-good succeeds, chat-drop drops
    success, err = delivery.deliver("Alert for unconfirmed drop", dry_run=False, conn=conn, outbox_id=out_id)
    assert success is False
    assert sent_to == ["chat-good"]

    # Verify chat-drop status is ambiguous in deliveries
    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox_deliveries WHERE outbox_id = ? AND chat_id = 'chat-drop'", (out_id,))
    assert cur.fetchone()[0] == "ambiguous"

    # Attempt 2: Next cycle / retry attempt
    sent_to.clear()
    monkeypatch.setattr(delivery, "send_to_telegram", lambda *args, **kwargs: sent_to.append(args[1]))
    success2, err2 = delivery.deliver("Alert for unconfirmed drop", dry_run=False, conn=conn, outbox_id=out_id)

    # Neither chat-good (sent) nor chat-drop (ambiguous) is re-sent!
    assert sent_to == []
    conn.close()


def test_h4_high_priority_initial_failure_delays_retry_with_next_attempt_at(tmp_path: Path, monkeypatch):
    """H4: Initial failure of high-priority message sets next_attempt_at = now + 15 min.

    Immediate cycle does not retry; retry cycle after next_attempt_at delivers successfully.
    """
    db_path = tmp_path / "h4_high_priority.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-h4", "vendor": "Anthropic", "product": "Claude"}
    ev = parser.FeedEntry(
        entry_id="claude-3-7-sonnet",
        title="Introducing Claude 3.7 Sonnet (Major Release)",
        link="https://anthropic.com/news/37",
        published_at="2026-10-29T12:00:00Z",
        summary="Major model launch",
        raw_content="Claude 3.7 Sonnet is a new flagship model.",
    )
    store.record_unseen_entries(conn, source, [ev])
    conn.close()

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )
    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-1"])

    base_time = datetime(2026, 10, 29, 12, 0, 0)

    class MockDatetime(datetime):
        current_time = base_time

        @classmethod
        def now(cls, tz=None):
            return cls.current_time

    monkeypatch.setattr(runner, "datetime", MockDatetime)
    monkeypatch.setattr(store, "datetime", MockDatetime)

    # Initial deliver fails with definite HTTP 502
    def mock_send_fail(token, chat_id, text, timeout=15):
        raise urllib.error.HTTPError("https://api.telegram.org", 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_fail)

    # Cycle 1: First attempt fails
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date="2026-10-29")

    # Verify outbox state: status == 'failed', attempts == 1, next_attempt_at == base_time + 15 min
    conn1 = store.get_connection(db_path)
    cur1 = conn1.cursor()
    cur1.execute("SELECT id, status, attempts, next_attempt_at FROM outbox WHERE priority = 'high'")
    row = cur1.fetchone()
    assert row is not None
    out_id, st, att, next_at = row
    assert st == "failed"
    assert att == 1
    expected_next = (base_time + timedelta(minutes=15)).isoformat()
    assert next_at == expected_next
    conn1.close()

    # Cycle 2: Immediate cycle (1 minute later) -> must NOT retry/deliver
    MockDatetime.current_time = base_time + timedelta(minutes=1)
    delivered = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda *args, **kwargs: delivered.append(args[2]))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date="2026-10-29")
    assert delivered == [], "Immediate cycle must NOT retry before next_attempt_at"

    # Cycle 3: Cycle after 16 minutes -> past next_attempt_at -> delivers successfully!
    MockDatetime.current_time = base_time + timedelta(minutes=16)
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date="2026-10-29")
    assert len(delivered) == 1
    assert "Claude 3.7 Sonnet" in delivered[0]

    conn2 = store.get_connection(db_path)
    cur2 = conn2.cursor()
    cur2.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur2.fetchone()[0] == "sent"
    conn2.close()


