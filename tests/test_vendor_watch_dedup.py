"""Tests for deduplication, outbox unique constraints, enrichment failure handling, and triage."""

from pathlib import Path
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
