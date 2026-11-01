"""Tests for deduplication, outbox unique constraints, enrichment failure handling, and triage."""

import json
from pathlib import Path
import shutil
import sqlite3
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


def test_two_runs_failed_first_delivery_one_message_total(tmp_path: Path, monkeypatch):
    """Run 1 delivery fails -> Run 2 succeeds -> exactly 1 message delivered, 1 outbox entry reused."""
    db_path = tmp_path / "test_dedup.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-dedup", "vendor": "OpenAI", "product": "GPT-4o"}
    entry = parser.FeedEntry(
        entry_id="gpt-4o-release",
        title="OpenAI releases GPT-4o model",
        link="https://openai.com/index/gpt-4o",
        published_at="2026-10-29T00:00:00Z",
        summary="GPT-4o model release announcement",
        raw_content="OpenAI introduces GPT-4o, our new flagship multimodal model.",
    )
    store.record_unseen_entries(conn, source, [entry])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    # Run 1: Telegram delivery fails
    delivered = []
    should_fail = True

    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None):
        if should_fail:
            return False, "Telegram network timeout"
        delivered.append(text)
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 0

    # Verify outbox has 1 pending row
    cur = conn.cursor()
    cur.execute("SELECT id, event_id, priority, status, attempts FROM outbox")
    rows = cur.fetchall()
    assert len(rows) == 1
    outbox_id, event_id, priority, status, attempts = rows[0]
    assert event_id == "src-dedup:gpt-4o-release"
    assert status in ("pending", "failed")
    assert attempts >= 1

    # Run 2: Delivery succeeds
    should_fail = False
    with store.get_connection(db_path) as c:
        c.execute("UPDATE outbox SET next_attempt_at = ''")
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 1

    # Exactly 1 outbox entry exists (reused same row, not duplicated)
    cur.execute("SELECT id, event_id, status FROM outbox")
    rows_after = cur.fetchall()
    assert len(rows_after) == 1
    assert rows_after[0][0] == outbox_id
    assert rows_after[0][2] == "sent"

    # Run 3: Subsequent run does not re-send
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 1


def test_startup_collapse_of_duplicates(tmp_path: Path):
    """Startup duplicate collapse keeps earliest sent or newest pending and deletes duplicates."""
    db_path = tmp_path / "test_collapse.db"
    conn = store.get_connection(db_path)

    # Temporarily drop unique index to simulate duplicate rows from older code
    conn.execute("DROP INDEX IF EXISTS idx_outbox_event_kind")

    event_id = "openai-news:https://openai.com/index/retell-ai"

    # Insert 4 duplicate sent rows (like in the real incident)
    conn.execute(
        "INSERT INTO outbox (event_id, priority, kind, rendered_text, status, attempts, created_at) "
        "VALUES (?, 'high', 'high', 'Text 1 (earliest)', 'sent', 1, '2026-10-29T08:33:07')",
        (event_id,),
    )
    conn.execute(
        "INSERT INTO outbox (event_id, priority, kind, rendered_text, status, attempts, created_at) "
        "VALUES (?, 'high', 'high', 'Text 2', 'sent', 1, '2026-10-29T09:33:05')",
        (event_id,),
    )
    conn.execute(
        "INSERT INTO outbox (event_id, priority, kind, rendered_text, status, attempts, created_at) "
        "VALUES (?, 'high', 'high', 'Text 3', 'sent', 1, '2026-10-29T10:33:07')",
        (event_id,),
    )
    conn.execute(
        "INSERT INTO outbox (event_id, priority, kind, rendered_text, status, attempts, created_at) "
        "VALUES (?, 'high', 'high', 'Text 4 (latest sent)', 'sent', 1, '2026-10-29T11:33:10')",
        (event_id,),
    )

    # Insert 3 duplicate pending digest rows
    conn.execute(
        "INSERT INTO outbox (event_id, priority, kind, rendered_text, status, attempts, created_at) "
        "VALUES ('digest', 'digest', 'digest', 'Digest 1 (old)', 'pending', 0, '2026-10-29T07:00:00')",
    )
    conn.execute(
        "INSERT INTO outbox (event_id, priority, kind, rendered_text, status, attempts, created_at) "
        "VALUES ('digest', 'digest', 'digest', 'Digest 2 (mid)', 'pending', 0, '2026-10-29T08:00:00')",
    )
    conn.execute(
        "INSERT INTO outbox (event_id, priority, kind, rendered_text, status, attempts, created_at) "
        "VALUES ('digest', 'digest', 'digest', 'Digest 3 (newest pending)', 'pending', 0, '2026-10-29T09:00:00')",
    )
    conn.commit()

    # Total rows before collapse
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM outbox")
    assert cur.fetchone()[0] == 7

    # Run collapse
    store.collapse_outbox_duplicates(conn)

    # Verify duplicate sent rows collapsed: earliest sent is kept ('Text 1 (earliest)')
    cur.execute("SELECT rendered_text, status FROM outbox WHERE event_id = ?", (event_id,))
    sent_rows = cur.fetchall()
    assert len(sent_rows) == 1
    assert sent_rows[0][0] == "Text 1 (earliest)"
    assert sent_rows[0][1] == "sent"

    # Verify duplicate pending rows collapsed: newest pending is kept ('Digest 3 (newest pending)')
    cur.execute("SELECT rendered_text, status FROM outbox WHERE event_id = 'digest'")
    pending_rows = cur.fetchall()
    assert len(pending_rows) == 1
    assert pending_rows[0][0] == "Digest 3 (newest pending)"
    assert pending_rows[0][1] == "pending"

    # Verify unique index is created/valid
    cur.execute("PRAGMA index_info(idx_outbox_event_kind)")
    index_cols = [r[2] for r in cur.fetchall()]
    assert "event_id" in index_cols
    assert "kind" in index_cols


def test_failed_enrichment_not_sent_as_high(tmp_path: Path, monkeypatch):
    """An item whose enrichment failed is never sent as high; bounded retries then digest marked unenriched."""
    db_path = tmp_path / "test_failed_enr.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-enr-fail", "vendor": "OpenAI", "product": "GPT-4o"}
    entry = parser.FeedEntry(
        entry_id="gpt-4o-voice",
        title="OpenAI releases GPT-4o Realtime Voice API model",
        link="https://openai.com/index/voice-api",
        published_at="2026-10-29T00:00:00Z",
        summary="Voice API release notes in English",
        raw_content="Raw English technical documentation for GPT-4o voice release.",
    )
    store.record_unseen_entries(conn, source, [entry])

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    delivered_high = []
    delivered_digest = []

    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None):
        if "Yuqori Muhimlik" in text or "🔔" in text:
            delivered_high.append(text)
        elif "Dayjest" in text or "📋" in text:
            delivered_digest.append(text)
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)

    # Real vertex fails, returning raw fallback
    # Run 1: attempt 1/3 - should NOT be sent as high priority
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered_high) == 0

    # Run 2: attempt 2/3 - still kept pending for retry
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered_high) == 0

    # Run 3: attempt 3/3 - still kept pending
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered_high) == 0

    # Run 4: bounded retries exceeded (>3) -> moved to daily digest marked [Boyitilmagan]
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)
    assert len(delivered_high) == 0
    assert len(delivered_digest) >= 1
    assert any("[Boyitilmagan]" in d for d in delivered_digest)

    # Verify no raw English text was ever sent as high priority
    for msg in delivered_high:
        assert not store.is_raw_or_unenriched_text(msg)


def test_triage_case_study():
    """Customer stories, case studies, and partner posts are at most digest; new_model requires announcement."""
    # Retell AI incident text
    retell_event = {
        "title": "Customizable, no-code voice agent automation with GPT-4o",
        "raw_content": (
            "Retell AI is transforming the call center experience with hyper-realistic "
            "conversational voice agents powered by GPT-4o."
        ),
        "product": "OpenAI News",
    }
    t_type, prio = triage.classify_event(retell_event)
    assert prio != "high"
    assert prio in ("digest", "ignore")
    assert t_type != "new_model"

    # Other customer stories and case studies
    customer_cases = [
        {
            "title": "Canva: How Canva uses Claude to power creative design",
            "raw_content": "A customer story on how Canva leverages Claude for users.",
            "product": "Anthropic News",
        },
        {
            "title": "Duolingo case study: AI-driven language learning",
            "raw_content": "Duolingo is partnering with OpenAI to improve learning outcomes.",
            "product": "Customer Stories",
        },
        {
            "title": "Partner post: Transforming enterprise workflows with Gemini",
            "raw_content": "How our partner Acme Corp is transforming internal search.",
            "product": "Google Cloud Blog",
        },
    ]
    for case in customer_cases:
        t_type, prio = triage.classify_event(case)
        assert prio != "high", f"Case '{case['title']}' should not be high priority"
        assert prio in ("digest", "ignore")
        assert t_type != "new_model"

    # Genuine model releases: model name/id + release wording
    genuine_releases = [
        {
            "title": "Google introduces Gemini 2.5 Flash foundation model",
            "raw_content": "We are releasing Gemini 2.5 Flash, a new state-of-the-art model.",
            "product": "Google AI",
        },
        {
            "title": "OpenAI launches GPT-5 flagship model",
            "raw_content": "Today we announce the release of GPT-5 model.",
            "product": "OpenAI Blog",
        },
        {
            "title": "Anthropic releases Claude 3.5 Sonnet foundation model",
            "raw_content": "Announcing the release of Claude 3.5 Sonnet model.",
            "product": "Anthropic Announcements",
        },
    ]
    for rel in genuine_releases:
        t_type, prio = triage.classify_event(rel)
        assert t_type == "new_model", f"Release '{rel['title']}' should be new_model"
        assert prio == "high", f"Release '{rel['title']}' should be high priority"


def test_digest_two_days_yields_two_digests(tmp_path: Path, monkeypatch):
    """Two days produce two separate daily digests, keyed by date."""
    db_path = tmp_path / "test_two_days.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-daily", "vendor": "Anthropic", "product": "Claude Code"}
    ev_day1 = parser.FeedEntry(
        entry_id="v1.0.1",
        title="Release v1.0.1 (Patch)",
        link="https://example.com/v101",
        published_at="2026-10-28T08:00:00Z",
        summary="Patch 1",
        raw_content="Routine patch release.",
    )
    store.record_unseen_entries(conn, source, [ev_day1])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    # Day 1: Run cycle for 2026-10-28
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-28")
    assert len(delivered) == 1
    assert "v1.0.1" in delivered[0]

    # Day 2: New entry arrives on 2026-10-29
    ev_day2 = parser.FeedEntry(
        entry_id="v1.0.2",
        title="Release v1.0.2 (Patch)",
        link="https://example.com/v102",
        published_at="2026-10-29T08:00:00Z",
        summary="Patch 2",
        raw_content="Routine patch release 2.",
    )
    store.record_unseen_entries(conn, source, [ev_day2])

    # Day 2: Run cycle for 2026-10-29
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")
    assert len(delivered) == 2
    assert "v1.0.2" in delivered[1]

    # Verify outbox has separate date-keyed entries
    cur = conn.cursor()
    cur.execute("SELECT event_id, status FROM outbox WHERE kind = 'digest' ORDER BY id ASC")
    rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "digest:2026-10-28" and rows[0][1] == "sent"
    assert rows[1][0] == "digest:2026-10-29" and rows[1][1] == "sent"


def test_digest_same_day_twice_yields_one_digest(tmp_path: Path, monkeypatch):
    """Running digest cycle twice on the same day delivers exactly one digest."""
    db_path = tmp_path / "test_same_day.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-same-day", "vendor": "Google", "product": "GenAI SDK"}
    ev = parser.FeedEntry(
        entry_id="v0.5.0",
        title="Release v0.5.0 (Patch)",
        link="https://example.com/v050",
        published_at="2026-10-29T09:00:00Z",
        summary="SDK patch",
        raw_content="Minor patch update.",
    )
    store.record_unseen_entries(conn, source, [ev])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    # Run 1 on 2026-10-29
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")
    assert len(delivered) == 1

    # Run 2 on same day (2026-10-29) with digest_mode=True: must NOT send a second digest
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")
    assert len(delivered) == 1

    # Run 3 on same day with ordinary run (digest_mode=False): also does not send
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date="2026-10-29")
    assert len(delivered) == 1


def test_revision_update_sent_once_and_same_revision_twice_nothing(tmp_path: Path, monkeypatch):
    """Rev 2 with changed content is sent once as an update; identical rev sent never twice."""
    db_path = tmp_path / "test_rev.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-rev", "vendor": "OpenAI", "product": "GPT-4o"}
    ev_rev1 = parser.FeedEntry(
        entry_id="gpt-4o-security",
        title="OpenAI GPT-4o Security Advisory",
        link="https://openai.com/security/1",
        published_at="2026-10-29T01:00:00Z",
        summary="Security Advisory",
        raw_content="Initial security bulletin regarding prompt injection.",
    )
    store.record_unseen_entries(conn, source, [ev_rev1])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    # Run 1: Rev 1 is delivered
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 1
    assert "Security Advisory" in delivered[0]

    # Feed updates to Rev 2 with changed content (security fix details)
    ev_rev2 = parser.FeedEntry(
        entry_id="gpt-4o-security",
        title="OpenAI GPT-4o Security Advisory (Updated Fix)",
        link="https://openai.com/security/1-fix",
        published_at="2026-10-29T03:00:00Z",
        summary="Updated security advisory",
        raw_content="Critical mitigation released for CVE-2026-12345.",
    )
    store.record_unseen_entries(conn, source, [ev_rev2])

    delivered.clear()
    # Run 2: Rev 2 with new content -> exactly 1 update message
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 1
    assert "Updated Fix" in delivered[0] or "CVE-2026-12345" in delivered[0]

    delivered.clear()
    # Run 3: Same rev 2 in feed (unchanged) -> nothing delivered
    store.record_unseen_entries(conn, source, [ev_rev2])
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 0

    # Run 4: Another cycle without feed call -> still nothing delivered
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 0


def test_regression_topilma1_digest_items_during_day_accumulate_without_overwrite(tmp_path: Path, monkeypatch):
    """Topilma 1: Kun davomida dayjest yuborilgach kelgan bir nechta partiyadagi hodisalar overwrite bo'lmasdan ertangi kunga saqlanadi."""
    db_path = tmp_path / "test_accumulate.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-accum", "vendor": "Google", "product": "Gemini SDK"}
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    # 1. 2026-10-29 kuni ertalabki dayjest yuboriladi
    ev_morning = parser.FeedEntry(
        entry_id="v1.0.0",
        title="Release v1.0.0 (Patch)",
        link="https://example.com/v100",
        published_at="2026-10-29T08:00:00Z",
        summary="Morning patch",
        raw_content="Morning patch release notes.",
    )
    store.record_unseen_entries(conn, source, [ev_morning])
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")
    assert len(delivered) == 1
    assert "v1.0.0" in delivered[0]

    # 2. Kun davomida 1-partiya keladi (soat 11:00)
    ev_part1 = parser.FeedEntry(
        entry_id="v1.0.1",
        title="Release v1.0.1 (Patch)",
        link="https://example.com/v101",
        published_at="2026-10-29T11:00:00Z",
        summary="Midday patch",
        raw_content="Midday patch release notes.",
    )
    store.record_unseen_entries(conn, source, [ev_part1])
    # Oddiy sikl (digest_mode=False)
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date="2026-10-29")
    # Bugun qayta dayjest yuborilmaydi
    assert len(delivered) == 1
    # 1-partiya hodisasi processing_jobs'da 'pending' holatida qolishi kerak (done qilib tashlanmagan)
    jobs = store.get_pending_processing_jobs(conn)
    assert any(j["entry_id"] == "v1.0.1" for j in jobs)

    # 3. Kun davomida 2-partiya keladi (soat 16:00)
    ev_part2 = parser.FeedEntry(
        entry_id="v1.0.2",
        title="Release v1.0.2 (Patch)",
        link="https://example.com/v102",
        published_at="2026-10-29T16:00:00Z",
        summary="Afternoon patch",
        raw_content="Afternoon patch release notes.",
    )
    store.record_unseen_entries(conn, source, [ev_part2])
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date="2026-10-29")
    assert len(delivered) == 1
    # Ikkala hodisa ham processing_jobs'da mavjud bo'lishi kerak
    jobs = store.get_pending_processing_jobs(conn)
    assert any(j["entry_id"] == "v1.0.1" for j in jobs)
    assert any(j["entry_id"] == "v1.0.2" for j in jobs)

    # 4. Ertasi kuni (2026-10-30) rejaviy dayjest sikli
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date="2026-10-30")
    assert len(delivered) == 2
    # Ertangi dayjestda 1-partiya ham, 2-partiya ham bo'lishi kerak (hech biri yo'qolmagan)
    assert "v1.0.1" in delivered[1]
    assert "v1.0.2" in delivered[1]


def test_regression_topilma2_failed_yesterday_digest_retry_does_not_suppress_today_digest(tmp_path: Path, monkeypatch):
    """Topilma 2: Kechagi dayjest bugun retry bilan yetkazilsa last_digest_date == bugun va o'sha siklda bugungi yangi dayjest yuborilmaydi (F4)."""
    db_path = tmp_path / "test_topilma2_yesterday_retry.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-suppress", "vendor": "OpenAI", "product": "Python SDK"}
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    # 1. Kechagi kungi dayjest outboxda 'pending' holatida qolgan (masalan, delivery timeout bo'lgan)
    out_id = store.add_to_outbox(conn, "digest:2026-10-28", "digest", "Kechagi 2026-10-28 dayjest matni")
    store.set_app_state(conn, "last_digest_date", "2026-10-27")

    # 2. Bugungi yangi hodisa keldi
    ev_today = parser.FeedEntry(
        entry_id="v2.1.0",
        title="Release v2.1.0 (Patch)",
        link="https://example.com/v210",
        published_at="2026-10-29T08:00:00Z",
        summary="Today patch",
        raw_content="Today patch release notes.",
    )
    store.record_unseen_entries(conn, source, [ev_today])

    # 3. Bugungi kun (2026-10-29) uchun rejaviy sikl
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True, today_date="2026-10-29")

    # Faqat kechagi retry qilingan dayjest yetkaziladi; bugungi yangi dayjest o'sha siklda yuborilmaydi
    assert len(delivered) == 1
    assert "Kechagi 2026-10-28 dayjest matni" in delivered[0]

    # last_digest_date bugungi kunga teng bo'lishi kerak
    assert store.get_app_state(conn, "last_digest_date") == "2026-10-29"

    # Bugungi yangi hodisa processing_jobs'da saqlanib qolgan (yo'qolmagan)
    jobs = store.get_pending_processing_jobs(conn)
    assert any(j["entry_id"] == "v2.1.0" for j in jobs)


def test_regression_topilma3_retried_group_outbox_marks_member_jobs_done(tmp_path: Path, monkeypatch):
    """Topilma 3: Guruh outbox (digest:/merged_high:) retryda muvaffaqiyatli bo'lsa a'zo hodisalar processing_jobs'da done bo'ladi."""
    db_path = tmp_path / "test_group_done.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-grp", "vendor": "Anthropic", "product": "Claude"}
    ev1 = parser.FeedEntry(
        entry_id="grp-1",
        title="Anthropic Update 1",
        link="https://example.com/1",
        published_at="2026-10-28T01:00:00Z",
        summary="Update 1",
        raw_content="Content 1",
    )
    ev2 = parser.FeedEntry(
        entry_id="grp-2",
        title="Anthropic Update 2",
        link="https://example.com/2",
        published_at="2026-10-28T02:00:00Z",
        summary="Update 2",
        raw_content="Content 2",
    )
    store.record_unseen_entries(conn, source, [ev1, ev2])

    # Hodisalar processing_jobs'da pending
    jobs = store.get_pending_processing_jobs(conn)
    assert len(jobs) == 2

    # Guruh dayjesti outboxga kiritiladi va link qilinadi
    out_id = store.add_to_outbox(conn, "digest:2026-10-28", "digest", "Digest text with grp-1 and grp-2")
    store.link_outbox_events(conn, out_id, [{"id": "src-grp:grp-1"}, {"id": "src-grp:grp-2"}])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    # Sikl ishga tushadi va pending outbox retry qilinadi
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-28")
    assert len(delivered) == 1

    # outbox_events'dagi barcha a'zolar processing_jobs'da done bo'lishi shart
    remaining_jobs = store.get_pending_processing_jobs(conn)
    assert len(remaining_jobs) == 0


def test_review50_defect1_failed_delivery_does_not_mark_sent_or_done(tmp_path: Path, monkeypatch):
    """Defect 1: Delivery failure does not mark item sent or processing job done."""
    db_path = tmp_path / "defect1.db"
    conn = store.get_connection(db_path)
    entry = parser.FeedEntry(
        entry_id="cve-fail-1",
        title="Critical Security Vulnerability CVE-2026-9999",
        link="https://example.com/cve-1",
        published_at="2026-10-29T10:00:00Z",
        summary="Security flaw",
        raw_content="Critical RCE vulnerability fixed.",
    )
    src = {"id": "src-d1", "vendor": "Anthropic", "product": "Claude"}
    store.record_unseen_entries(conn, src, [entry])
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text))
    # Delivery fails
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (False, "Network timeout"))
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # Outbox status is failed
    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox WHERE event_id = 'src-d1:cve-fail-1'")
    row = cur.fetchone()
    assert row is not None and row[0] == "failed"

    # Processing job is still pending
    jobs = store.get_pending_processing_jobs(conn)
    assert len(jobs) == 1
    assert jobs[0]["event_id"] == "src-d1:cve-fail-1"


def test_review50_defect2_failed_digest_not_repackaged_with_later_items(tmp_path: Path, monkeypatch):
    """Defect 2: Failed digest is retried with its original items and does not repackage new items arriving later."""
    db_path = tmp_path / "defect2.db"
    conn = store.get_connection(db_path)
    src = {"id": "src-d2", "vendor": "Google", "product": "Gemini"}
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text))

    # Morning item arrives
    ev1 = parser.FeedEntry(entry_id="d2-morning", title="Morning v1", link="https://example.com/1", published_at="2026-10-29T08:00:00Z", summary="s1", raw_content="c1")
    store.record_unseen_entries(conn, src, [ev1])

    # Delivery fails for morning digest
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (False, "Telegram 500"))
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")

    # Afternoon item arrives
    ev2 = parser.FeedEntry(entry_id="d2-afternoon", title="Afternoon v2", link="https://example.com/2", published_at="2026-10-29T14:00:00Z", summary="s2", raw_content="c2")
    store.record_unseen_entries(conn, src, [ev2])

    # Second run on same day: delivery succeeds
    with store.get_connection(db_path) as c:
        c.execute("UPDATE outbox SET next_attempt_at = ''")
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")

    # The retried digest contains only Morning v1; Afternoon v2 was NOT repackaged into today's digest
    assert len(delivered) == 1
    assert "Morning v1" in delivered[0]
    assert "Afternoon v2" not in delivered[0]

    # Afternoon v2 remains pending for tomorrow
    pending_jobs = store.get_pending_processing_jobs(conn)
    assert any(j["event_id"] == "src-d2:d2-afternoon" for j in pending_jobs)


def test_review50_defect3_rev1_delivery_does_not_complete_rev2_job(tmp_path: Path):
    """Defect 3: Revision 1 delivery does not complete revision 2 processing job."""
    db_path = tmp_path / "defect3.db"
    conn = store.get_connection(db_path)
    now_iso = "2026-10-29T10:00:00Z"
    event_id = "test-d3:item-1"

    # Insert two processing jobs for rev 1 and rev 2
    conn.execute("INSERT INTO processing_jobs (event_id, revision, status, created_at, updated_at) VALUES (?, 1, 'pending', ?, ?)", (event_id, now_iso, now_iso))
    conn.execute("INSERT INTO processing_jobs (event_id, revision, status, created_at, updated_at) VALUES (?, 2, 'pending', ?, ?)", (event_id, now_iso, now_iso))
    conn.commit()

    # Deliver rev 1 and mark done for rev 1
    store.mark_processing_job_done(conn, event_id, revision=1)

    cur = conn.cursor()
    cur.execute("SELECT revision, status FROM processing_jobs WHERE event_id = ? ORDER BY revision ASC", (event_id,))
    rows = cur.fetchall()
    assert rows[0][0] == 1 and rows[0][1] == "completed"
    assert rows[1][0] == 2 and rows[1][1] == "pending"


def test_review50_defect4_atomic_digest_outbox_and_member_reservation(tmp_path: Path):
    """Defect 4: Digest row and member reservations are created atomically in the same transaction."""
    db_path = tmp_path / "defect4.db"
    conn = store.get_connection(db_path)
    ev = {"id": "src-d4:item-1", "revision": 1}

    out_id = store.add_digest_to_outbox(
        conn,
        key="digest:2026-10-29",
        rendered_text="Digest text",
        pages=[("Digest page 1", [(ev, {})])],
        member_events=[ev],
    )
    assert out_id > 0
    assert store.is_event_revision_reserved(conn, "src-d4:item-1", 1)

    # Unique index on (event_id, revision) prevents duplicate reservation
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (?, ?, ?)", (999, "src-d4:item-1", 1))


def test_review50_defect5_legacy_migration_table_creation_order(tmp_path: Path):
    """Defect 5: Legacy DB migration creates and upgrades all tables before running duplicate collapse."""
    db_path = tmp_path / "defect5.db"
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, priority TEXT, rendered_text TEXT, status TEXT, attempts INTEGER DEFAULT 0, created_at TEXT)")
    raw_conn.execute("INSERT INTO outbox (event_id, priority, rendered_text, status, created_at) VALUES ('openai-news:foo', 'high', 'msg1', 'sent', '2026-10-29T08:00:00')")
    raw_conn.commit()
    raw_conn.close()

    migrated_conn = store.get_connection(db_path)
    cur = migrated_conn.cursor()
    cur.execute("PRAGMA table_info(outbox)")
    cols = {r[1] for r in cur.fetchall()}
    assert "key" in cols
    assert "revision" in cols

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cur.fetchall()}
    assert "outbox_pages" in tables
    assert "outbox_deliveries" in tables


def test_review50_defect6_duplicate_collapse_preserves_merged_deliveries(tmp_path: Path):
    """Defect 6: Duplicate collapse merges recipient delivery records and never deletes a sent record."""
    db_path = tmp_path / "defect6.db"
    conn = store.get_connection(db_path)
    eid = "openai-news:test-collapse"

    cur = conn.cursor()
    cur.execute("INSERT INTO outbox (event_id, priority, kind, rendered_text, status, created_at) VALUES (?, 'high', 'high', 'Msg 1', 'sent', '2026-10-29T08:00:00')", (eid,))
    id1 = cur.lastrowid
    cur.execute("INSERT INTO outbox (event_id, priority, kind, rendered_text, status, created_at) VALUES (?, 'high', 'high', 'Msg 2', 'sent', '2026-10-29T09:00:00')", (eid,))
    id2 = cur.lastrowid

    conn.execute("INSERT INTO outbox_deliveries (outbox_id, chat_id, status) VALUES (?, 'chat-1', 'sent')", (id1,))
    conn.execute("INSERT INTO outbox_deliveries (outbox_id, chat_id, status) VALUES (?, 'chat-2', 'sent')", (id2,))
    conn.commit()

    store.collapse_outbox_duplicates(conn)

    cur.execute("SELECT chat_id, status FROM outbox_deliveries WHERE outbox_id = ? ORDER BY chat_id ASC", (id1,))
    delivs = cur.fetchall()
    assert len(delivs) == 2
    assert delivs[0][0] == "chat-1" and delivs[0][1] == "sent"
    assert delivs[1][0] == "chat-2" and delivs[1][1] == "sent"


def test_review50_defect7_enrichment_retry_bounded_per_revision(tmp_path: Path):
    """Defect 7: Enrichment retry attempts are persisted per (event_id, revision) and capped at 3."""
    db_path = tmp_path / "defect7.db"
    conn = store.get_connection(db_path)
    eid = "src-d7:item-1"

    a1 = store.increment_enrichment_attempts(conn, eid, revision=1)
    a2 = store.increment_enrichment_attempts(conn, eid, revision=1)
    a3 = store.increment_enrichment_attempts(conn, eid, revision=1)
    assert a1 == 1 and a2 == 2 and a3 == 3

    b1 = store.increment_enrichment_attempts(conn, eid, revision=2)
    assert b1 == 1


def test_review50_defect8_triage_security_word_boundaries_no_workforce():
    """Defect 8: Security keywords use word boundaries; 'workforce' or 'source' do not trigger security."""
    ev_workforce = {
        "title": "Optimizing workforce productivity with AI",
        "raw_content": "We are expanding our open source workforce to build tools.",
        "product": "Anthropic Enterprise",
    }
    t_type, prio = triage.classify_event(ev_workforce)
    assert t_type != "security"
    assert prio != "high"

    ev_real_rce = {
        "title": "Advisory: Critical RCE in CLI parser",
        "raw_content": "Arbitrary code execution flaw discovered.",
        "product": "Claude Code",
    }
    t_type, prio = triage.classify_event(ev_real_rce)
    assert t_type == "security"
    assert prio == "high"


def test_review50_defect9_sdk_added_support_is_digest_not_new_model():
    """Defect 9: 'Added support for model X' in SDK release notes is digest, not new_model/high."""
    ev_sdk = {
        "title": "Python SDK v1.50.0",
        "raw_content": "Added support for GPT-4o and updated dependencies.",
        "product": "OpenAI Python SDK",
    }
    t_type, prio = triage.classify_event(ev_sdk)
    assert t_type != "new_model"
    assert prio != "high"

    ev_actual_model = {
        "title": "Introducing GPT-4o: Our new flagship model",
        "raw_content": "GPT-4o is now generally available with multimodal reasoning.",
        "product": "OpenAI Models",
    }
    t_type, prio = triage.classify_event(ev_actual_model)
    assert t_type == "new_model"
    assert prio == "high"


def test_review50_defect10_customer_story_cap_before_any_high_return():
    """Defect 10: Customer story / case study cap applies before any high priority classification."""
    ev_story = {
        "title": "Customer Story: How Retell AI prevented critical security vulnerability",
        "raw_content": "Case study on how Retell AI uses GPT-4o for security automation.",
        "product": "OpenAI News",
    }
    t_type, prio = triage.classify_event(ev_story)
    assert prio == "digest"
    assert prio != "high"


def test_review50_defect11_overflow_high_items_go_to_digest_no_merged_high(tmp_path: Path, monkeypatch):
    """Defect 11: More than 5 high items in a day: extra items go to digest; no merged_high is created."""
    db_path = tmp_path / "defect11.db"
    conn = store.get_connection(db_path)
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text))

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    entries = []
    for i in range(7):
        entries.append(parser.FeedEntry(
            entry_id=f"cve-{i}",
            title=f"Security Advisory CVE-2026-100{i}",
            link=f"https://example.com/cve/{i}",
            published_at="2026-10-29T08:00:00Z",
            summary=f"Security fix {i}",
            raw_content=f"Critical RCE vulnerability #{i}.",
        ))
    store.record_unseen_entries(conn, {"id": "src-d11", "vendor": "OpenAI", "product": "API"}, entries)

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date="2026-10-29")

    # Exactly 5 sent as immediate high priority
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM outbox WHERE priority = 'high' AND status = 'sent'")
    high_sent_count = cur.fetchone()[0]
    assert high_sent_count == 5

    # NO merged_high outbox row created!
    cur.execute("SELECT COUNT(*) FROM outbox WHERE key LIKE '%merged_high%' OR event_id LIKE '%merged_high%'")
    assert cur.fetchone()[0] == 0


def test_review50_defect12_startup_sending_marked_ambiguous_and_rendered_in_digest(tmp_path: Path, monkeypatch):
    """Defect 12: Startup recovery marks rows left in 'sending' as 'ambiguous'; next digest renders the count."""
    db_path = tmp_path / "defect12.db"
    conn = store.get_connection(db_path)
    now_iso = "2026-10-29T08:00:00Z"

    conn.execute("INSERT INTO outbox (key, event_id, priority, kind, rendered_text, status, created_at) VALUES ('high:stuck:1', 'stuck', 'high', 'high', 'Stuck msg', 'sending', ?)", (now_iso,))
    conn.commit()

    ev = parser.FeedEntry(entry_id="d12-e1", title="Normal SDK Update", link="https://example.com/d12", published_at=now_iso, summary="s", raw_content="c")
    store.record_unseen_entries(conn, {"id": "src-d12", "vendor": "Anthropic", "product": "SDK"}, [ev])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text))
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")

    # Stuck item is marked 'ambiguous' and NOT re-sent
    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox WHERE key = 'high:stuck:1'")
    assert cur.fetchone()[0] == "ambiguous"

    # Delivered digest must contain the ambiguous warning
    assert len(delivered) == 1
    assert "1 ta yetkazib berish holati noaniq (ambiguous)" in delivered[0]


def test_crash_after_send_before_status_no_resend(tmp_path: Path, monkeypatch):
    """Crash after send before status: outbox left in 'sending' is marked 'ambiguous' on restart and never re-sent."""
    db_path = tmp_path / "crash_resend.db"
    conn = store.get_connection(db_path)
    now_iso = "2026-10-29T09:00:00Z"

    conn.execute("INSERT INTO outbox (key, event_id, priority, kind, rendered_text, status, created_at) VALUES ('high:crash:1', 'crash', 'high', 'high', 'Crash alert', 'sending', ?)", (now_iso,))
    store.set_app_state(conn, "last_digest_date", "2026-10-29")
    conn.commit()

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date="2026-10-29")

    assert len(delivered) == 0

    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox WHERE key = 'high:crash:1'")
    assert cur.fetchone()[0] == "ambiguous"


def test_migration_on_copy_of_real_vendor_watch_db(tmp_path: Path):
    """Migration works on a copy of real data/vendor_watch.db: collapses retell-ai duplicates, keeps row 2, merges deliveries, preserves row 10."""
    real_db = Path(__file__).resolve().parents[1] / "data" / "vendor_watch.db"
    if not real_db.is_file():
        pytest.skip("data/vendor_watch.db not found")

    copy_path = tmp_path / "vendor_watch_copy.db"
    shutil.copy2(real_db, copy_path)

    conn = store.get_connection(copy_path)
    cur = conn.cursor()

    cur.execute("SELECT id, status, revision FROM outbox WHERE event_id = 'openai-news:https://openai.com/index/retell-ai'")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 2
    assert rows[0][1] == "sent"

    cur.execute("SELECT status FROM outbox_deliveries WHERE outbox_id = 2 AND chat_id = '460927214'")
    deliv = cur.fetchone()
    assert deliv is not None and deliv[0] == "sent"

    cur.execute("SELECT id, status FROM outbox WHERE id = 10")
    r10 = cur.fetchone()
    assert r10 is not None and r10[1] == "sent"

    cur.execute("PRAGMA index_info(idx_outbox_key)")
    assert len(cur.fetchall()) > 0


def test_review50_topilma1_legacy_processing_jobs_unique_migration(tmp_path: Path):
    """Topilma 1: Legacy DB with event_id TEXT UNIQUE on processing_jobs accepts new revisions (rev >= 2)."""
    db_path = tmp_path / "topilma1.db"
    raw = sqlite3.connect(str(db_path))
    raw.execute("""
        CREATE TABLE processing_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    raw.execute("""
        CREATE TABLE events (
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
        )
    """)
    raw.execute("""
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            priority TEXT,
            rendered_text TEXT,
            status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    raw.commit()
    raw.close()

    conn = store.get_connection(db_path)
    source = {"id": "src-t1", "vendor": "OpenAI", "product": "News"}
    ev_rev1 = parser.FeedEntry(
        entry_id="item-t1",
        title="Title Rev 1",
        link="https://openai.com/item-t1",
        published_at="2026-10-29T08:00:00Z",
        summary="Summary 1",
        raw_content="Content 1",
    )
    store.record_unseen_entries(conn, source, [ev_rev1])

    # Now rev 2 arrives with changed content
    ev_rev2 = parser.FeedEntry(
        entry_id="item-t1",
        title="Title Rev 2",
        link="https://openai.com/item-t1",
        published_at="2026-10-29T10:00:00Z",
        summary="Summary 2",
        raw_content="Content 2 updated",
    )
    store.record_unseen_entries(conn, source, [ev_rev2])

    cur = conn.cursor()
    cur.execute("SELECT revision, status FROM processing_jobs WHERE event_id = 'src-t1:item-t1' ORDER BY revision ASC")
    rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == 1 and rows[1][0] == 2


def test_review50_topilma2_ambiguous_high_outbox_not_resent(tmp_path: Path, monkeypatch):
    """Topilma 2: High priority item marked 'ambiguous' is not re-sent on next cycle."""
    db_path = tmp_path / "topilma2.db"
    conn = store.get_connection(db_path)
    now_iso = "2026-10-29T08:00:00Z"

    entry = parser.FeedEntry(
        entry_id="cve-ambig-1",
        title="Critical Security Advisory CVE-2026-7777",
        link="https://example.com/cve-ambig",
        published_at=now_iso,
        summary="RCE flaw",
        raw_content="Critical RCE vulnerability fix.",
    )
    source = {"id": "src-t2", "vendor": "Anthropic", "product": "Claude"}
    store.record_unseen_entries(conn, source, [entry])

    eid = "src-t2:cve-ambig-1"
    out_id = store.add_to_outbox(conn, eid, "high", "Alert msg", revision=1)
    # Simulate crash while sending
    store.update_outbox_status(conn, out_id, "sending")

    # Restart and run cycle
    store.set_app_state(conn, "last_digest_date", "2026-10-29")
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text))
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date="2026-10-29")

    # High item must NOT be delivered
    assert not any("Alert msg" in d for d in delivered)
    assert len(delivered) == 0

    cur = conn.cursor()
    cur.execute("SELECT status FROM outbox WHERE id = ?", (out_id,))
    assert cur.fetchone()[0] == "ambiguous"


def test_review50_topilma3_mark_outbox_done_colon_event_id(tmp_path: Path):
    """Topilma 3: _mark_outbox_done parses high:<event_id>:<rev> properly when event_id contains colons."""
    db_path = tmp_path / "topilma3.db"
    conn = store.get_connection(db_path)
    now_iso = "2026-10-29T08:00:00Z"
    mid = "openai-news:https://openai.com/index/retell-ai"

    conn.execute("INSERT INTO processing_jobs (event_id, revision, status, created_at, updated_at) VALUES (?, 1, 'pending', ?, ?)", (mid, now_iso, now_iso))
    conn.commit()

    key = f"high:{mid}:1"
    runner._mark_outbox_done(conn, 999999, key)

    cur = conn.cursor()
    cur.execute("SELECT status FROM processing_jobs WHERE event_id = ?", (mid,))
    row = cur.fetchone()
    assert row is not None and row[0] == "completed"


def test_review50_topilma4_digest_sent_when_only_ambiguous_exists(tmp_path: Path, monkeypatch):
    """Topilma 4: Daily digest is generated to report ambiguous deliveries even when no new items arrived."""
    db_path = tmp_path / "topilma4.db"
    conn = store.get_connection(db_path)
    now_iso = "2026-10-29T08:00:00Z"

    conn.execute("INSERT INTO outbox (key, event_id, priority, kind, rendered_text, status, created_at) VALUES ('high:stuck:1', 'stuck', 'high', 'high', 'Stuck msg', 'ambiguous', ?)", (now_iso,))
    conn.commit()

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True, today_date="2026-10-29")

    assert len(delivered) == 1
    assert "1 ta yetkazib berish holati noaniq (ambiguous)" in delivered[0]


def test_review50_topilma5_legacy_and_new_duplicate_collapse(tmp_path: Path):
    """Topilma 5: collapse_outbox_duplicates merges legacy unkeyed row with new keyed row without UNIQUE constraint error."""
    db_path = tmp_path / "topilma5.db"
    conn = store.get_connection(db_path)

    conn.execute("DROP INDEX IF EXISTS idx_outbox_key")
    eid = "openai-news:https://example.com/item-t5"
    target_k = f"high:{eid}:1"

    # Legacy row without key
    conn.execute("INSERT INTO outbox (key, event_id, priority, kind, rendered_text, status, created_at) VALUES (NULL, ?, 'high', 'high', 'Legacy text', 'sent', '2026-10-29T08:00:00')", (eid,))
    # New row with key
    conn.execute("INSERT INTO outbox (key, event_id, priority, kind, rendered_text, status, created_at) VALUES (?, ?, 'high', 'high', 'New text', 'sent', '2026-10-29T09:00:00')", (target_k, eid))
    conn.commit()

    # Must collapse without IntegrityError on idx_outbox_key
    store.collapse_outbox_duplicates(conn)

    cur = conn.cursor()
    cur.execute("SELECT id, key, status FROM outbox WHERE event_id = ?", (eid,))
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][1] == target_k


def test_review50_topilma6_multipage_digest_page1_sent_page2_failed(tmp_path: Path, monkeypatch):
    """Topilma 6: In multipage digest, page 1 success events remain 'sent' and are not overwritten to 'failed' when page 2 fails."""
    db_path = tmp_path / "topilma6.db"
    conn = store.get_connection(db_path)

    # Ingest enough items to span at least 2 digest pages
    for i in range(20):
        ev = parser.FeedEntry(
            entry_id=f"entry-t6-{i}",
            title=f"Release v{i}.0.0 with multiple long notes and feature descriptions",
            link=f"https://example.com/entry/t6/{i}",
            published_at="2026-10-29T01:00:00Z",
            summary="Long summary text " * 30,
            raw_content="Detailed content " * 40,
        )
        store.record_unseen_entries(conn, {"id": f"src-t6-{i}", "vendor": f"Vendor {i}", "product": "SDK"}, [ev])

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Delivery mock: page 1 succeeds, page 2 fails
    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        if page_number == 2:
            return False, "Page 2 network timeout"
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)

    runner.run_cycle(db_path=db_path, dry_run=False, fixtures=True, digest_mode=True, once=True, today_date="2026-10-29")

    cur = conn.cursor()
    cur.execute("SELECT page_number, status FROM outbox_pages ORDER BY page_number ASC")
    pages = cur.fetchall()
    assert len(pages) >= 2
    assert pages[0][1] == "sent"
    assert pages[1][1] == "failed"

    # Notification records for page 1 members MUST be 'sent', not overwritten with 'failed'
    cur.execute("SELECT member_ids_json FROM outbox_pages WHERE page_number = 1")
    p1_mids = json.loads(cur.fetchone()[0])
    assert len(p1_mids) > 0

    for mid in p1_mids:
        cur.execute("SELECT status FROM event_notifications WHERE event_id = ?", (mid,))
        row = cur.fetchone()
        assert row is not None and row[0] == "sent", f"Event {mid} should be 'sent', got {row[0] if row else None}"


def test_review50_topilma7_count_today_high_priority_custom_date(tmp_path: Path, monkeypatch):
    """Topilma 7: runner.run_cycle passes today_date to count_today_high_priority."""
    db_path = tmp_path / "topilma7.db"
    conn = store.get_connection(db_path)
    past_date = "2026-10-28"

    # Insert 5 sent high priority items for past_date
    for i in range(5):
        key = f"high:src-t7:cve-{i}:1"
        conn.execute(
            "INSERT INTO outbox (key, event_id, priority, kind, rendered_text, status, created_at) VALUES (?, ?, 'high', 'high', 'Alert', 'sent', ?)",
            (key, f"src-t7:cve-{i}", f"{past_date}T10:00:00"),
        )
    conn.commit()

    # Ingest 1 new high priority item
    entry = parser.FeedEntry(
        entry_id="cve-6th",
        title="Critical Security Flaw 6",
        link="https://example.com/cve-6",
        published_at=f"{past_date}T12:00:00Z",
        summary="RCE flaw",
        raw_content="Critical RCE vulnerability fix.",
    )
    store.record_unseen_entries(conn, {"id": "src-t7", "vendor": "Anthropic", "product": "Claude"}, [entry])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text))

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    # Run cycle with past_date: the 5 sent items for past_date must count, routing 6th item to digest
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, today_date=past_date)

    # 6th item should NOT be sent as immediate high priority (only in digest)
    assert not any("🔔" in d and "Critical Security Flaw 6" in d for d in delivered)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM outbox WHERE priority = 'high' AND status = 'sent' AND (created_at >= ? OR key LIKE ?)", (past_date, f"%:{past_date}%"))
    assert cur.fetchone()[0] == 5
    cur.execute("SELECT COUNT(*) FROM outbox WHERE event_id = 'src-t7:cve-6th' AND priority = 'high'")
    assert cur.fetchone()[0] == 0
    # Linked to digest outbox
    cur.execute("SELECT o.key FROM outbox o JOIN outbox_events oe ON o.id = oe.outbox_id WHERE oe.event_id = 'src-t7:cve-6th'")
    row = cur.fetchone()
    assert row is not None and row[0] == f"digest:{past_date}"


def test_review61_defect1_enrichment_exhaustion_downgrade_persisted_no_4th_vertex_call(tmp_path: Path, monkeypatch):
    """Review 61 Defect 1: Enrichment-exhaustion downgrade must be persisted in DB (no 4th Vertex call on subsequent cycles)."""
    from datetime import datetime
    db_path = tmp_path / "defect1.db"
    conn = store.get_connection(db_path)
    today_str = datetime.now().date().isoformat()
    store.set_app_state(conn, "last_digest_date", today_str)

    # Ingest a high-priority event (e.g. security advisory / breaking change)
    source = {"id": "src-d1", "vendor": "Anthropic", "product": "Claude"}
    entry = parser.FeedEntry(
        entry_id="d1-security-cve",
        title="Security Advisory: Critical vulnerability in Claude CLI",
        link="https://anthropic.com/security/cve-2026-001",
        published_at="2026-10-29T00:00:00Z",
        summary="Critical CVE remote code execution flaw",
        raw_content="Critical security advisory. Immediate action required.",
    )
    store.record_unseen_entries(conn, source, [entry])

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Enricher mock that always fails (returns raw mode)
    call_count = 0
    def failing_enricher(ev, profile_text="", use_stub=False):
        nonlocal call_count
        call_count += 1
        return {
            "uz_title": ev.get("title", ""),
            "uz_summary": ev.get("raw_content", ""),
            "uz_benefit": "",
            "uz_how_to_use": "",
            "possible_overlap_with_custom_tool": None,
            "is_raw_mode": True,
        }

    monkeypatch.setattr(enricher, "enrich_event", failing_enricher)
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None, page_number=None: (True, ""))

    # Cycle 1: attempt 1 fails -> stays high, pending for retry
    runner.run_cycle(db_path=db_path, dry_run=False, fixtures=False, once=True)
    assert call_count == 1
    assert store.get_enrichment_attempts(conn, "src-d1:d1-security-cve", 1) == 1

    # Cycle 2: attempt 2 fails -> stays high, pending for retry
    runner.run_cycle(db_path=db_path, dry_run=False, fixtures=False, once=True)
    assert call_count == 2
    assert store.get_enrichment_attempts(conn, "src-d1:d1-security-cve", 1) == 2

    # Cycle 3: attempt 3 fails -> downgraded to digest
    runner.run_cycle(db_path=db_path, dry_run=False, fixtures=False, once=True)
    assert call_count == 3
    assert store.get_enrichment_attempts(conn, "src-d1:d1-security-cve", 1) == 3

    # Verify priority in DB is persisted as 'digest'
    cur = conn.cursor()
    cur.execute("SELECT priority FROM events WHERE id = 'src-d1:d1-security-cve'")
    row = cur.fetchone()
    assert row is not None and row[0] == "digest", f"Expected priority 'digest' in events, got {row}"

    # Cycle 4: subsequent cycle must NOT call enricher a 4th time (no 4th Vertex call)
    delivered_digest = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None, page_number=None: (delivered_digest.append(text) or True, ""))
    runner.run_cycle(db_path=db_path, dry_run=False, fixtures=False, digest_mode=True, once=True)
    assert call_count == 3, f"Expected 3 enricher calls, but got {call_count} (4th Vertex call occurred!)"
    assert any("[Boyitilmagan]" in d for d in delivered_digest), "Expected item to route to digest marked [Boyitilmagan]"


def test_review61_defect2_last_digest_date_set_only_after_all_pages_delivered(tmp_path: Path, monkeypatch):
    """Review 61 Defect 2: last_digest_date set only after every page of the digest was delivered."""
    from datetime import datetime
    db_path = tmp_path / "defect2.db"
    conn = store.get_connection(db_path)
    today_str = datetime.now().date().isoformat()

    # Ingest an item for digest
    source = {"id": "src-d2", "vendor": "Google", "product": "Gemini"}
    entry = parser.FeedEntry(
        entry_id="gemini-update",
        title="Gemini 2.5 Flash minor improvements",
        link="https://google.com/gemini-2-5",
        published_at=f"{today_str}T10:00:00Z",
        summary="Minor model updates",
        raw_content="General improvements and bug fixes.",
    )
    store.record_unseen_entries(conn, source, [entry])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    # Initial state: last_digest_date is empty
    assert store.get_app_state(conn, "last_digest_date", "") == ""

    # Attempt delivery with failure (e.g. network timeout)
    def failing_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        return False, "Connection reset by peer"

    monkeypatch.setattr(delivery, "deliver", failing_deliver)

    # Run cycle with digest_mode=True
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)

    # Delivery failed: last_digest_date MUST NOT be set!
    assert store.get_app_state(conn, "last_digest_date", "") == "", "last_digest_date should not be set when delivery fails"

    # Now simulate delivery success
    delivered_pages = []
    def successful_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        delivered_pages.append((outbox_id, page_number))
        return True, ""

    monkeypatch.setattr(delivery, "deliver", successful_deliver)

    # Next cycle retries the digest
    with store.get_connection(db_path) as c:
        c.execute("UPDATE outbox SET next_attempt_at = ''")
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)

    # Now that all pages were delivered, last_digest_date MUST be set to today
    assert len(delivered_pages) > 0
    assert store.get_app_state(conn, "last_digest_date", "") == today_str


def test_review61_defect3_per_page_delivery_records_page2_not_skipped(tmp_path: Path, monkeypatch):
    """Review 61 Defect 3: Per-page delivery records (outbox_deliveries includes page_number) so page 2+ is never skipped."""
    db_path = tmp_path / "defect3.db"
    conn = store.get_connection(db_path)

    # Ingest enough items to span at least 2 digest pages
    for i in range(25):
        entry = parser.FeedEntry(
            entry_id=f"page-test-{i}",
            title=f"Release note item #{i} with long description for paging",
            link=f"https://example.com/notes/{i}",
            published_at="2026-10-29T00:00:00Z",
            summary="Extensive feature summary text " * 20,
            raw_content="Detailed changelog content " * 30,
        )
        store.record_unseen_entries(conn, {"id": f"src-p-{i}", "vendor": "VendorX", "product": "ToolY"}, [entry])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    # Mock Telegram delivery to record every dispatch: (page_number, chat_id)
    sent_dispatches = []
    monkeypatch.setenv("BOT_TOKEN", "mock_bot_token")
    monkeypatch.setattr(
        delivery,
        "get_target_chat_ids",
        lambda: ["chat_alpha", "chat_beta"],
    )
    def mock_send(token, chat_id, text):
        sent_dispatches.append((chat_id, text[:30]))

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send)

    # Run cycle to deliver multipage digest
    runner.run_cycle(db_path=db_path, dry_run=False, fixtures=True, digest_mode=True, once=True)

    # Assert outbox_pages has >= 2 pages and all pages are sent
    cur = conn.cursor()
    cur.execute("SELECT page_number, status FROM outbox_pages ORDER BY page_number ASC")
    pages = cur.fetchall()
    assert len(pages) >= 2, f"Expected at least 2 pages, got {len(pages)}"
    for p_num, p_status in pages:
        assert p_status == "sent", f"Page {p_num} status was {p_status}, expected 'sent'"

    # Check outbox_deliveries records per page
    cur.execute("SELECT outbox_id, page_number, chat_id, status FROM outbox_deliveries ORDER BY page_number, chat_id")
    deliv_records = cur.fetchall()
    p1_records = [r for r in deliv_records if r["page_number"] == 1]
    p2_records = [r for r in deliv_records if r["page_number"] == 2]

    assert len(p1_records) == 2, f"Expected 2 delivery records for page 1, got {len(p1_records)}"
    assert len(p2_records) == 2, f"Expected 2 delivery records for page 2, got {len(p2_records)}"
    for r in deliv_records:
        assert r["status"] == "sent"

    # Both page 1 and page 2 must have dispatches sent to both chats (total >= 4 dispatches)
    assert len(sent_dispatches) >= 4, f"Page 2 was skipped! Total dispatches: {len(sent_dispatches)}"


def test_review61_defect4_cluster_member_ids_linked_and_completed_in_digest_and_outbox(tmp_path: Path, monkeypatch):
    """Review 61 Defect 4: Cluster member IDs linked in outbox_events and completed in processing_jobs."""
    db_path = tmp_path / "defect4.db"
    conn = store.get_connection(db_path)

    # Ingest 2 entries from same vendor/product within 24h that cluster into 1 item
    source = {"id": "src-d4", "vendor": "Anthropic", "product": "Claude"}
    entry_a = parser.FeedEntry(
        entry_id="claude-3-5-sonnet-announcement",
        title="Claude 3.5 Sonnet Announcement",
        link="https://anthropic.com/news/claude-3-5-sonnet",
        published_at="2026-10-29T08:00:00Z",
        summary="Introducing Claude 3.5 Sonnet",
        raw_content="Anthropic introduces Claude 3.5 Sonnet model with enhanced coding capabilities.",
    )
    entry_b = parser.FeedEntry(
        entry_id="claude-3-5-sonnet-details",
        title="Claude 3.5 Sonnet Announcement - Details",
        link="https://anthropic.com/news/claude-3-5-sonnet-details",
        published_at="2026-10-29T09:00:00Z",
        summary="Further details on Claude 3.5 Sonnet release",
        raw_content="Anthropic introduces Claude 3.5 Sonnet model benchmarks and architecture.",
    )
    store.record_unseen_entries(conn, source, [entry_a, entry_b])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None, page_number=None: (True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)

    # Verify both cluster members are in outbox_events
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT event_id FROM outbox_events")
    linked_event_ids = {r[0] for r in cur.fetchall()}
    assert "src-d4:claude-3-5-sonnet-announcement" in linked_event_ids
    assert "src-d4:claude-3-5-sonnet-details" in linked_event_ids

    # Verify BOTH processing_jobs are completed
    cur.execute(
        "SELECT event_id, status FROM processing_jobs WHERE event_id IN ('src-d4:claude-3-5-sonnet-announcement', 'src-d4:claude-3-5-sonnet-details')"
    )
    jobs = dict(cur.fetchall())
    assert jobs.get("src-d4:claude-3-5-sonnet-announcement") == "completed"
    assert jobs.get("src-d4:claude-3-5-sonnet-details") == "completed"


def test_review61_defect5_event_status_updated_per_event_id_and_revision_only(tmp_path: Path):
    """Review 61 Defect 5: Event status updated per (event_id, revision) only (not overwriting new revisions with old revision status)."""
    db_path = tmp_path / "defect5.db"
    conn = store.get_connection(db_path)

    event_id = "src-d5:entry-rev-test"
    # Ingest revision 1
    conn.execute(
        """
        INSERT INTO events (id, entry_id, title, revision, status, first_seen_at)
        VALUES (?, 'entry-rev-test', 'Title Rev 1', 1, 'pending', '2026-10-29T00:00:00')
        """,
        (event_id,),
    )
    store.mark_event_notified(conn, event_id, status="pending", revision=1)

    # Now a new revision 2 arrives
    conn.execute(
        """
        UPDATE events
        SET revision = 2, status = 'pending', title = 'Title Rev 2'
        WHERE id = ?
        """,
        (event_id,),
    )
    store.mark_event_notified(conn, event_id, status="pending", revision=2)

    assert store.is_event_already_sent(conn, event_id, revision=1) is False
    assert store.is_event_already_sent(conn, event_id, revision=2) is False

    # Simulate late delivery of revision 1 (e.g. outbox flush or delayed notification of rev 1)
    store.mark_event_notified(conn, event_id, status="sent", revision=1)

    # Revision 1 should be marked 'sent'
    assert store.is_event_already_sent(conn, event_id, revision=1) is True

    # Revision 2 must REMAIN pending / not sent (not overwritten by rev 1)
    assert store.is_event_already_sent(conn, event_id, revision=2) is False

    cur = conn.cursor()
    cur.execute("SELECT status FROM event_notifications WHERE event_id = ? AND revision = 2", (event_id,))
    row = cur.fetchone()
    assert row is not None and row[0] == "pending", f"Rev 2 status was {row[0] if row else None}, expected 'pending'"

    # Also events table (which is on revision 2) must not have been set to 'sent'
    cur.execute("SELECT revision, status FROM events WHERE id = ?", (event_id,))
    ev_row = cur.fetchone()
    assert ev_row[0] == 2
    assert ev_row[1] == "pending", f"events table status was {ev_row[1]}, expected 'pending'"


def test_review61_defect6_legacy_collapse_preserves_membership_and_delivery_records(tmp_path: Path):
    """Review 61 Defect 6: Legacy collapse must not lose membership (outbox_events) or delivery (outbox_deliveries) records."""
    real_db = Path("data/vendor_watch.db")
    if not real_db.exists():
        pytest.skip("data/vendor_watch.db not present")

    copy_path = tmp_path / "copy_vendor_watch.db"
    shutil.copy2(real_db, copy_path)

    # Connect with raw sqlite3 first to count original records
    raw_conn = sqlite3.connect(copy_path)
    raw_conn.row_factory = sqlite3.Row
    orig_deliv_cnt = raw_conn.execute("SELECT count(*) FROM outbox_deliveries").fetchone()[0]
    orig_events_cnt = raw_conn.execute("SELECT count(*) FROM outbox_events").fetchone()[0]
    raw_conn.close()

    # Now open with store.get_connection which runs schema migration and collapse_outbox_duplicates
    conn = store.get_connection(copy_path)

    # Assert that NO records were lost in outbox_deliveries or outbox_events
    cur = conn.cursor()
    new_deliv_cnt = cur.execute("SELECT count(*) FROM outbox_deliveries").fetchone()[0]
    new_events_cnt = cur.execute("SELECT count(*) FROM outbox_events").fetchone()[0]

    assert new_deliv_cnt == orig_deliv_cnt, f"Delivery records lost! Expected {orig_deliv_cnt}, got {new_deliv_cnt}"
    assert new_events_cnt == orig_events_cnt, f"Outbox events lost! Expected {orig_events_cnt}, got {new_events_cnt}"
    conn.close()


def test_review_overnight_defect1_collapse_preserves_membership_under_old_and_current_schema_and_reopen_real_db(tmp_path: Path):
    """Defect 1: collapse_outbox_duplicates preserves every member link transactionally under both old and current schema, and on reopening real-shape DB."""
    # Part A: Current schema with existing global idx_outbox_events_member
    db_a = tmp_path / "defect1_cur.db"
    conn_a = store.get_connection(db_a)
    conn_a.execute("DROP INDEX IF EXISTS idx_outbox_key")

    conn_a.execute(
        "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
        "VALUES (101, NULL, 'event-cur-dup', 'high', 'high', 'Text 1', 'sent', '2026-10-29T10:00:00')"
    )
    conn_a.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (101, 'member_cur_1', 1)")

    conn_a.execute(
        "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
        "VALUES (102, NULL, 'event-cur-dup', 'high', 'high', 'Text 1', 'pending', '2026-10-29T11:00:00')"
    )
    conn_a.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (102, 'member_cur_2', 1)")
    conn_a.commit()

    store.collapse_outbox_duplicates(conn_a)
    cur_a = conn_a.cursor()
    cur_a.execute("SELECT event_id FROM outbox_events WHERE outbox_id = 101 ORDER BY event_id")
    members_a = [r[0] for r in cur_a.fetchall()]
    assert members_a == ["member_cur_1", "member_cur_2"], f"Members lost in current schema collapse: {members_a}"
    conn_a.close()

    # Part B: Old schema without idx_outbox_events_member and overlapping members
    db_b = tmp_path / "defect1_old.db"
    conn_b = sqlite3.connect(db_b)
    conn_b.row_factory = sqlite3.Row
    conn_b.execute(
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY, key TEXT, event_id TEXT, priority TEXT, kind TEXT, "
        "rendered_text TEXT, status TEXT, attempts INTEGER DEFAULT 0, created_at TEXT, revision INTEGER DEFAULT 1)"
    )
    conn_b.execute(
        "CREATE TABLE outbox_events (outbox_id INTEGER, event_id TEXT, revision INTEGER, PRIMARY KEY (outbox_id, event_id, revision))"
    )
    conn_b.execute(
        "CREATE TABLE outbox_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, outbox_id INTEGER, page_number INTEGER DEFAULT 1, chat_id TEXT, status TEXT)"
    )
    conn_b.execute(
        "CREATE TABLE outbox_pages (outbox_id INTEGER, page_number INTEGER, rendered_text TEXT, status TEXT, PRIMARY KEY (outbox_id, page_number))"
    )

    conn_b.execute(
        "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
        "VALUES (201, NULL, 'event-old-dup', 'high', 'high', 'Text Old', 'sent', '2026-10-29T10:00:00')"
    )
    conn_b.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (201, 'member_common', 1)")
    conn_b.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (201, 'member_only_1', 1)")

    conn_b.execute(
        "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
        "VALUES (202, NULL, 'event-old-dup', 'high', 'high', 'Text Old', 'pending', '2026-10-29T11:00:00')"
    )
    conn_b.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (202, 'member_common', 1)")
    conn_b.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (202, 'member_only_2', 1)")
    conn_b.commit()

    store.collapse_outbox_duplicates(conn_b)
    cur_b = conn_b.cursor()
    cur_b.execute("SELECT event_id FROM outbox_events WHERE outbox_id = 201 ORDER BY event_id")
    members_b = [r[0] for r in cur_b.fetchall()]
    assert members_b == ["member_common", "member_only_1", "member_only_2"]
    # Verify unique index creation succeeds after duplicate resolution
    conn_b.execute("CREATE UNIQUE INDEX idx_outbox_events_member ON outbox_events(event_id, revision)")
    conn_b.close()

    # Part C: Real-shape DB copy reopen
    real_db = Path("data/vendor_watch.db")
    if real_db.exists():
        copy_path = tmp_path / "real_shape_copy.db"
        shutil.copy2(real_db, copy_path)

        # First open migrates the DB
        conn_c1 = store.get_connection(copy_path)
        c1_out = conn_c1.execute("SELECT count(*) FROM outbox").fetchone()[0]
        c1_oe = conn_c1.execute("SELECT count(*) FROM outbox_events").fetchone()[0]
        conn_c1.close()

        # Reopen an already-migrated real-shape DB
        conn_c2 = store.get_connection(copy_path)
        c2_out = conn_c2.execute("SELECT count(*) FROM outbox").fetchone()[0]
        c2_oe = conn_c2.execute("SELECT count(*) FROM outbox_events").fetchone()[0]
        assert c2_out == c1_out
        assert c2_oe == c1_oe
        conn_c2.close()


def test_review_overnight_defect2_legacy_digest_collapse_only_identical_payload_and_membership_idempotent_reopen(tmp_path: Path):
    """Defect 2: Legacy digest date key preserves different payloadA and failed payloadB with distinct keys; only identical collapse."""
    db_path = tmp_path / "defect2.db"
    conn = store.get_connection(db_path)
    conn.execute("DROP INDEX IF EXISTS idx_outbox_key")
    conn.execute("DROP INDEX IF EXISTS idx_outbox_events_member")

    date_str = "2026-10-27"
    # Row 1: Payload A, sent
    conn.execute(
        "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
        "VALUES (1, NULL, 'digest', 'digest', 'digest', 'Digest Payload A', 'sent', ?)",
        (f"{date_str}T08:00:00",),
    )
    conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (1, 'item_A', 1)")

    # Row 2: Payload B (different text and membership), failed
    conn.execute(
        "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
        "VALUES (2, NULL, 'digest', 'digest', 'digest', 'Digest Payload B Different', 'failed', ?)",
        (f"{date_str}T09:00:00",),
    )
    conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (2, 'item_B', 1)")

    # Row 3 and 4: Truly identical copies of Payload C (failed), should collapse together
    conn.execute(
        "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
        "VALUES (3, NULL, 'digest', 'digest', 'digest', 'Digest Payload C Duplicate', 'failed', ?)",
        (f"{date_str}T10:00:00",),
    )
    conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (3, 'item_C', 1)")

    conn.execute(
        "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
        "VALUES (4, NULL, 'digest', 'digest', 'digest', 'Digest Payload C Duplicate', 'failed', ?)",
        (f"{date_str}T10:30:00",),
    )
    conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (4, 'item_C', 1)")
    conn.commit()

    store.collapse_outbox_duplicates(conn)

    cur = conn.cursor()
    # Row 1 must be kept with canonical digest key and status sent
    cur.execute("SELECT key, status, rendered_text FROM outbox WHERE id = 1")
    r1 = cur.fetchone()
    assert r1 is not None
    assert r1[0] == f"digest:{date_str}"
    assert r1[1] == "sent"
    assert r1[2] == "Digest Payload A"

    # Row 2 must NOT be deleted or marked sent; must retain status failed and distinct stable legacy key
    cur.execute("SELECT key, status, rendered_text FROM outbox WHERE id = 2")
    r2 = cur.fetchone()
    assert r2 is not None
    assert r2[0] == f"legacy:digest:{date_str}:2"
    assert r2[1] == "failed"
    assert r2[2] == "Digest Payload B Different"

    # Row 2 membership must be intact in outbox_events
    cur.execute("SELECT event_id FROM outbox_events WHERE outbox_id = 2")
    assert cur.fetchone()[0] == "item_B"

    # Row 3 and 4: Truly identical copies of Payload C (failed), should collapse together (newer kept)
    cur.execute("SELECT id, key, status FROM outbox WHERE id IN (3, 4)")
    c_rows = cur.fetchall()
    assert len(c_rows) == 1
    assert c_rows[0][0] == 4
    assert c_rows[0][1] == f"legacy:digest:{date_str}:3"

    conn.close()

    # Verify idempotent migration across reopen
    reopen_conn = store.get_connection(db_path)
    cur_ro = reopen_conn.cursor()
    cur_ro.execute("SELECT id, key, status FROM outbox ORDER BY id")
    ro_rows = cur_ro.fetchall()
    assert len(ro_rows) == 3
    assert ro_rows[0][1] == f"digest:{date_str}"
    assert ro_rows[0][2] == "sent"
    assert ro_rows[1][1] == f"legacy:digest:{date_str}:2"
    assert ro_rows[1][2] == "failed"
    assert ro_rows[2][1] == f"legacy:digest:{date_str}:3"
    assert ro_rows[2][2] == "failed"
    reopen_conn.close()


def test_review_overnight_defect3_cluster_member_revisions_carried_gemini25flash_both_orders(tmp_path: Path, monkeypatch):
    """Defect 3: Mixed-revision cluster (Gemini 2.5 Flash A rev2 + B rev1 and reverse order) carries true member revisions and avoids premarking future revs."""
    db_path = tmp_path / "defect3.db"
    conn = store.get_connection(db_path)

    # Block external delivery and track delivered texts
    delivered_msgs = []
    monkeypatch.setattr(
        delivery,
        "deliver",
        lambda text, dry_run=False, conn=None, outbox_id=None, page_number=None: (delivered_msgs.append(text) or True, ""),
    )

    ev_a_rev2 = {
        "id": "source-a:gemini-2-5-flash",
        "vendor": "Google",
        "title": "Google introduces Gemini 2.5 Flash foundation model",
        "product": "Google AI",
        "published_at": "2026-10-29T10:00:00Z",
        "revision": 2,
        "raw_content": "Gemini 2.5 Flash is our new model.",
    }
    ev_b_rev1 = {
        "id": "source-b:gemini-2-5-flash",
        "vendor": "Google",
        "title": "Gemini 2.5 Flash release notes",
        "product": "Google Developers",
        "published_at": "2026-10-29T10:05:00Z",
        "revision": 1,
        "raw_content": "Gemini 2.5 Flash API docs.",
    }

    # Order 1: A rev 2 + B rev 1
    clustered1 = triage.cluster_duplicates([ev_a_rev2, ev_b_rev1])
    assert len(clustered1) == 1
    m1 = clustered1[0]
    assert m1["member_items"] == [("source-a:gemini-2-5-flash", 2), ("source-b:gemini-2-5-flash", 1)]

    out_id1 = store.add_to_outbox(conn, m1["id"], "high", "Gemini 2.5 Flash Announcement", revision=m1["revision"], member_events=[m1])
    # Verify outbox_events has exact member revisions
    cur = conn.cursor()
    cur.execute("SELECT event_id, revision FROM outbox_events WHERE outbox_id = ? ORDER BY event_id", (out_id1,))
    oe_map1 = dict(cur.fetchall())
    assert oe_map1["source-a:gemini-2-5-flash"] == 2
    assert oe_map1["source-b:gemini-2-5-flash"] == 1

    # Complete delivery
    store.update_outbox_status(conn, out_id1, "sent")
    runner._mark_outbox_done(conn, out_id1, m1["id"])
    runner._mark_all_done(conn, m1)
    runner._mark_all_notified(conn, m1, "sent")

    # A rev 2 sent, B rev 1 sent
    assert store.is_event_already_sent(conn, "source-a:gemini-2-5-flash", revision=2) is True
    assert store.is_event_already_sent(conn, "source-b:gemini-2-5-flash", revision=1) is True
    # Crucial: B rev 2 must NOT be premarked as sent
    assert store.is_event_already_sent(conn, "source-b:gemini-2-5-flash", revision=2) is False

    # Now new B rev 2 arrives
    ev_b_rev2 = dict(ev_b_rev1)
    ev_b_rev2["revision"] = 2
    ev_b_rev2["raw_content"] = "Gemini 2.5 Flash API docs v2 with updated pricing."

    # Since B rev 2 was not premarked, it is recognized as unseen/not already sent
    assert store.is_event_already_sent(conn, "source-b:gemini-2-5-flash", revision=2) is False
    out_id1_b2 = store.add_to_outbox(conn, ev_b_rev2["id"], "high", "Gemini 2.5 Flash B Rev 2 Update", revision=2, member_events=[ev_b_rev2])
    store.update_outbox_status(conn, out_id1_b2, "sent")
    runner._mark_outbox_done(conn, out_id1_b2, ev_b_rev2["id"])
    runner._mark_all_done(conn, ev_b_rev2)
    runner._mark_all_notified(conn, ev_b_rev2, "sent")

    # Now B rev 2 is marked sent, preventing duplicate delivery on subsequent checks
    assert store.is_event_already_sent(conn, "source-b:gemini-2-5-flash", revision=2) is True

    # Order 2: Reverse order (B rev 1 + A rev 2) in a fresh database
    db_path2 = tmp_path / "defect3_reverse.db"
    conn2 = store.get_connection(db_path2)

    clustered2 = triage.cluster_duplicates([ev_b_rev1, ev_a_rev2])
    assert len(clustered2) == 1
    m2 = clustered2[0]
    assert m2["member_items"] == [("source-b:gemini-2-5-flash", 1), ("source-a:gemini-2-5-flash", 2)]

    out_id2 = store.add_to_outbox(conn2, m2["id"], "high", "Gemini 2.5 Flash Reverse Order", revision=m2["revision"], member_events=[m2])
    cur2 = conn2.cursor()
    cur2.execute("SELECT event_id, revision FROM outbox_events WHERE outbox_id = ? ORDER BY event_id", (out_id2,))
    oe_map2 = dict(cur2.fetchall())
    assert oe_map2["source-b:gemini-2-5-flash"] == 1
    assert oe_map2["source-a:gemini-2-5-flash"] == 2

    store.update_outbox_status(conn2, out_id2, "sent")
    runner._mark_outbox_done(conn2, out_id2, m2["id"])
    runner._mark_all_done(conn2, m2)
    runner._mark_all_notified(conn2, m2, "sent")

    assert store.is_event_already_sent(conn2, "source-b:gemini-2-5-flash", revision=1) is True
    assert store.is_event_already_sent(conn2, "source-a:gemini-2-5-flash", revision=2) is True
    assert store.is_event_already_sent(conn2, "source-b:gemini-2-5-flash", revision=2) is False

    # New B rev 2 arrives for reverse order
    assert store.is_event_already_sent(conn2, "source-b:gemini-2-5-flash", revision=2) is False
    out_id2_b2 = store.add_to_outbox(conn2, ev_b_rev2["id"], "high", "Gemini 2.5 Flash B Rev 2 Reverse", revision=2, member_events=[ev_b_rev2])
    store.update_outbox_status(conn2, out_id2_b2, "sent")
    runner._mark_outbox_done(conn2, out_id2_b2, ev_b_rev2["id"])
    runner._mark_all_done(conn2, ev_b_rev2)
    runner._mark_all_notified(conn2, ev_b_rev2, "sent")
    assert store.is_event_already_sent(conn2, "source-b:gemini-2-5-flash", revision=2) is True

    conn.close()
    conn2.close()


def test_review_overnight_defect4_triage_model_launch_clause_negatives_and_positives():
    """Defect 4: Unrelated model name + launch wording in separate clauses does not misclassify tutorial/dashboard as new_model."""
    # 1. Defect reproduction fixture from brief
    ev_defective = {
        "title": "Fine-tuning best practices for GPT-4o",
        "raw_content": "We are launching a new dashboard feature today",
        "product": "OpenAI API",
        "source_id": "blog",
    }
    t_type, prio = triage.classify_event(ev_defective)
    assert t_type != "new_model", f"Expected t_type != new_model, got {t_type}"
    assert prio != "high", f"Expected prio != high, got {prio}"

    # 2. True model release positives: model name and release wording in same clause/announcement
    positives = [
        {
            "title": "Google introduces Gemini 2.5 Flash foundation model",
            "raw_content": "We are releasing Gemini 2.5 Flash today.",
            "product": "Google AI Updates",
        },
        {
            "title": "Claude 3.7 Sonnet is now available",
            "raw_content": "Anthropic announces Claude 3.7 Sonnet.",
            "product": "Anthropic Announcements",
        },
        {
            "title": "Introducing GPT-4o: Our new flagship model",
            "raw_content": "GPT-4o is now generally available with multimodal reasoning.",
            "product": "OpenAI Models",
        },
        {
            "title": "Google introduces Gemini 4 Argon",
            "raw_content": "Announcing the Gemini 4 Argon model release.",
            "product": "Google Updates",
        },
    ]
    for rel in positives:
        t, p = triage.classify_event(rel)
        assert t == "new_model", f"Expected new_model for '{rel['title']}', got {t}"
        assert p == "high", f"Expected high for '{rel['title']}', got {p}"

    # 3. Negatives: SDK support, tutorials, dashboards, customer stories
    negatives = [
        {
            "title": "Python SDK v1.50.0",
            "raw_content": "Added support for GPT-4o and updated dependencies.",
            "product": "OpenAI Python SDK",
            "source_id": "github",
        },
        {
            "title": "Tutorial: How to fine-tune GPT-4o for code generation",
            "raw_content": "A step-by-step walkthrough for developers fine-tuning GPT-4o.",
            "product": "OpenAI API",
            "source_id": "blog",
        },
        {
            "title": "Console Updates",
            "raw_content": "We are launching a new playground dashboard for Claude 3.5 Sonnet users.",
            "product": "Anthropic Console",
            "source_id": "changelog",
        },
        {
            "title": "How Canva uses Claude to power creative design",
            "raw_content": "Canva is transforming creative workflows with Claude.",
            "product": "Anthropic Customer Stories",
            "source_id": "blog",
        },
    ]
    for neg in negatives:
        t, p = triage.classify_event(neg)
        assert t != "new_model", f"Negative '{neg['title']}' must not be new_model, got {t}"
        assert p != "high", f"Negative '{neg['title']}' must not be high, got {p}"


def test_review_watcher64_defect1_legacy_digest_partial_recipient_failure_retry_b_only_and_idempotent_reopen(tmp_path: Path, monkeypatch):
    """Watcher64 Defect 1: Legacy identical digest payload+members with different recipient delivery (target-A sent, target-B failed).

    Migration collapse derives consistent aggregate status (failed), preserves pending outbox row and page status.
    Pending delivery retries target-B only without resending target-A. Second reopen is idempotent.
    Tested under both current and old index schemas.
    """
    for is_old_schema in (False, True):
        schema_name = "old" if is_old_schema else "cur"
        db_path = tmp_path / f"defect1_{schema_name}.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        if is_old_schema:
            conn.execute(
                "CREATE TABLE outbox (id INTEGER PRIMARY KEY, key TEXT, event_id TEXT, priority TEXT, kind TEXT, "
                "rendered_text TEXT, status TEXT, attempts INTEGER DEFAULT 0, created_at TEXT, revision INTEGER DEFAULT 1)"
            )
            conn.execute(
                "CREATE TABLE outbox_events (outbox_id INTEGER, event_id TEXT, revision INTEGER, PRIMARY KEY (outbox_id, event_id, revision))"
            )
            conn.execute(
                "CREATE TABLE outbox_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, outbox_id INTEGER, page_number INTEGER DEFAULT 1, chat_id TEXT, status TEXT, attempts INTEGER DEFAULT 0, last_attempt_at TEXT DEFAULT '', error_message TEXT DEFAULT '')"
            )
            conn.execute(
                "CREATE TABLE outbox_pages (outbox_id INTEGER, page_number INTEGER, rendered_text TEXT, status TEXT, member_ids_json TEXT DEFAULT '[]', PRIMARY KEY (outbox_id, page_number))"
            )
        else:
            store._init_schema(conn)
            conn.execute("DROP INDEX IF EXISTS idx_outbox_key")
            conn.execute("DROP INDEX IF EXISTS idx_outbox_events_member")

        date_str = "2026-10-29"
        conn.execute(
            "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
            "VALUES (1, NULL, 'digest', 'digest', 'digest', 'Digest Payload', 'sent', ?)",
            (f"{date_str}T08:00:00",),
        )
        conn.execute("INSERT INTO outbox_pages (outbox_id, page_number, rendered_text, status, member_ids_json) VALUES (1, 1, 'Digest Payload', 'sent', '[\"item_1\"]')")
        conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (1, 'item_1', 1)")
        conn.execute("INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status) VALUES (1, 1, 'target-A', 'sent')")

        conn.execute(
            "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
            "VALUES (2, NULL, 'digest', 'digest', 'digest', 'Digest Payload', 'failed', ?)",
            (f"{date_str}T08:30:00",),
        )
        conn.execute("INSERT INTO outbox_pages (outbox_id, page_number, rendered_text, status, member_ids_json) VALUES (2, 1, 'Digest Payload', 'failed', '[\"item_1\"]')")
        conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (2, 'item_1', 1)")
        conn.execute("INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status) VALUES (2, 1, 'target-B', 'failed')")
        conn.commit()
        conn.close()

        # Step 1: Open and migrate via store.get_connection
        migrated_conn = store.get_connection(db_path)
        cur = migrated_conn.cursor()
        cur.execute("SELECT id, status FROM outbox")
        target_rows = [(r[0], r[1]) for r in cur.fetchall()]
        assert target_rows == [(1, "failed")], f"Expected row 1 status 'failed', got {target_rows}"

        cur.execute("SELECT outbox_id, chat_id, status FROM outbox_deliveries ORDER BY chat_id")
        deliveries = [(r[0], r[1], r[2]) for r in cur.fetchall()]
        assert deliveries == [(1, "target-A", "sent"), (1, "target-B", "failed")]

        pending = [r["id"] for r in store.get_pending_outbox(migrated_conn)]
        assert pending == [1], f"Expected pending_outbox [1], got {pending}"

        cur.execute("SELECT outbox_id, page_number, status FROM outbox_pages")
        pages = [(r[0], r[1], r[2]) for r in cur.fetchall()]
        assert pages == [(1, 1, "failed")]

        # Step 2: Retry delivery - only B must be retried, A must not be resent
        delivered_chats = []
        def fake_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
            p_num = page_number or 1
            p_cids = store.get_pending_delivery_chat_ids(conn, outbox_id, page_number=p_num)
            for c in p_cids:
                delivered_chats.append(c)
                store.record_delivery_attempt(conn, outbox_id, c, success=True, page_number=p_num)
            return True, ""

        monkeypatch.setenv("WATCH_CHAT_IDS", "target-A,target-B")
        monkeypatch.setattr(delivery, "deliver", fake_deliver)
        monkeypatch.setattr(runner, "load_sources", lambda: [])
        monkeypatch.setattr(enricher, "enrich_event", lambda ev, *args, **kwargs: enricher.stub_enrich(ev, ""))

        runner.run_cycle(db_path=db_path, dry_run=False, once=True, digest_mode=True)
        assert delivered_chats == ["target-B"], f"Expected only target-B retried, got {delivered_chats}"

        cur.execute("SELECT id, status FROM outbox")
        assert cur.fetchone()[1] == "sent"
        cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = 1 AND page_number = 1")
        assert cur.fetchone()[0] == "sent"
        migrated_conn.close()

        # Step 3: Second reopen idempotent
        ro_conn = store.get_connection(db_path)
        cur_ro = ro_conn.cursor()
        cur_ro.execute("SELECT id, status FROM outbox")
        assert cur_ro.fetchall()[0][1] == "sent"
        assert len(store.get_pending_outbox(ro_conn)) == 0
        cur_ro.execute("SELECT outbox_id, page_number, status FROM outbox_pages")
        assert cur_ro.fetchall()[0][2] == "sent"
        cur_ro.execute("SELECT chat_id, status FROM outbox_deliveries ORDER BY chat_id")
        deliv_ro = cur_ro.fetchall()
        assert deliv_ro[0][0] == "target-A" and deliv_ro[0][1] == "sent"
        assert deliv_ro[1][0] == "target-B" and deliv_ro[1][1] == "sent"
        ro_conn.close()


def test_review_watcher64_defect2_fullrunner_mixed_cluster_rev2_changed_content_delivered_and_reverse_order(tmp_path: Path, monkeypatch):
    """Watcher64 Defect 2: Fullrunner mixed recognized model cluster A2+B1 initially links revisions correctly.

    Exact repeat delivers no additional messages. Later B2 has changed content and is delivered
    (not completed without delivery), next repeat sends no duplicate. Reverse A1+B2 case preserved.
    """
    monkeypatch.setenv("WATCH_CHAT_IDS", "chat-1")
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(enricher, "enrich_event", lambda ev, *args, **kwargs: enricher.stub_enrich(ev, ""))

    # Order 1: Initial A2 + B1, then B2 changed content
    db_path1 = tmp_path / "defect2_order1.db"
    conn1 = store.get_connection(db_path1)

    delivered_msgs = []
    def fake_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
        delivered_msgs.append(text)
        if conn is not None and outbox_id is not None:
            p_num = page_number or 1
            cids = store.get_pending_delivery_chat_ids(conn, outbox_id, page_number=p_num)
            for c in cids:
                store.record_delivery_attempt(conn, outbox_id, c, success=True, page_number=p_num)
        return True, ""

    monkeypatch.setattr(delivery, "deliver", fake_deliver)

    src_a = {"id": "source-a", "vendor": "Google", "product": "Google AI"}
    entry_a2 = parser.FeedEntry(
        entry_id="gemini-2-5-flash",
        title="Google introduces Gemini 2.5 Flash foundation model",
        link="https://example.com/a2",
        published_at="2026-10-29T10:00:00Z",
        summary="Gemini 2.5 Flash",
        raw_content="We are releasing Gemini 2.5 Flash foundation model today.",
    )
    store.record_unseen_entries(conn1, src_a, [entry_a2])
    conn1.execute("UPDATE events SET revision = 2 WHERE id = 'source-a:gemini-2-5-flash'")
    conn1.execute("UPDATE processing_jobs SET revision = 2 WHERE event_id = 'source-a:gemini-2-5-flash'")
    conn1.commit()

    src_b = {"id": "source-b", "vendor": "Google", "product": "Google Developers"}
    entry_b1 = parser.FeedEntry(
        entry_id="gemini-2-5-flash",
        title="Google releases Gemini 2.5 Flash",
        link="https://example.com/b1",
        published_at="2026-10-29T10:05:00Z",
        summary="Gemini 2.5 Flash announcement",
        raw_content="Announcing Gemini 2.5 Flash model availability.",
    )
    store.record_unseen_entries(conn1, src_b, [entry_b1])

    # Run 1: Cluster of A2 + B1 delivered
    runner.run_cycle(db_path=db_path1, dry_run=False, once=True)
    assert len(delivered_msgs) == 1
    delivered_msgs.clear()

    # Verify outbox has both members in outbox_events
    cur1 = conn1.cursor()
    cur1.execute("SELECT event_id, revision FROM outbox_events WHERE outbox_id = 1 ORDER BY event_id")
    members_r1 = cur1.fetchall()
    assert [tuple(r) for r in members_r1] == [("source-a:gemini-2-5-flash", 2), ("source-b:gemini-2-5-flash", 1)]

    # Exact repeat: no additional messages
    runner.run_cycle(db_path=db_path1, dry_run=False, once=True)
    assert len(delivered_msgs) == 0

    # Run 2: B2 changed content
    entry_b2 = parser.FeedEntry(
        entry_id="gemini-2-5-flash",
        title="Google releases Gemini 2.5 Flash updated",
        link="https://example.com/b1",
        published_at="2026-10-29T12:00:00Z",
        summary="Gemini 2.5 Flash updated",
        raw_content="Announcing Gemini 2.5 Flash model availability with updated benchmarks and pricing.",
    )
    unseen_b2 = store.record_unseen_entries(conn1, src_b, [entry_b2])
    assert len(unseen_b2) == 1 and unseen_b2[0]["revision"] == 2
    assert store.is_event_already_sent(conn1, "source-b:gemini-2-5-flash", revision=2) is False

    runner.run_cycle(db_path=db_path1, dry_run=False, once=True)
    assert len(delivered_msgs) == 1, "B2 changed content must be delivered and not skipped"
    delivered_msgs.clear()
    assert store.is_event_already_sent(conn1, "source-b:gemini-2-5-flash", revision=2) is True

    # Next repeat: no duplicate
    runner.run_cycle(db_path=db_path1, dry_run=False, once=True)
    assert len(delivered_msgs) == 0
    conn1.close()

    # Order 2: Reverse A1 + B2 case preserved
    db_path2 = tmp_path / "defect2_order2.db"
    conn2 = store.get_connection(db_path2)
    delivered_msgs.clear()

    entry_a1 = parser.FeedEntry(
        entry_id="gemini-2-5-flash",
        title="Google introduces Gemini 2.5 Flash foundation model",
        link="https://example.com/a1",
        published_at="2026-10-29T10:00:00Z",
        summary="Gemini 2.5 Flash",
        raw_content="We are releasing Gemini 2.5 Flash foundation model today.",
    )
    store.record_unseen_entries(conn2, src_a, [entry_a1])

    store.record_unseen_entries(conn2, src_b, [entry_b1])
    conn2.execute("UPDATE events SET revision = 2 WHERE id = 'source-b:gemini-2-5-flash'")
    conn2.execute("UPDATE processing_jobs SET revision = 2 WHERE event_id = 'source-b:gemini-2-5-flash'")
    conn2.commit()

    # Initial Run 1: Reverse order A1 + B2
    runner.run_cycle(db_path=db_path2, dry_run=False, once=True)
    assert len(delivered_msgs) == 1
    delivered_msgs.clear()

    # Exact repeat: no additional messages
    runner.run_cycle(db_path=db_path2, dry_run=False, once=True)
    assert len(delivered_msgs) == 0

    # Run 2: A2 changed content
    entry_a2_changed = parser.FeedEntry(
        entry_id="gemini-2-5-flash",
        title="Google introduces Gemini 2.5 Flash foundation model updated",
        link="https://example.com/a1",
        published_at="2026-10-29T12:00:00Z",
        summary="Gemini 2.5 Flash updated",
        raw_content="We are releasing Gemini 2.5 Flash foundation model today with updated pricing.",
    )
    unseen_a2 = store.record_unseen_entries(conn2, src_a, [entry_a2_changed])
    assert len(unseen_a2) == 1 and unseen_a2[0]["revision"] == 2
    assert store.is_event_already_sent(conn2, "source-a:gemini-2-5-flash", revision=2) is False

    runner.run_cycle(db_path=db_path2, dry_run=False, once=True)
    assert len(delivered_msgs) == 1, "A2 changed content must be delivered and not skipped"
    delivered_msgs.clear()
    assert store.is_event_already_sent(conn2, "source-a:gemini-2-5-flash", revision=2) is True

    # Next repeat: no duplicate
    runner.run_cycle(db_path=db_path2, dry_run=False, once=True)
    assert len(delivered_msgs) == 0
    conn2.close()


def test_review_watcher75_defect1_legacy_digest_ambiguous_delivery_no_retry_and_durable_reopen(tmp_path: Path, monkeypatch):
    """Watcher75 Defect 1: Legacy identical digest collapse preserves ambiguous delivery evidence.

    Identical legacy digest row 1 (target-A sent) and duplicate row 2 (target-B globally/page ambiguous or
    sending after crash while delivery record still pending).
    Collapse preserves ambiguous/sending evidence, keeps target-A sent, updates target-B delivery to ambiguous,
    and sets kept row and page status to ambiguous.
    Fake full-runner never retries either target-A or target-B.
    Ambiguity is durable across subsequent reopens.
    Tested under both current and old index schemas and for both ambiguous and sending crash states.
    """
    for is_old_schema in (False, True):
        for b_status in ("ambiguous", "sending"):
            schema_name = "old" if is_old_schema else "cur"
            db_path = tmp_path / f"defect1_{schema_name}_{b_status}.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            if is_old_schema:
                conn.execute(
                    "CREATE TABLE outbox (id INTEGER PRIMARY KEY, key TEXT, event_id TEXT, priority TEXT, kind TEXT, "
                    "rendered_text TEXT, status TEXT, attempts INTEGER DEFAULT 0, created_at TEXT, revision INTEGER DEFAULT 1)"
                )
                conn.execute(
                    "CREATE TABLE outbox_events (outbox_id INTEGER, event_id TEXT, revision INTEGER, PRIMARY KEY (outbox_id, event_id, revision))"
                )
                conn.execute(
                    "CREATE TABLE outbox_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, outbox_id INTEGER, page_number INTEGER DEFAULT 1, chat_id TEXT, status TEXT, attempts INTEGER DEFAULT 0, last_attempt_at TEXT DEFAULT '', error_message TEXT DEFAULT '')"
                )
                conn.execute(
                    "CREATE TABLE outbox_pages (outbox_id INTEGER, page_number INTEGER, rendered_text TEXT, status TEXT, member_ids_json TEXT DEFAULT '[]', PRIMARY KEY (outbox_id, page_number))"
                )
            else:
                store._init_schema(conn)
                conn.execute("DROP INDEX IF EXISTS idx_outbox_key")
                conn.execute("DROP INDEX IF EXISTS idx_outbox_events_member")

            date_str = "2026-10-29"
            conn.execute(
                "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
                "VALUES (1, NULL, 'digest', 'digest', 'digest', 'Digest Payload', 'sent', ?)",
                (f"{date_str}T08:00:00",),
            )
            conn.execute("INSERT INTO outbox_pages (outbox_id, page_number, rendered_text, status, member_ids_json) VALUES (1, 1, 'Digest Payload', 'sent', '[\"item_1\"]')")
            conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (1, 'item_1', 1)")
            conn.execute("INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status) VALUES (1, 1, 'target-A', 'sent')")

            conn.execute(
                "INSERT INTO outbox (id, key, event_id, priority, kind, rendered_text, status, created_at) "
                "VALUES (2, NULL, 'digest', 'digest', 'digest', 'Digest Payload', ?, ?)",
                (b_status, f"{date_str}T08:30:00"),
            )
            conn.execute("INSERT INTO outbox_pages (outbox_id, page_number, rendered_text, status, member_ids_json) VALUES (2, 1, 'Digest Payload', ?, '[\"item_1\"]')", (b_status,))
            conn.execute("INSERT INTO outbox_events (outbox_id, event_id, revision) VALUES (2, 'item_1', 1)")
            # Recipient delivery record for target-B still pending at crash time
            conn.execute("INSERT INTO outbox_deliveries (outbox_id, page_number, chat_id, status) VALUES (2, 1, 'target-B', 'pending')")
            conn.commit()
            conn.close()

            # Step 1: Open and migrate via store.get_connection
            migrated_conn = store.get_connection(db_path)
            cur = migrated_conn.cursor()
            cur.execute("SELECT id, status FROM outbox")
            target_rows = [(r[0], r[1]) for r in cur.fetchall()]
            assert target_rows == [(1, "ambiguous")], f"Expected row 1 status 'ambiguous', got {target_rows}"

            cur.execute("SELECT outbox_id, chat_id, status FROM outbox_deliveries ORDER BY chat_id")
            deliveries = [(r[0], r[1], r[2]) for r in cur.fetchall()]
            assert deliveries == [(1, "target-A", "sent"), (1, "target-B", "ambiguous")], f"Expected target-B 'ambiguous', got {deliveries}"

            pending = [r["id"] for r in store.get_pending_outbox(migrated_conn)]
            assert pending == [], f"Expected pending_outbox [], got {pending}"

            cur.execute("SELECT outbox_id, page_number, status FROM outbox_pages")
            pages = [(r[0], r[1], r[2]) for r in cur.fetchall()]
            assert pages == [(1, 1, "ambiguous")], f"Expected page 1 status 'ambiguous', got {pages}"

            assert store.get_pending_delivery_chat_ids(migrated_conn, 1, page_number=1) == []

            # Step 2: Fake full-runner execution - neither target-A nor target-B should be delivered
            delivered_chats = []
            def fake_deliver(text, dry_run=False, conn=None, outbox_id=None, page_number=None):
                p_num = page_number or 1
                p_cids = store.get_pending_delivery_chat_ids(conn, outbox_id, page_number=p_num)
                for c in p_cids:
                    delivered_chats.append(c)
                    store.record_delivery_attempt(conn, outbox_id, c, success=True, page_number=p_num)
                return True, ""

            monkeypatch.setenv("WATCH_CHAT_IDS", "target-A,target-B")
            monkeypatch.setattr(delivery, "deliver", fake_deliver)
            monkeypatch.setattr(runner, "load_sources", lambda: [])
            monkeypatch.setattr(enricher, "enrich_event", lambda ev, *args, **kwargs: enricher.stub_enrich(ev, ""))

            runner.run_cycle(db_path=db_path, dry_run=False, once=True, digest_mode=True)
            assert delivered_chats == [], f"Expected no delivery to either target, but delivered: {delivered_chats}"

            cur.execute("SELECT id, status FROM outbox")
            assert cur.fetchone()[1] == "ambiguous"
            cur.execute("SELECT status FROM outbox_pages WHERE outbox_id = 1 AND page_number = 1")
            assert cur.fetchone()[0] == "ambiguous"
            migrated_conn.close()

            # Step 3: Second reopen - verify durable ambiguity and idempotence
            ro_conn = store.get_connection(db_path)
            cur_ro = ro_conn.cursor()
            cur_ro.execute("SELECT id, status FROM outbox")
            assert cur_ro.fetchall()[0][1] == "ambiguous"
            assert len(store.get_pending_outbox(ro_conn)) == 0
            cur_ro.execute("SELECT outbox_id, page_number, status FROM outbox_pages")
            assert cur_ro.fetchall()[0][2] == "ambiguous"
            cur_ro.execute("SELECT chat_id, status FROM outbox_deliveries ORDER BY chat_id")
            deliv_ro = cur_ro.fetchall()
            assert deliv_ro[0][0] == "target-A" and deliv_ro[0][1] == "sent"
            assert deliv_ro[1][0] == "target-B" and deliv_ro[1][1] == "ambiguous"
            assert store.get_pending_delivery_chat_ids(ro_conn, 1, page_number=1) == []
            ro_conn.close()


