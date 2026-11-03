"""Tests for delivery state resolution and Codex senior findings scenarios.

Covers:
1. Single-page message with recipient A=ambiguous, B=HTTP 502 across 4 cycles:
   B is sent at most 3 times, A is never resent, next_attempt_at is populated on each definite failure,
   and outbox transitions to failed_final after the 3rd rejection.
2. Today's digest with page 1=sent, page 2=ambiguous, page 3=pending:
   A normal cycle (digest_mode=False, daily schedule not due) sends page 3, skips page 2.
3. digest_mode=True:
   When yesterday's digest retry is delivered today, today's new daily digest is NOT created or sent.
4. 4-page digest:
   In the first two cycles, ambiguous/pending updates do not increment attempts;
   after the first HTTP 502 rejection, outbox status is 'failed' (not 'failed_final').
5. Single page with recipient A=ambiguous, B=failed:
   Next cycle retries the page only to recipient B, never to A.
"""

from datetime import datetime, timedelta
from pathlib import Path
import urllib.error
import pytest

from src.vendor_watch import delivery, runner, store
from src.vendor_watch.delivery_state import derive_status, derive_recipient_status, derive_deliveries_status


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["failed"], "failed"),
        (["ambiguous", "failed"], "failed"),
        (["sent", "failed"], "failed"),
        (["pending", "failed"], "failed"),
        (["sending", "failed"], "failed"),
        (["pending"], "pending"),
        (["sending"], "pending"),
        (["ambiguous", "pending"], "pending"),
        (["sent", "pending"], "pending"),
        (["ambiguous", "sending"], "pending"),
        (["ambiguous"], "ambiguous"),
        (["sent", "ambiguous"], "ambiguous"),
        (["sent"], "sent"),
        (["sent", "sent"], "sent"),
        ([], "sent"),
    ],
)
def test_derive_status_table(statuses, expected):
    """T1: Table-driven unit test verifying derive_status precedence rules."""
    assert derive_status(statuses) == expected


@pytest.fixture(autouse=True)
def _block_real_vertex_and_telegram(monkeypatch):
    from src import vertex

    def _blocked_vertex(*args, **kwargs):
        raise RuntimeError("Real Vertex call blocked in tests")

    def _blocked_telegram(*args, **kwargs):
        raise RuntimeError("Real Telegram call blocked in tests")

    monkeypatch.setattr(vertex, "chat_json", _blocked_vertex)
    monkeypatch.setattr(delivery, "send_to_telegram", _blocked_telegram)


def test_scenario_1_single_page_ambiguous_and_definite_rejection_four_cycles(tmp_path: Path, monkeypatch):
    """Scenario 1: Single-page message, A=ambiguous, B=HTTP 502 across 4 cycles.

    B must not be sent more than 3 times; A is never resent;
    each definite rejection sets next_attempt_at; transitions to failed_final after 3 rejections.
    """
    db_path = tmp_path / "s1.db"
    conn = store.get_connection(db_path)
    out_id = store.add_to_outbox(conn, "ev-s1", "high", "High priority alert")

    # Prevent daily digest from running
    today_str = "2026-11-02"
    store.set_app_state(conn, "last_digest_date", today_str)

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-a", "chat-b"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    calls = []

    def mock_send(token, chat_id, text, timeout=15):
        calls.append(chat_id)
        if chat_id == "chat-a":
            raise TimeoutError("Simulated timeout for chat-a (ambiguous)")
        if chat_id == "chat-b":
            raise urllib.error.HTTPError("https://api.telegram.org", 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send)

    # --- Cycle 1 ---
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)
    assert calls == ["chat-a", "chat-b"]

    cur = conn.cursor()
    cur.execute("SELECT status, attempts, next_attempt_at FROM outbox WHERE id = ?", (out_id,))
    st1, att1, next_at1 = cur.fetchone()
    assert st1 == "failed", f"Expected 'failed' after cycle 1, got {st1}"
    assert att1 == 1, f"Expected attempts=1, got {att1}"
    assert next_at1 != "", "next_attempt_at must be populated on failure"
    assert next_at1 > today_str

    cur.execute("SELECT chat_id, status FROM outbox_deliveries WHERE outbox_id = ? ORDER BY chat_id", (out_id,))
    d_st1 = dict(cur.fetchall())
    assert d_st1["chat-a"] == "ambiguous"
    assert d_st1["chat-b"] == "failed"

    # --- Cycle 2 (advance simulated time past next_attempt_at) ---
    conn.execute("UPDATE outbox SET next_attempt_at = '2020-01-01T00:00:00' WHERE id = ?", (out_id,))
    conn.commit()
    calls.clear()

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)
    assert calls == ["chat-b"], f"Expected only chat-b in cycle 2, got {calls}"

    cur.execute("SELECT status, attempts, next_attempt_at FROM outbox WHERE id = ?", (out_id,))
    st2, att2, next_at2 = cur.fetchone()
    assert st2 == "failed", f"Expected 'failed' after cycle 2, got {st2}"
    assert att2 == 2, f"Expected attempts=2, got {att2}"
    assert next_at2 != ""

    # --- Cycle 3 (advance simulated time past next_attempt_at) ---
    conn.execute("UPDATE outbox SET next_attempt_at = '2020-01-01T00:00:00' WHERE id = ?", (out_id,))
    conn.commit()
    calls.clear()

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)
    assert calls == ["chat-b"], f"Expected only chat-b in cycle 3, got {calls}"

    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st3, att3 = cur.fetchone()
    assert st3 == "failed_final", f"Expected 'failed_final' after 3 rejections, got {st3}"
    assert att3 == 3, f"Expected attempts=3, got {att3}"

    # --- Cycle 4 (final state: no further delivery attempts) ---
    conn.execute("UPDATE outbox SET next_attempt_at = '2020-01-01T00:00:00' WHERE id = ?", (out_id,))
    conn.commit()
    calls.clear()

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)
    assert calls == [], f"Expected 0 calls in cycle 4 (failed_final), got {calls}"
    conn.close()


def test_scenario_2_digest_continuation_not_blocked_by_daily_schedule(tmp_path: Path, monkeypatch):
    """Scenario 2: Today's digest page 1 sent, page 2 ambiguous, page 3 pending.

    A normal cycle (digest_mode=False, daily schedule not due) must send page 3 and skip page 2.
    """
    db_path = tmp_path / "s2.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"

    pages = [("Dayjest 1", []), ("Dayjest 2", []), ("Dayjest 3", [])]
    out_id = store.add_digest_to_outbox(
        conn,
        key=f"digest:{today_str}",
        rendered_text="Dayjest 1",
        pages=pages,
        member_events=[],
    )

    # Page 1: sent
    store.mark_outbox_page_sent(conn, out_id, page_number=1)
    store.init_outbox_deliveries(conn, out_id, ["chat1"], page_number=1)
    store.record_delivery_attempt(conn, out_id, "chat1", success=True, page_number=1)

    # Page 2: ambiguous
    store.mark_outbox_page_status(conn, out_id, page_number=2, status="ambiguous")
    store.init_outbox_deliveries(conn, out_id, ["chat1"], page_number=2)
    store.record_delivery_attempt(conn, out_id, "chat1", success=False, error="timeout", page_number=2, status="ambiguous")

    # Page 3: pending
    store.init_outbox_deliveries(conn, out_id, ["chat1"], page_number=3)

    # Outbox status is pending
    store.update_outbox_status(conn, out_id, "pending")
    # Today digest was already marked as sent today so is_daily_due is False
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.close()

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat1"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    sent_texts = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda token, chat_id, text, timeout=15: sent_texts.append(text))

    # Run normal cycle with digest_mode=False
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date=today_str)

    # Must have sent page 3, not page 2
    assert len(sent_texts) == 1, f"Expected exactly 1 send, got {sent_texts}"
    assert "Dayjest 3" in sent_texts[0], f"Expected Page 3 to be sent, got {sent_texts[0]}"


def test_scenario_3_yesterday_digest_retry_suppresses_new_today_digest_in_digest_mode(tmp_path: Path, monkeypatch):
    """Scenario 3: digest_mode=True: yesterday's digest retried and delivered today.

    Today's new digest must NOT be created or sent in this cycle.
    """
    db_path = tmp_path / "s3.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"
    yesterday_str = "2026-11-01"

    # Yesterday's digest outbox item in pending
    pages = [("Kechagi dayjest matni", [])]
    y_out_id = store.add_digest_to_outbox(
        conn,
        key=f"digest:{yesterday_str}",
        rendered_text="Kechagi dayjest matni",
        pages=pages,
        member_events=[],
    )
    store.update_outbox_status(conn, y_out_id, "pending")
    store.set_app_state(conn, "last_digest_date", "2026-10-31")

    # Add a new event that would qualify for today's digest
    conn.execute(
        """
        INSERT INTO events
        (id, source_id, entry_id, vendor, product, title, link, published_at,
         content_hash, revision, raw_content, triage_type, priority, status, first_seen_at, last_seen_at)
        VALUES ('ev-today', 'src1', 'ent1', 'V', 'P', 'Bugungi yangilik', 'http://link', ?,
                'hash1', 1, 'Bugungi content', 'model_update', 'digest', 'unseen', ?, ?)
        """,
        (f"{today_str}T10:00:00", f"{today_str}T10:00:00", f"{today_str}T10:00:00"),
    )
    conn.execute(
        """
        INSERT INTO processing_jobs (event_id, revision, status, created_at, updated_at)
        VALUES ('ev-today', 1, 'pending', ?, ?)
        """,
        (f"{today_str}T10:00:00", f"{today_str}T10:00:00"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat1"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    sent_messages = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda token, chat_id, text, timeout=15: sent_messages.append(text))

    # Run cycle with digest_mode=True
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date=today_str)

    # Yesterday's digest was sent
    assert any("Kechagi dayjest matni" in msg for msg in sent_messages)

    # Today's new digest must NOT have been created or sent!
    conn2 = store.get_connection(db_path)
    today_digest = store.get_outbox_by_key(conn2, f"digest:{today_str}")
    assert today_digest is None, f"Today's new digest should NOT be created, found: {today_digest}"
    conn2.close()


def test_scenario_4_multipage_digest_attempts_not_incremented_on_ambiguous_first_failure_is_failed(tmp_path: Path, monkeypatch):
    """Scenario 4: 4-page digest: first 2 cycles with ambiguous/pending do not increment attempts;

    first HTTP 502 rejection marks status as 'failed' (attempts=1), not 'failed_final'.
    """
    db_path = tmp_path / "s4.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"

    pages = [("P1", []), ("P2", []), ("P3", []), ("P4", [])]
    out_id = store.add_digest_to_outbox(
        conn,
        key=f"digest:{today_str}",
        rendered_text="P1",
        pages=pages,
        member_events=[],
    )
    store.set_app_state(conn, "last_digest_date", today_str)

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat1"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # --- Cycle 1: P1 succeeds, P2 times out (ambiguous), P3/P4 pending ---
    def mock_send_c1(token, chat_id, text, timeout=15):
        if "P1" in text:
            return
        if "P2" in text:
            raise TimeoutError("P2 timeout (ambiguous)")
        raise RuntimeError("Unexpected send in cycle 1")

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_c1)
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    cur = conn.cursor()
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st1, att1 = cur.fetchone()
    assert st1 == "pending", f"Expected outbox 'pending' after cycle 1, got {st1}"
    assert att1 == 0, f"Expected attempts=0 after cycle 1 (no definite failure), got {att1}"

    # --- Cycle 2: P3 times out (ambiguous), P4 pending ---
    def mock_send_c2(token, chat_id, text, timeout=15):
        if "P3" in text:
            raise TimeoutError("P3 timeout (ambiguous)")
        raise RuntimeError(f"Unexpected send in cycle 2: {text}")

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_c2)
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st2, att2 = cur.fetchone()
    assert st2 == "pending", f"Expected outbox 'pending' after cycle 2, got {st2}"
    assert att2 == 0, f"Expected attempts=0 after cycle 2 (no definite failure), got {att2}"

    # --- Cycle 3: P4 fails with HTTP 502 (definite failure) ---
    def mock_send_c3(token, chat_id, text, timeout=15):
        if "P4" in text:
            raise urllib.error.HTTPError("https://api.telegram.org", 502, "Bad Gateway", {}, None)
        raise RuntimeError(f"Unexpected send in cycle 3: {text}")

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_c3)
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    st3, att3 = cur.fetchone()
    assert st3 == "failed", f"Expected 'failed' after cycle 3, got {st3} (must not be failed_final!)"
    assert att3 == 1, f"Expected attempts=1 after first definite failure, got {att3}"
    conn.close()


def test_scenario_5_page_mixed_recipients_retry_sends_only_failed_recipient(tmp_path: Path, monkeypatch):
    """Scenario 5: Single page with A=ambiguous, B=failed:

    Next cycle must retry this page only to B, never to A.
    """
    db_path = tmp_path / "s5.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"

    pages = [("Dayjest sahifasi", [])]
    out_id = store.add_digest_to_outbox(
        conn,
        key=f"digest:{today_str}",
        rendered_text="Dayjest sahifasi",
        pages=pages,
        member_events=[],
    )
    store.set_app_state(conn, "last_digest_date", today_str)

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-a", "chat-b"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # --- Cycle 1: chat-a ambiguous, chat-b failed ---
    calls_c1 = []

    def mock_send_c1(token, chat_id, text, timeout=15):
        calls_c1.append(chat_id)
        if chat_id == "chat-a":
            raise TimeoutError("Timeout for chat-a (ambiguous)")
        if chat_id == "chat-b":
            raise urllib.error.HTTPError("https://api.telegram.org", 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_c1)
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    assert calls_c1 == ["chat-a", "chat-b"]

    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ? AND page_number = 1", (out_id,))
    page_st1 = cur.fetchone()[0]
    assert page_st1 == "failed", f"Expected page status 'failed', got {page_st1}"

    # --- Cycle 2: Next cycle should retry only chat-b ---
    conn.execute("UPDATE outbox SET next_attempt_at = '2020-01-01T00:00:00' WHERE id = ?", (out_id,))
    conn.commit()

    calls_c2 = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda token, chat_id, text, timeout=15: calls_c2.append(chat_id))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    assert calls_c2 == ["chat-b"], f"Expected retry only to chat-b, got {calls_c2}"
    conn.close()


def test_finding_1_page_retry_success_with_ambiguous_recipient_remains_ambiguous(tmp_path: Path, monkeypatch):
    """Finding 1 / T1: On a page with A=ambiguous, B=failed, when B succeeds on retry,
    if A is still ambiguous, the page and outbox must resolve to 'ambiguous', NOT 'sent'.
    """
    db_path = tmp_path / "f1.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"

    pages = [("Dayjest sahifasi 1", [])]
    out_id = store.add_digest_to_outbox(
        conn,
        key=f"digest:{today_str}",
        rendered_text="Dayjest sahifasi 1",
        pages=pages,
        member_events=[],
    )
    store.set_app_state(conn, "last_digest_date", today_str)

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-a", "chat-b"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # --- Cycle 1: chat-a ambiguous, chat-b failed ---
    def mock_send_c1(token, chat_id, text, timeout=15):
        if chat_id == "chat-a":
            raise TimeoutError("Timeout for chat-a (ambiguous)")
        if chat_id == "chat-b":
            raise urllib.error.HTTPError("https://api.telegram.org", 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_c1)
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ? AND page_number = 1", (out_id,))
    assert cur.fetchone()[0] == "failed"

    # --- Cycle 2: advance next_attempt_at, chat-b succeeds ---
    conn.execute("UPDATE outbox SET next_attempt_at = '2020-01-01T00:00:00' WHERE id = ?", (out_id,))
    conn.commit()

    calls_c2 = []

    def mock_send_c2(token, chat_id, text, timeout=15):
        calls_c2.append(chat_id)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_c2)
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    assert calls_c2 == ["chat-b"]

    cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ? AND page_number = 1", (out_id,))
    page_st2 = cur.fetchone()[0]
    assert page_st2 == "ambiguous", f"Page status must be 'ambiguous' (chat-a is ambiguous, chat-b is sent), got {page_st2}"

    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    out_st2 = cur.fetchone()[0]
    assert out_st2 == "ambiguous", f"Outbox status must be 'ambiguous', got {out_st2}"
    conn.close()


def test_finding_2_single_page_digest_continuation_not_blocked_by_daily_schedule(tmp_path: Path, monkeypatch):
    """Finding 2 / T4: A single-page (no outbox_pages) existing digest with chat-a=sent, chat-b=pending
    must be continued even when is_daily_due=False and digest_mode=False.
    """
    db_path = tmp_path / "f2.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"

    out_id = store.add_to_outbox(
        conn,
        event_id=f"digest:{today_str}",
        priority="digest",
        rendered_text="Single-page digest text",
    )
    # chat-a is sent, chat-b is pending
    store.init_outbox_deliveries(conn, out_id, ["chat-a", "chat-b"], page_number=1)
    store.record_delivery_attempt(conn, out_id, "chat-a", success=True, page_number=1)
    store.update_outbox_status(conn, out_id, "pending")

    # Mark daily schedule not due
    store.set_app_state(conn, "last_digest_date", today_str)
    conn.commit()
    conn.close()

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-a", "chat-b"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    sent_to = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda token, chat_id, text, timeout=15: sent_to.append(chat_id))

    # Run cycle with digest_mode=False, daily schedule not due
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date=today_str)

    assert sent_to == ["chat-b"], f"Expected chat-b to receive pending single-page digest, got {sent_to}"


@pytest.mark.parametrize(
    "rows,expected",
    [
        # Rule 1: sent present -> sent
        (["sent"], "sent"),
        (["failed", "sent"], "sent"),
        (["sent", "failed"], "sent"),
        (["ambiguous", "sent"], "sent"),
        (["sent", "ambiguous"], "sent"),
        (["pending", "sent"], "sent"),
        (["sent", "pending"], "sent"),
        (["failed_final", "sent"], "sent"),
        (["failed", "ambiguous", "sent"], "sent"),
        ([{"status": "failed"}, {"status": "sent"}], "sent"),
        ([("chat-1", "failed"), ("chat-1", "sent")], "sent"),

        # Rule 2: ambiguous present (no sent) -> ambiguous
        (["ambiguous"], "ambiguous"),
        (["failed", "ambiguous"], "ambiguous"),
        (["ambiguous", "failed"], "ambiguous"),
        (["pending", "ambiguous"], "ambiguous"),
        (["ambiguous", "pending"], "ambiguous"),
        (["sending", "ambiguous"], "ambiguous"),
        (["failed_final", "ambiguous"], "ambiguous"),
        (["failed", "failed", "ambiguous"], "ambiguous"),
        ([{"status": "failed"}, {"status": "ambiguous"}], "ambiguous"),
        ([("chat-1", "failed"), ("chat-1", "ambiguous")], "ambiguous"),

        # Rule 3: neither sent nor ambiguous -> latest row status (time / rowid / order)
        (["failed"], "failed"),
        (["failed", "failed"], "failed"),
        (["failed", "pending"], "pending"),
        (["pending", "failed"], "failed"),
        (["failed", "sending"], "sending"),
        (["sending", "failed"], "failed"),
        (["failed", "failed_final"], "failed_final"),
        (["pending"], "pending"),
        ([], "sent"),
        ([("chat-1", "pending"), ("chat-1", "failed")], "failed"),
        ([("chat-1", "failed"), ("chat-1", "pending")], "pending"),
    ],
)
def test_d1_derive_recipient_status_table(rows, expected):
    """D1 table test: verifying single-pair effective status precedence."""
    assert derive_recipient_status(rows) == expected


def test_d3_codex_scenario_failed_and_sent_no_failed_final(tmp_path: Path, monkeypatch):
    """Codex scenario: A pair with legacy 'failed' + current 'sent' rows.
    Must resolve page to 'sent', make 0 HTTP calls over next 3 cycles,
    leave attempts unchanged, and never transition to 'failed_final'.
    """
    db_path = tmp_path / "codex.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"
    store.set_app_state(conn, "last_digest_date", today_str)
    out_id = store.add_to_outbox(conn, "ev-codex", "high", "High alert text")

    # Multipage digest page 1
    conn.execute(
        "INSERT INTO outbox_pages (outbox_id, page_number, rendered_text, status) VALUES (?, 1, 'Page 1 text', 'pending')",
        (out_id,),
    )
    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, error_message) "
        "VALUES (?, 1, 'chat-1', 'failed', 1, 'HTTP 502 Bad Gateway')",
        (out_id,),
    )
    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, error_message) "
        "VALUES (?, 1, 'chat-1', 'sent', 1, '')",
        (out_id,),
    )
    conn.commit()

    # Recalculate startup statuses
    store._derive_consistent_outbox_and_page_statuses(conn)
    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ? AND page_number = 1", (out_id,))
    p_st = cur.fetchone()[0]
    assert p_st == "sent", f"Page status must be 'sent', got {p_st}"

    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    out_st, att_init = cur.fetchone()
    assert out_st == "sent", f"Outbox status must be 'sent', got {out_st}"
    conn.commit()
    conn.close()

    # Now run runner across 3 cycles
    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-1"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    http_calls = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda token, cid, txt, timeout=15: http_calls.append(cid))

    for cycle in range(1, 4):
        runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    assert http_calls == [], f"Expected 0 HTTP calls, got {http_calls}"

    conn2 = store.get_connection(db_path)
    cur = conn2.cursor()
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    final_st, final_att = cur.fetchone()
    assert final_att == att_init, f"Attempts must not increment, got {final_att} vs {att_init}"
    assert final_st == "sent", f"Outbox must remain 'sent', never 'failed_final', got {final_st}"
    conn2.close()


def test_d3_codex_scenario_single_page_failed_and_sent_no_failed_final(tmp_path: Path, monkeypatch):
    """Codex scenario on single-page outbox (no outbox_pages table rows).
    Legacy 'failed' + current 'sent' must resolve outbox to 'sent', 0 HTTP calls, no failed_final.
    """
    db_path = tmp_path / "codex_single.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"
    store.set_app_state(conn, "last_digest_date", today_str)
    out_id = store.add_to_outbox(conn, "ev-codex-single", "high", "Single alert text")

    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, error_message) "
        "VALUES (?, 1, 'chat-1', 'failed', 1, 'HTTP 502 Bad Gateway')",
        (out_id,),
    )
    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, error_message) "
        "VALUES (?, 1, 'chat-1', 'sent', 1, '')",
        (out_id,),
    )
    conn.commit()

    store._derive_consistent_outbox_and_page_statuses(conn)
    cur = conn.cursor()
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    out_st, att_init = cur.fetchone()
    assert out_st == "sent", f"Outbox status must be 'sent', got {out_st}"
    conn.commit()
    conn.close()

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-1"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    http_calls = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda token, cid, txt, timeout=15: http_calls.append(cid))

    for cycle in range(1, 4):
        runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)

    assert http_calls == [], f"Expected 0 HTTP calls, got {http_calls}"

    conn2 = store.get_connection(db_path)
    cur = conn2.cursor()
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    final_st, final_att = cur.fetchone()
    assert final_att == att_init, f"Attempts must not increment, got {final_att} vs {att_init}"
    assert final_st == "sent", f"Outbox must remain 'sent', never 'failed_final', got {final_st}"
    conn2.close()


def test_d3_failed_and_ambiguous_remains_ambiguous_no_resend(tmp_path: Path, monkeypatch):
    """Scenario: A pair with legacy 'failed' + current 'ambiguous' rows.
    Must resolve to 'ambiguous', and must NOT be resent in subsequent cycle.
    """
    db_path = tmp_path / "failed_ambig.db"
    conn = store.get_connection(db_path)
    today_str = "2026-11-02"
    store.set_app_state(conn, "last_digest_date", today_str)
    out_id = store.add_to_outbox(conn, "ev-ambig", "high", "High alert text")
    conn.execute(
        "INSERT INTO outbox_pages (outbox_id, page_number, rendered_text, status) VALUES (?, 1, 'Page 1 text', 'pending')",
        (out_id,),
    )

    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, error_message) "
        "VALUES (?, 1, 'chat-1', 'failed', 1, 'HTTP 502 Bad Gateway')",
        (out_id,),
    )
    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, error_message) "
        "VALUES (?, 1, 'chat-1', 'ambiguous', 1, 'Connection reset by peer')",
        (out_id,),
    )
    conn.commit()

    store._derive_consistent_outbox_and_page_statuses(conn)
    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ? AND page_number = 1", (out_id,))
    p_st = cur.fetchone()[0]
    assert p_st == "ambiguous", f"Page status must be 'ambiguous', got {p_st}"

    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    out_st = cur.fetchone()[0]
    assert out_st == "ambiguous", f"Outbox status must be 'ambiguous', got {out_st}"
    conn.commit()
    conn.close()

    # Next cycle should NOT resend chat-1
    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-1"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    http_calls = []
    monkeypatch.setattr(delivery, "send_to_telegram", lambda token, cid, txt, timeout=15: http_calls.append(cid))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=today_str)
    assert http_calls == [], f"Expected 0 HTTP calls (ambiguous must not be resent), got {http_calls}"


def test_d3_only_failed_failed_remains_failed(tmp_path: Path):
    """Scenario: A pair with only 'failed', 'failed' rows must resolve to 'failed'."""
    db_path = tmp_path / "failed_failed.db"
    conn = store.get_connection(db_path)
    out_id = store.add_to_outbox(conn, "ev-ff", "high", "High alert text")
    conn.execute(
        "INSERT INTO outbox_pages (outbox_id, page_number, rendered_text, status) VALUES (?, 1, 'Page 1 text', 'pending')",
        (out_id,),
    )

    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, error_message) "
        "VALUES (?, 1, 'chat-1', 'failed', 1, 'HTTP 500 Internal Server Error')",
        (out_id,),
    )
    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status, attempts, error_message) "
        "VALUES (?, 1, 'chat-1', 'failed', 2, 'HTTP 502 Bad Gateway')",
        (out_id,),
    )
    conn.commit()

    store._derive_consistent_outbox_and_page_statuses(conn)
    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = ? AND page_number = 1", (out_id,))
    p_st = cur.fetchone()[0]
    assert p_st == "failed", f"Page status must be 'failed', got {p_st}"

    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    out_st = cur.fetchone()[0]
    assert out_st == "failed", f"Outbox status must be 'failed', got {out_st}"
    conn.close()


def test_record_delivery_attempt_in_flight_preserves_sending_when_one_recipient_failed(tmp_path: Path):
    """record_delivery_attempt must not prematurely fail outbox while other recipients are pending."""
    db_path = tmp_path / "in_flight.db"
    conn = store.get_connection(db_path)
    out_id = store.add_to_outbox(conn, "ev-inflight", "high", "Alert for 2 recipients")

    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status) VALUES (?, 1, 'chat-1', 'pending')",
        (out_id,),
    )
    conn.execute(
        "INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status) VALUES (?, 1, 'chat-2', 'pending')",
        (out_id,),
    )
    conn.commit()

    # chat-1 fails, chat-2 is still pending
    store.record_delivery_attempt(conn, out_id, "chat-1", False, "502 Bad Gateway", 1)

    cur = conn.cursor()
    cur.execute("SELECT status, attempts FROM outbox WHERE id = ?", (out_id,))
    out_st, att = cur.fetchone()
    assert out_st == "pending", f"Outbox must remain in-flight (pending), got {out_st}"
    assert att == 0, f"Outbox attempts must not increment prematurely, got {att}"

    # Now chat-2 succeeds
    store.record_delivery_attempt(conn, out_id, "chat-2", True, "", 1)
    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    final_st = cur.fetchone()[0]
    assert final_st == "failed", f"Final outbox status must be 'failed' once all recipients are done, got {final_st}"
    conn.close()


def test_derive_recipient_status_sqlite_row_with_null_fields():
    """derive_recipient_status must handle sqlite3.Row with NULL id or last_attempt_at gracefully."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("CREATE TABLE d (chat_id TEXT, status TEXT, last_attempt_at TEXT, id INTEGER)")
    cur.execute("INSERT INTO d VALUES (?, ?, ?, ?)", ("chat-1", "failed", None, None))
    cur.execute("INSERT INTO d VALUES (?, ?, ?, ?)", ("chat-1", "pending", "2026-11-02T10:00:00", 2))
    cur.execute("SELECT * FROM d ORDER BY rowid ASC")
    rows = cur.fetchall()

    # Latest row by order should be 'pending', and row with NULL must not raise TypeError
    effective = derive_recipient_status(rows)
    assert effective == "pending", f"Expected 'pending' as latest status, got {effective}"
    conn.close()


def test_derive_deliveries_status_no_chat_id_dicts_extracts_status():
    """derive_deliveries_status with dicts without chat_id must extract status, not return 'sent'."""
    assert derive_deliveries_status([{"status": "failed"}]) == "failed"
    assert derive_deliveries_status([{"status": "pending"}]) == "pending"


def test_derive_recipient_status_error_message_with_hyphen_not_treated_as_timestamp():
    """Error message strings containing '-' or 'T' must not misorder delivery rows."""
    rows = [
        ("c", "failed", "HTTP 502 - Bad Gateway", ""),
        ("c", "pending", "", "2026-11-02T10:00:00"),
    ]
    assert derive_recipient_status(rows) == "pending"

