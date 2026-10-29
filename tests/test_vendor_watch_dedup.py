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
    """Topilma 2: Kechagi kundan qolgan dayjest retry qilinganda bugungi yangi dayjest surilib ketmaydi."""
    db_path = tmp_path / "test_retry_date.db"
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

    # Ikkita xabar ham yetkazilishi kerak:
    # 1) Kechagi retry qilingan dayjest
    # 2) Bugungi yangi hodisalar dayjesti
    assert len(delivered) == 2
    assert "Kechagi 2026-10-28 dayjest matni" in delivered[0]
    assert "v2.1.0" in delivered[1]

    # last_digest_date bugungi kunga teng bo'lishi kerak
    assert store.get_app_state(conn, "last_digest_date") == "2026-10-29"


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

