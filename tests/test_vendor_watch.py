"""Unit and integration tests for vendor watch MVP."""

from pathlib import Path
import pytest

from src.vendor_watch import delivery, enricher, fetcher, parser, renderer, store, triage


@pytest.fixture(autouse=True)
def _block_real_vertex(monkeypatch):
    # Testlar haqiqiy Vertex'ga chiqmasin; kerakli testlar o'z mock'ini ustiga qo'yadi
    from src import vertex

    def _blocked(*args, **kwargs):
        raise RuntimeError("Real Vertex call blocked in tests")

    monkeypatch.setattr(vertex, "chat_json", _blocked)


def test_baseline_marks_seen(tmp_path: Path):
    """Initial baseline parse marks all entries as seen; no new events emitted."""
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    source = {
        "id": "anthropic-claude-code",
        "vendor": "Anthropic",
        "product": "Claude Code",
    }
    entries = [
        parser.FeedEntry(
            entry_id="v1.0.0",
            title="Claude Code v1.0.0",
            link="https://github.com/anthropics/claude-code/releases/tag/v1.0.0",
            published_at="2026-10-01T00:00:00Z",
            summary="Initial release",
            raw_content="Initial release notes",
        ),
        parser.FeedEntry(
            entry_id="v1.1.0",
            title="Claude Code v1.1.0",
            link="https://github.com/anthropics/claude-code/releases/tag/v1.1.0",
            published_at="2026-10-15T00:00:00Z",
            summary="Bug fixes",
            raw_content="Bug fixes and improvements",
        ),
    ]

    store.record_baseline(conn, source, entries, etag="etag-1", last_modified="lm-1")

    meta = store.get_source_meta(conn, source["id"])
    assert meta is not None
    assert meta["is_baseline_done"] == 1
    assert meta["etag"] == "etag-1"

    # Subsequent check with the exact same entries yields 0 unseen events
    unseen = store.record_unseen_entries(conn, source, entries)
    assert len(unseen) == 0


def test_second_run_detects_new_entry(tmp_path: Path):
    """Subsequent run detects exactly newly added entry."""
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    source = {
        "id": "openai-python",
        "vendor": "OpenAI",
        "product": "OpenAI Python SDK",
    }
    initial_entries = [
        parser.FeedEntry(
            entry_id="v1.50.0",
            title="Release v1.50.0",
            link="https://github.com/openai/openai-python/releases/tag/v1.50.0",
            published_at="2026-10-01T00:00:00Z",
            summary="Base version",
            raw_content="Base version notes",
        )
    ]
    store.record_baseline(conn, source, initial_entries)

    # Second run introduces v1.51.0
    new_entry = parser.FeedEntry(
        entry_id="v1.51.0",
        title="Release v1.51.0",
        link="https://github.com/openai/openai-python/releases/tag/v1.51.0",
        published_at="2026-10-28T00:00:00Z",
        summary="New features",
        raw_content="Added support for new reasoning endpoints",
    )
    all_entries = [new_entry, initial_entries[0]]

    unseen = store.record_unseen_entries(conn, source, all_entries)
    assert len(unseen) == 1
    assert unseen[0]["entry_id"] == "v1.51.0"
    assert unseen[0]["title"] == "Release v1.51.0"


def test_duplicate_clustering():
    """Duplicate announcements across sources within 24h cluster to one item with multiple links."""
    ev1 = {
        "source_id": "google-vertex-ai-release-notes",
        "vendor": "Google",
        "product": "Vertex AI Release Notes",
        "title": "Gemini 2.5 Flash GA in Vertex AI",
        "link": "https://cloud.google.com/vertex-ai/docs/release-notes#gemini-25",
        "published_at": "2026-10-28T00:00:00Z",
        "raw_content": "Gemini 2.5 Flash is now generally available in Vertex AI.",
    }
    ev2 = {
        "source_id": "google-blog-gemini",
        "vendor": "Google",
        "product": "Google Gemini Blog",
        "title": "Gemini 2.5 Flash updates and performance",
        "link": "https://blog.google/products/gemini/gemini-2-5-flash-ga/",
        "published_at": "2026-10-28T04:00:00Z",
        "raw_content": "Announcing Gemini 2.5 Flash with superior multimodal benchmarks.",
    }

    clustered = triage.cluster_duplicates([ev1, ev2])
    assert len(clustered) == 1
    item = clustered[0]
    assert len(item["links"]) == 2
    assert "https://cloud.google.com/vertex-ai/docs/release-notes#gemini-25" in item["links"]
    assert "https://blog.google/products/gemini/gemini-2-5-flash-ga/" in item["links"]


def test_malformed_feed_resilience(tmp_path: Path):
    """Malformed feed raises FeedParseError, produces no events and retains last good snapshot."""
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    source = {
        "id": "anthropic-claude-code",
        "vendor": "Anthropic",
        "product": "Claude Code",
    }
    # Establish prior good snapshot
    store.mark_source_success(conn, source["id"], etag="good-etag", last_modified="good-lm")

    malformed_xml = b"<feed><title>Unclosed feed<entry><title>Incomplete"
    with pytest.raises(parser.FeedParseError):
        parser.parse_feed(malformed_xml)

    # When failure is recorded
    store.mark_source_failure(conn, source["id"], status_code=500)

    meta = store.get_source_meta(conn, source["id"])
    assert meta is not None
    assert meta["etag"] == "good-etag"
    assert meta["last_modified"] == "good-lm"
    assert meta["fail_count"] == 1


def test_semver_patch_goes_to_digest():
    """SDK patch release goes to digest unless security or breaking keywords are present."""
    ev = {
        "source_id": "openai-python",
        "vendor": "OpenAI",
        "product": "OpenAI Python SDK",
        "title": "Release v1.52.1",
        "raw_content": "Bug fix for stream timeout handling. No breaking changes.",
    }
    t_type, priority = triage.classify_event(ev)
    assert t_type == "sdk_patch"
    assert priority == "digest"


def test_deprecation_or_shutdown_high_priority():
    """'deprecat' or 'shutdown' keywords result in high priority."""
    ev_deprecate = {
        "source_id": "google-vertex-ai-release-notes",
        "vendor": "Google",
        "product": "Vertex AI",
        "title": "Gemini 1.0 Pro deprecation schedule",
        "raw_content": "This model is deprecated and will be sunset on June 1.",
    }
    t_type, prio = triage.classify_event(ev_deprecate)
    assert t_type == "deprecation"
    assert prio == "high"

    ev_shutdown = {
        "source_id": "openai-news",
        "vendor": "OpenAI",
        "product": "OpenAI News",
        "title": "Legacy Completions API shutdown",
        "raw_content": "The legacy endpoint will be completely shut down.",
    }
    t_type2, prio2 = triage.classify_event(ev_shutdown)
    assert t_type2 == "deprecation"
    assert prio2 == "high"


def test_outbox_retry_after_send_failure(tmp_path: Path, monkeypatch):
    """Failed send marks outbox item failed, subsequent retry dispatches successfully."""
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    out_id = store.add_to_outbox(conn, "test-event-1", "high", "Xabar matni")
    monkeypatch.setenv("BOT_TOKEN", "mock-bot-token-xyz")

    # 1. Simulate failure
    def mock_fail_send(*args, **kwargs):
        raise RuntimeError("Telegram API ulanish uzildi")

    monkeypatch.setattr(delivery, "send_to_telegram", mock_fail_send)
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["123456"])

    success, err = delivery.deliver("Xabar matni", dry_run=False)
    assert success is False
    store.update_outbox_status(conn, out_id, "failed", err)

    pending = store.get_pending_outbox(conn)
    assert len(pending) == 1
    assert pending[0]["status"] == "failed"
    assert pending[0]["attempts"] == 1

    # 2. Simulate subsequent successful retry
    sent_payloads = []
    def mock_success_send(token, chat_id, text, timeout=15):
        sent_payloads.append((chat_id, text))

    monkeypatch.setattr(delivery, "send_to_telegram", mock_success_send)

    retry_item = pending[0]
    success2, err2 = delivery.deliver(retry_item["rendered_text"], dry_run=False)
    assert success2 is True
    store.update_outbox_status(conn, retry_item["id"], "sent", err2)

    assert len(sent_payloads) == 1
    # Ensure item is no longer in pending/failed outbox
    still_pending = store.get_pending_outbox(conn)
    assert len(still_pending) == 0


def test_message_char_limit():
    """Rendered messages strictly comply with <= 3900 character limit."""
    long_text = "Juda uzun reliz matni bo'lib, uning ichida ko'plab qatorlar mavjud. " * 150
    ev = {
        "vendor": "Anthropic",
        "product": "Claude Code",
        "triage_type": "breaking",
        "title": "Claude Code Major 2.0 with Huge Changes",
        "link": "https://github.com/anthropics/claude-code/releases/tag/v2.0.0",
        "links": ["https://github.com/anthropics/claude-code/releases/tag/v2.0.0"],
    }
    enr = {
        "uz_title": "Claude Code 2.0 Katta Reliz",
        "uz_summary": long_text,
        "uz_benefit": "Ushbu foydali qism ham juda keng tushuntirilgan. " * 80,
        "uz_how_to_use": "npm install -g @anthropic-ai/claude-code@latest\n" * 50,
        "possible_overlap_with_custom_tool": {
            "tool_name": "Antigravity Switcher",
            "reason": "Ehtimoliy o'xshashlik mavjud.",
        },
    }

    high_msg = renderer.render_high_priority_message(ev, enr)
    assert len(high_msg) <= 3900

    # Test digest chunking
    items = [(ev, enr) for _ in range(15)]
    digest_msgs = renderer.render_digest_messages(items)
    assert len(digest_msgs) > 1
    for d_msg in digest_msgs:
        assert len(d_msg) <= 3900


def test_raw_mode_when_llm_raises(monkeypatch):
    """When LLM raises, raw mode fallback is activated and message delivery proceeds."""
    ev = {
        "vendor": "Anthropic",
        "product": "Claude Code",
        "title": "Claude Code v2.0 - New Architecture",
        "link": "https://github.com/anthropics/claude-code/releases/tag/v2.0.0",
        "raw_content": "A" * 600,
    }

    import src.vertex as vertex
    def mock_broken_chat(messages, schema):
        raise vertex.VertexError("HTTP 503: Service Unavailable")

    monkeypatch.setattr(vertex, "chat_json", mock_broken_chat)

    enr = enricher.enrich_event(ev, profile_text="", use_stub=False)
    assert enr["is_raw_mode"] is True
    assert enr["uz_title"] == "Claude Code v2.0 - New Architecture"
    assert len(enr["uz_summary"]) <= 300
    assert enr["uz_summary"] == "A" * 300

    msg = renderer.render_high_priority_message(ev, enr)
    assert len(msg) <= 3900
    assert "Claude Code v2.0 - New Architecture" in msg
    assert "https://github.com/anthropics/claude-code/releases/tag/v2.0.0" in msg


def test_non_breaking_negation_in_triage():
    """'non-breaking' or 'not breaking' phrases should not trigger breaking priority."""
    ev = {
        "source_id": "google-gemini-cli",
        "vendor": "Google",
        "product": "Gemini CLI",
        "title": "Gemini CLI v1.2.1",
        "raw_content": "This release includes non-breaking change improvements and is not breaking any workflow.",
    }
    t_type, prio = triage.classify_event(ev)
    assert t_type != "breaking"
    assert prio != "high"


def test_revision_updates_title_and_link(tmp_path: Path):
    """When an entry is revised, title, link and published_at must be updated in events store."""
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)
    source = {"id": "src-1", "vendor": "V", "product": "P"}

    entry_v1 = parser.FeedEntry(
        entry_id="e-1",
        title="Title V1",
        link="https://example.com/v1",
        published_at="2026-10-20T00:00:00Z",
        summary="Summary 1",
        raw_content="Content 1",
    )
    store.record_baseline(conn, source, [entry_v1])

    # Update with new content, title and link
    entry_v2 = parser.FeedEntry(
        entry_id="e-1",
        title="Title V2 Updated",
        link="https://example.com/v2-updated",
        published_at="2026-10-21T00:00:00Z",
        summary="Summary 2",
        raw_content="Content 2 Modified",
    )
    unseen = store.record_unseen_entries(conn, source, [entry_v2])
    assert len(unseen) == 1
    assert unseen[0]["revision"] == 2

    cur = conn.cursor()
    cur.execute("SELECT title, link, published_at, revision FROM events WHERE id = 'src-1:e-1'")
    row = dict(cur.fetchone())
    assert row["title"] == "Title V2 Updated"
    assert row["link"] == "https://example.com/v2-updated"
    assert row["published_at"] == "2026-10-21T00:00:00Z"
    assert row["revision"] == 2


def test_truncate_lines_long_single_line():
    """A long single line is truncated to remaining budget without crashing or dropping text."""
    long_line = "A" * 5000
    res = renderer.truncate_lines(long_line, limit=1000)
    assert len(res) <= 1000
    assert "(qisqartirildi)" in res


def test_regression_defect_1_atomic_processing_jobs(tmp_path: Path, monkeypatch):
    """Defect 1: Events/validators are persisted with atomic processing jobs; unfinished jobs resume independently of fetching."""
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)
    source = {"id": "src-defect1", "vendor": "Anthropic", "product": "Claude Code"}

    entry = parser.FeedEntry(
        entry_id="v1.9.0",
        title="Claude Code v1.9.0",
        link="https://github.com/anthropics/claude-code/releases/tag/v1.9.0",
        published_at="2026-10-28T00:00:00Z",
        summary="Summary text",
        raw_content="Feature release notes",
    )
    # 1. Ingestion atomically inserts event and creates pending processing job
    store.record_unseen_entries(conn, source, [entry])

    jobs = store.get_pending_processing_jobs(conn)
    assert len(jobs) == 1
    assert jobs[0]["event_id"] == "src-defect1:v1.9.0"
    assert jobs[0]["title"] == "Claude Code v1.9.0"

    # 2. Simulate resuming unfinished jobs in run_cycle independently of fetching
    from src.vendor_watch import runner
    # Mock load_sources to return empty list (simulating zero successful fetches)
    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # Job is resumed, enriched, placed into outbox, and marked completed
    pending_jobs_after = store.get_pending_processing_jobs(conn)
    assert len(pending_jobs_after) == 0

    pending_outbox = store.get_pending_outbox(conn)
    # The outbox item was delivered or stored
    assert len(delivered) > 0 or len(pending_outbox) > 0


def test_regression_defect_2_flush_digest_once_per_day_ordinary_runs(tmp_path: Path, monkeypatch):
    """Defect 2: 6-hour --once launcher flushes pending digest items once per day during ordinary runs."""
    from datetime import datetime
    from src.vendor_watch import runner

    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    # Put a pending digest item in outbox
    out_id = store.add_to_outbox(conn, "digest", "digest", "Dayjest xabari matni")
    assert store.get_app_state(conn, "last_digest_date") == ""

    delivered_items = []
    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None):
        delivered_items.append(text)
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Ordinary run with digest_mode=False (simulating standard 6-hour scheduled run)
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True)

    # Item must be delivered and last_digest_date updated to today
    assert len(delivered_items) == 1
    assert "Dayjest xabari" in delivered_items[0]
    today_str = datetime.now().date().isoformat()
    assert store.get_app_state(conn, "last_digest_date") == today_str

    # Second ordinary run on the same day should NOT flush digest again
    delivered_items.clear()
    store.add_to_outbox(conn, "digest2", "digest", "Ikkinchi dayjest matni")
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=False, once=True)
    assert len(delivered_items) == 0


def test_regression_defect_3_no_cut_inside_html_tags():
    """Defect 3: Truncating fields before escaping prevents broken HTML tags or entities."""
    long_raw = "Paragraf matni juda uzun. " * 200
    ev = {
        "vendor": "Anthropic",
        "product": "Claude Code",
        "triage_type": "breaking",
        "title": "Claude Code Major 2.0",
        "link": "https://github.com/anthropics/claude-code/releases/tag/v2.0.0",
        "links": ["https://github.com/anthropics/claude-code/releases/tag/v2.0.0"],
    }
    enr = {
        "uz_title": "Claude Code 2.0 Katta Reliz",
        "uz_summary": f"<b>Qalin</b> matn va {long_raw}",
        "uz_benefit": f"Foydali jihatlar: {long_raw}",
        "uz_how_to_use": f"npm install @anthropic/sdk-long-command-argument --save\n" * 30,
    }

    rendered = renderer.render_high_priority_message(ev, enr)
    assert len(rendered) <= 3900

    # Ensure all HTML tags are well-formed and paired (never truncated mid-tag)
    assert rendered.count("<b>") == rendered.count("</b>")
    assert rendered.count("<code>") == rendered.count("</code>")
    assert rendered.count("<i>") == rendered.count("</i>")
    assert rendered.count("<a href=") == rendered.count("</a>")
    # No trailing broken entity like '&am...' or unclosed tag like '<b'
    assert "<b\n" not in rendered
    assert "<code\n" not in rendered
    assert not rendered.endswith("<")


def test_regression_defect_4_merged_overflow_splits_without_dropping():
    """Defect 4: High-priority overflow > 3900 chars splits into several bounded messages containing EVERY event."""
    items = []
    for i in range(1, 26):
        ev = {
            "vendor": f"Vendor-{i}",
            "product": f"Product-{i}",
            "title": f"Update Title #{i} with meaningful long text description",
            "link": f"https://example.com/update-{i}",
        }
        enr = {
            "uz_title": f"Sarlavha #{i}",
            "uz_summary": f"Xulosa matni #{i}: muhim o'zgarishlar kiritildi va tafsilotlar quyidagicha " * 5,
            "uz_benefit": f"Foyda #{i}: tizim unumdorligi oshadi " * 3,
        }
        items.append((ev, enr))

    msgs = renderer.render_merged_high_priority(items)
    # Must be split into multiple messages
    assert isinstance(msgs, list)
    assert len(msgs) > 1

    # Every individual message must be <= 3900 chars
    for m in msgs:
        assert len(m) <= 3900

    # Every single event from 1 to 25 must appear in the combined messages (no dropped events)
    all_text = "\n".join(msgs)
    for i in range(1, 26):
        assert f"Vendor-{i}" in all_text
        assert f"Product-{i}" in all_text


def test_regression_defect_5_different_products_same_version_not_merged_and_max_severity():
    """Defect 5: Different products with same version within 24h must NOT merge, and genuine merges preserve maximum severity."""
    # 1. Different products from same vendor with same version
    ev_sdk = {
        "vendor": "OpenAI",
        "product": "OpenAI Python SDK",
        "title": "Release v2.1.0",
        "raw_content": "Critical security fix for CVE-2026-9999 authentication bypass vulnerability.",
        "published_at": "2026-10-28T00:00:00Z",
    }
    ev_cli = {
        "vendor": "OpenAI",
        "product": "Codex CLI",
        "title": "Codex CLI v2.1.0",
        "raw_content": "Added support for terminal animations.",
        "published_at": "2026-10-28T02:00:00Z",
    }
    clustered = triage.cluster_duplicates([ev_sdk, ev_cli])
    # They MUST NOT be merged
    assert len(clustered) == 2

    # 2. Genuine merge of the same entity keeps maximum severity and all content
    ev_note1 = {
        "vendor": "Google",
        "product": "Vertex AI Release Notes",
        "title": "Gemini 2.5 Flash GA in Vertex AI",
        "link": "https://cloud.google.com/vertex-ai/docs/release-notes#gemini-25",
        "raw_content": "Critical security fix for model sandbox isolation.",
        "published_at": "2026-10-28T00:00:00Z",
    }
    ev_note2 = {
        "vendor": "Google",
        "product": "Google Gemini Blog",
        "title": "Gemini 2.5 Flash updates and benchmarks",
        "link": "https://blog.google/products/gemini/gemini-2-5-flash-ga/",
        "raw_content": "Announcing Gemini 2.5 Flash benchmarks.",
        "published_at": "2026-10-28T04:00:00Z",
    }
    merged_cluster = triage.cluster_duplicates([ev_note1, ev_note2])
    assert len(merged_cluster) == 1
    m_item = merged_cluster[0]
    # Maximum severity (security) must be preserved
    assert m_item["triage_type"] == "security"
    assert m_item["priority"] == "high"
    # Both contents must be retained
    assert "benchmarks" in m_item["raw_content"]
    assert "Critical security fix" in m_item["raw_content"]


def test_regression_defect_6_cross_run_dedup_against_persisted_clusters(tmp_path: Path, monkeypatch):
    """Defect 6: Deduplication spans polling runs by comparing against persisted clusters from the last 24h."""
    from src.vendor_watch import runner

    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    # Run 1: Google Blog publishes Gemini 2.5 Flash announcement
    ev1 = parser.FeedEntry(
        entry_id="blog-gemini-25",
        title="Gemini 2.5 Flash GA in Vertex AI",
        link="https://blog.google/gemini-25",
        published_at="2026-10-28T01:00:00Z",
        summary="Gemini 2.5 Flash model release",
        raw_content="Announcing gemini-2.5-flash general availability.",
    )
    src_blog = {"id": "google-blog", "vendor": "Google", "product": "Google Blog"}
    store.record_unseen_entries(conn, src_blog, [ev1])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) >= 1

    # Run 2: 4 hours later, Vertex AI Release Notes publishes the same model release
    ev2 = parser.FeedEntry(
        entry_id="notes-gemini-25",
        title="Vertex AI Release: Gemini 2.5 Flash",
        link="https://cloud.google.com/vertex-ai/notes",
        published_at="2026-10-28T05:00:00Z",
        summary="Gemini 2.5 Flash GA",
        raw_content="gemini-2.5-flash is now GA in Vertex AI console.",
    )
    src_notes = {"id": "vertex-notes", "vendor": "Google", "product": "Vertex AI"}
    store.record_unseen_entries(conn, src_notes, [ev2])

    delivered.clear()
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # Should NOT trigger another duplicate notification
    assert len(delivered) == 0


def test_regression_defect_7_atom_and_rss_content_priority_and_revisions(tmp_path: Path):
    """Defect 7: Summary does not block reading full content, and content updates produce new revisions."""
    # 1. Atom feed with <summary> before <content>
    atom_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>atom-item-1</id>
        <title>Atom Release v1.0.0</title>
        <link href="https://example.com/v1"/>
        <summary>Short summary only</summary>
        <content type="text">Full comprehensive changelog content with all details</content>
      </entry>
    </feed>"""
    atom_entries = parser.parse_feed(atom_xml)
    assert len(atom_entries) == 1
    assert atom_entries[0].summary == "Short summary only"
    assert atom_entries[0].raw_content == "Full comprehensive changelog content with all details"

    # 2. RSS feed with <description> before <content:encoded>
    rss_xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel>
        <item>
          <guid>rss-item-1</guid>
          <title>RSS Release v1.0.0</title>
          <link>https://example.com/rss-1</link>
          <description>Brief RSS description</description>
          <content:encoded>Full RSS encoded rich body content</content:encoded>
        </item>
      </channel>
    </rss>"""
    rss_entries = parser.parse_feed(rss_xml)
    assert len(rss_entries) == 1
    assert rss_entries[0].summary == "Brief RSS description"
    assert rss_entries[0].raw_content == "Full RSS encoded rich body content"

    # 3. Content-only update produces a new revision even if summary is unchanged
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)
    src = {"id": "src-rev", "vendor": "V", "product": "P"}

    store.record_baseline(conn, src, [atom_entries[0]])

    updated_entry = parser.FeedEntry(
        entry_id="atom-item-1",
        title="Atom Release v1.0.0",
        link="https://example.com/v1",
        published_at=atom_entries[0].published_at,
        summary="Short summary only",
        raw_content="Updated full comprehensive changelog with critical patch notes",
    )
    unseen = store.record_unseen_entries(conn, src, [updated_entry])
    assert len(unseen) == 1
    assert unseen[0]["revision"] == 2


def test_regression_defect_8_dry_run_does_not_mutate_persistent_db(tmp_path: Path, monkeypatch):
    """Defect 8: Dry-run must not mutate persistent state (outbox status or records)."""
    from src.vendor_watch import runner

    db_path = tmp_path / "persistent.db"
    conn = store.get_connection(db_path)

    # Insert a real pending outbox item
    out_id = store.add_to_outbox(conn, "test-pending", "high", "Xabar matni")
    conn.close()

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Execute with dry_run=True
    runner.run_cycle(db_path=db_path, dry_run=True, once=True)

    # Re-open original persistent database and verify it was NEVER modified
    check_conn = store.get_connection(db_path)
    pending = store.get_pending_outbox(check_conn)
    assert len(pending) == 1
    assert pending[0]["id"] == out_id
    assert pending[0]["status"] == "pending"


def test_regression_defect_9_delivery_tracks_per_recipient(tmp_path: Path, monkeypatch):
    """Defect 9: Partial recipient failure tracks delivery per recipient and never re-sends to succeeded recipients."""
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    out_id = store.add_to_outbox(conn, "ev-9", "high", "Alert text")
    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-ok", "chat-err"])

    sent_to = []
    def mock_send(token, chat_id, text, timeout=15):
        if chat_id == "chat-err":
            raise RuntimeError("Telegram network drop for chat-err")
        sent_to.append(chat_id)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send)

    # 1. First attempt: chat-ok succeeds, chat-err fails
    success, err = delivery.deliver("Alert text", dry_run=False, conn=conn, outbox_id=out_id)
    assert success is False
    assert "chat-err" in err
    assert sent_to == ["chat-ok"]

    # 2. Retry attempt: only chat-err is attempted; chat-ok is NOT re-sent
    sent_to.clear()
    def mock_send_retry(token, chat_id, text, timeout=15):
        sent_to.append(chat_id)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_retry)
    success2, err2 = delivery.deliver("Alert text", dry_run=False, conn=conn, outbox_id=out_id)
    assert success2 is True
    assert sent_to == ["chat-err"]  # Only the failed recipient is retried!


def test_regression_defect_10_raw_mode_strips_html_tags():
    """Defect 10: Raw mode converts source HTML into plain text before excerpting, preventing escaped HTML in output."""
    ev = {
        "title": "Release v3.0.0",
        "vendor": "OpenAI",
        "product": "Codex",
        "raw_content": "<h3>New Features</h3><p>Support for <b>reasoning</b> streaming.</p>",
    }
    enr = enricher.make_raw_mode(ev)
    assert "<h3>" not in enr["uz_summary"]
    assert "New Features" in enr["uz_summary"]

    rendered = renderer.render_high_priority_message(ev, enr)
    # Output must NOT contain literal &lt;h3&gt;
    assert "&lt;h3&gt;" not in rendered
    assert "<h3>" not in rendered
    assert "New Features" in rendered


def test_regression_defect_11_prereleases_classified_as_noise_unless_security():
    """Defect 11: Pre-releases (-alpha, -beta, -rc) are noise: classified as prerelease, never immediate, excluded from digest unless security/breaking."""
    # 1. Standard pre-release without security or breaking changes
    ev_alpha = {
        "title": "Anthropic Claude Code 0.166.0-alpha.3",
        "vendor": "Anthropic",
        "product": "Claude Code",
        "raw_content": "Experimental preview testing new terminal hooks.",
    }
    t_type, prio = triage.classify_event(ev_alpha)
    assert t_type == "prerelease"
    assert prio not in ("high", "digest")

    ev_rc = {
        "title": "Codex CLI v2.0.0-rc.1",
        "vendor": "OpenAI",
        "product": "Codex CLI",
        "raw_content": "Release candidate preview for early adopters.",
    }
    t_type_rc, prio_rc = triage.classify_event(ev_rc)
    assert t_type_rc == "prerelease"
    assert prio_rc not in ("high", "digest")

    # 2. Pre-release containing security fix is promoted to security (high priority)
    ev_sec = {
        "title": "Claude Code 0.166.0-alpha.4",
        "vendor": "Anthropic",
        "product": "Claude Code",
        "raw_content": "Security fix for critical CVE-2026-8888 vulnerability.",
    }
    t_type_sec, prio_sec = triage.classify_event(ev_sec)
    assert t_type_sec == "security"
    assert prio_sec == "high"


def test_db_schema_migration_from_2401697(tmp_path: Path):
    """Verify that a database created with commit 2401697 schema is migrated with no data loss."""
    import sqlite3
    db_file = tmp_path / "legacy.db"

    # Create exact 2401697 schema
    legacy_conn = sqlite3.connect(str(db_file))
    legacy_conn.executescript("""
        CREATE TABLE sources_meta (
            source_id TEXT PRIMARY KEY,
            etag TEXT DEFAULT '',
            last_modified TEXT DEFAULT '',
            last_fetched_at TEXT DEFAULT '',
            last_status INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            is_baseline_done INTEGER DEFAULT 0
        );

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
        );

        CREATE TABLE outbox (
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

        INSERT INTO events (id, title, content_hash) VALUES ('legacy-ev-1', 'Legacy Title', 'hash123');
        INSERT INTO outbox (event_id, priority, rendered_text) VALUES ('legacy-ev-1', 'high', 'Legacy Render');
    """)
    legacy_conn.commit()
    legacy_conn.close()

    # Open with new store.get_connection
    migrated_conn = store.get_connection(db_file)

    # 1. Verify legacy data is preserved (no data loss)
    cur = migrated_conn.cursor()
    cur.execute("SELECT title FROM events WHERE id = 'legacy-ev-1'")
    assert cur.fetchone()["title"] == "Legacy Title"

    cur.execute("SELECT rendered_text FROM outbox WHERE event_id = 'legacy-ev-1'")
    assert cur.fetchone()["rendered_text"] == "Legacy Render"

    # 2. Verify new tables are successfully created
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='processing_jobs'")
    assert cur.fetchone() is not None

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='app_state'")
    assert cur.fetchone() is not None

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_deliveries'")
    assert cur.fetchone() is not None


def test_regression_p1_crash_recovery_latest_revision_and_highest_severity(tmp_path: Path, monkeypatch):
    """P1: Resumed crash-recovery processes the LATEST revision of an event and preserves the highest severity."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    # Ingestion: revision 1 pending
    source = {"id": "src-p1", "vendor": "Anthropic", "product": "Claude Code"}
    ev_rev1 = parser.FeedEntry(
        entry_id="v1.0.0",
        title="Claude Code v1.0.0",
        link="https://github.com/anthropics/claude-code/releases/tag/v1.0.0",
        published_at="2026-10-28T00:00:00Z",
        summary="Minor patch fixes",
        raw_content="Routine bug fixes with no security issues.",
    )
    store.record_unseen_entries(conn, source, [ev_rev1])

    # Pending processing job exists for revision 1
    jobs = store.get_pending_processing_jobs(conn)
    assert len(jobs) == 1
    assert jobs[0]["revision"] == 1

    # Simulate fetch discovering revision 2 with critical security fix
    mock_src = dict(source)
    mock_src["url"] = "https://example.com/feed.atom"
    monkeypatch.setattr(runner, "load_sources", lambda: [mock_src])

    from src.vendor_watch import fetcher
    mock_atom = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>v1.0.0</id>
        <title>Claude Code v1.0.0</title>
        <link href="https://github.com/anthropics/claude-code/releases/tag/v1.0.0"/>
        <summary>Security update</summary>
        <content type="text">Critical security fix for CVE-2026-7777 vulnerability.</content>
      </entry>
    </feed>"""
    monkeypatch.setattr(fetcher, "fetch_feed", lambda src, snapshot=None, fixtures_dir=None: fetcher.FetchResult(status=200, content=mock_atom.encode("utf-8")))

    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # Job is completed
    assert len(store.get_pending_processing_jobs(conn)) == 0
    # Immediate high priority notification was sent for the security fix
    assert len(delivered) >= 1
    assert any("CVE-2026-7777" in d or "Xavfsizlik" in d or "security" in d.lower() for d in delivered)


def test_regression_p3_digest_rendering_does_not_truncate_html_with_huge_url():
    """P3: Digest rendering with a 4,500-char URL does not truncate HTML mid-tag and keeps valid HTML <= 3900 chars."""
    huge_url = "https://example.com/release/" + ("a" * 4500)
    ev = {
        "vendor": "Google",
        "product": "Gemini CLI",
        "title": "Gemini CLI v1.5.0",
        "link": huge_url,
        "links": [huge_url],
    }
    enr = {
        "uz_title": "Gemini CLI v1.5.0 Yangilanishi",
        "uz_summary": "Yangi qulayliklar qo'shildi va tizim barqarorligi oshirildi.",
    }
    msgs = renderer.render_digest_messages([(ev, enr)])
    assert len(msgs) == 1
    msg = msgs[0]
    assert len(msg) <= 3900
    assert not msg.endswith("<a href=\"")
    assert "<a href=" not in msg or msg.count("<a href=") == msg.count("</a>")
    assert "Havola:" in msg


def test_regression_p4_merged_item_with_huge_url_bounded():
    """P4: Merged high-priority item with a 4,500-char URL stays bounded <= 3900 chars with valid HTML."""
    huge_url = "https://example.com/security-bulletin/" + ("x" * 4500)
    ev = {
        "vendor": "Anthropic",
        "product": "Claude Code",
        "title": "Claude Code Security Fix",
        "link": huge_url,
        "links": [huge_url],
    }
    enr = {
        "uz_title": "Claude Code Xavfsizlik Tuzatishi",
        "uz_summary": "Muhim xavfsizlik yangilanishi e'lon qilindi.",
        "uz_benefit": "Tizim himoyasi ta'minlanadi.",
    }
    msgs = renderer.render_merged_high_priority([(ev, enr)])
    assert len(msgs) == 1
    msg = msgs[0]
    assert len(msg) <= 3900
    assert msg.count("<b>") == msg.count("</b>")
    assert msg.count("<a href=") == msg.count("</a>")


def test_regression_p5_runner_reclassification_preserves_merged_severity(tmp_path: Path, monkeypatch):
    """P5: Runner re-classification does not discard merged severity (v1.2.3 + v1.2.3 security fix remains high)."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    ev_normal = parser.FeedEntry(
        entry_id="v1.2.3",
        title="Release v1.2.3",
        link="https://example.com/v123",
        published_at="2026-10-28T00:00:00Z",
        summary="Minor maintenance release",
        raw_content="General improvements.",
    )
    ev_security = parser.FeedEntry(
        entry_id="v1.2.3-sec",
        title="Release v1.2.3",
        link="https://example.com/v123-sec",
        published_at="2026-10-28T02:00:00Z",
        summary="Security bulletin",
        raw_content="Critical security fix for CVE-2026-5555 vulnerability.",
    )
    src = {"id": "src-p5", "vendor": "OpenAI", "product": "OpenAI Python SDK"}
    store.record_unseen_entries(conn, src, [ev_normal, ev_security])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # Merged cluster must be delivered immediately as high priority, not downgraded to digest
    assert len(delivered) >= 1
    assert any("CVE-2026-5555" in d or "Xavfsizlik" in d or "security" in d.lower() for d in delivered)


def test_regression_p6_cross_run_dedup_for_digest_and_overflow_member_events(tmp_path: Path, monkeypatch):
    """P6: Cross-run dedup recognizes digest member events as notified and prevents duplicate alerts."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    # Run 1: SDK patch delivered in digest
    ev1 = parser.FeedEntry(
        entry_id="v1.5.1",
        title="Release v1.5.1",
        link="https://example.com/v151",
        published_at="2026-10-28T01:00:00Z",
        summary="Patch release",
        raw_content="Bug fix for connection retry.",
    )
    src1 = {"id": "src-feed-1", "vendor": "OpenAI", "product": "OpenAI Python SDK"}
    store.record_unseen_entries(conn, src1, [ev1])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)
    assert len(delivered) == 1

    # Run 2: Another source announces same v1.5.1 4 hours later
    ev2 = parser.FeedEntry(
        entry_id="news-v1.5.1",
        title="OpenAI Python SDK v1.5.1",
        link="https://example.com/news-v151",
        published_at="2026-10-28T05:00:00Z",
        summary="OpenAI Python SDK patch announcement",
        raw_content="v1.5.1 is now released.",
    )
    src2 = {"id": "src-feed-2", "vendor": "OpenAI", "product": "OpenAI Python SDK"}
    store.record_unseen_entries(conn, src2, [ev2])

    delivered.clear()
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)

    # Must be deduplicated against the earlier digest notification!
    assert len(delivered) == 0


def test_regression_p8_dry_run_on_legacy_2401697_db_does_not_modify_disk_file(tmp_path: Path, monkeypatch):
    """P8: Dry-run on a legacy (commit 2401697) DB copies first and never creates tables on disk."""
    import sqlite3
    from src.vendor_watch import runner
    db_file = tmp_path / "legacy_readonly.db"

    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
        CREATE TABLE sources_meta (
            source_id TEXT PRIMARY KEY,
            etag TEXT DEFAULT '',
            last_modified TEXT DEFAULT '',
            last_fetched_at TEXT DEFAULT '',
            last_status INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            is_baseline_done INTEGER DEFAULT 0
        );
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
        );
        CREATE TABLE outbox (
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
        INSERT INTO events (id, title, content_hash) VALUES ('ev-1', 'Legacy Title', 'hash-1');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Execute with dry_run=True
    runner.run_cycle(db_path=db_file, dry_run=True, once=True)

    # Verify on-disk schema has NOT been altered: still exactly 3 tables
    check_conn = sqlite3.connect(str(db_file))
    cur = check_conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall() if not r[0].startswith("sqlite_")]
    check_conn.close()

    assert sorted(tables) == ["events", "outbox", "sources_meta"]


def test_regression_p11_prerelease_with_deprecation_is_not_immediate_and_filtered_from_baseline_digest(tmp_path: Path, monkeypatch):
    """P11: v1.2.3-alpha.1 containing 'Deprecated' is classified as prerelease/ignore, and alpha entries are excluded from --baseline-digest."""
    from src.vendor_watch import runner
    # 1. Classification check
    ev_alpha = {
        "title": "Anthropic Claude Code v1.2.3-alpha.1",
        "raw_content": "This experimental API is deprecated and will be removed in next alpha.",
    }
    t_type, prio = triage.classify_event(ev_alpha)
    assert t_type == "prerelease"
    assert prio == "ignore"

    # 2. Baseline digest check
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)
    entry_alpha = parser.FeedEntry(
        entry_id="v1.2.3-alpha.1",
        title="Anthropic Claude Code v1.2.3-alpha.1",
        link="https://example.com/alpha1",
        published_at="2026-10-28T00:00:00Z",
        summary="Alpha preview",
        raw_content="Deprecated endpoint inside alpha.",
    )
    src = {"id": "src-alpha", "vendor": "Anthropic", "product": "Claude Code"}
    store.record_baseline(conn, src, [entry_alpha])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, baseline_digest=True, once=True)

    # Baseline digest must NOT contain alpha entry
    assert len(delivered) == 0


def test_regression_n1_crash_after_send_before_job_completion_skips_sent_recipients(tmp_path: Path, monkeypatch):
    """N1: Crash after send but before job completion skips recipients already marked sent on restart."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    # Ingest event
    entry = parser.FeedEntry(
        entry_id="v2.0.0",
        title="Codex CLI v2.0.0 Major",
        link="https://example.com/v2",
        published_at="2026-10-28T00:00:00Z",
        summary="Breaking release",
        raw_content="Breaking change details.",
    )
    src = {"id": "src-n1", "vendor": "OpenAI", "product": "Codex CLI"}
    store.record_unseen_entries(conn, src, [entry])

    monkeypatch.setenv("BOT_TOKEN", "mock-token")
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-1", "chat-2"])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    sent_log = []
    def mock_send(token, chat_id, text, timeout=15):
        sent_log.append(chat_id)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send)

    # Simulate run that sends to both chats, marks deliveries sent in outbox, but crashes before mark_processing_job_done
    out_id = store.add_to_outbox(conn, "src-n1:v2.0.0", "high", "Alert text")
    delivery.deliver("Alert text", dry_run=False, conn=conn, outbox_id=out_id)
    store.update_outbox_status(conn, out_id, "sent")
    assert sent_log == ["chat-1", "chat-2"]
    # Job remains pending (simulating crash before completion)
    assert len(store.get_pending_processing_jobs(conn)) == 1

    # Restart run_cycle
    sent_log.clear()
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # Nothing should be re-sent to chat-1 or chat-2!
    assert len(sent_log) == 0
    # Job is now completed
    assert len(store.get_pending_processing_jobs(conn)) == 0


def test_regression_n2_all_cluster_member_jobs_completed(tmp_path: Path, monkeypatch):
    """N2: All members of a cluster have their processing jobs marked completed (not just primary)."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    ev1 = parser.FeedEntry(
        entry_id="gemini-flash-blog",
        title="Gemini 2.5 Flash GA in Vertex AI",
        link="https://blog.google/gemini-25",
        published_at="2026-10-28T01:00:00Z",
        summary="Gemini 2.5 Flash GA",
        raw_content="gemini-2.5-flash general availability announcement.",
    )
    ev2 = parser.FeedEntry(
        entry_id="gemini-flash-notes",
        title="Vertex AI Release: Gemini 2.5 Flash",
        link="https://cloud.google.com/vertex/gemini-25",
        published_at="2026-10-28T02:00:00Z",
        summary="Gemini 2.5 Flash release notes",
        raw_content="gemini-2.5-flash GA in Vertex console.",
    )
    src1 = {"id": "src-blog", "vendor": "Google", "product": "Vertex AI"}
    src2 = {"id": "src-notes", "vendor": "Google", "product": "Vertex AI"}

    store.record_unseen_entries(conn, src1, [ev1])
    store.record_unseen_entries(conn, src2, [ev2])

    assert len(store.get_pending_processing_jobs(conn)) == 2

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)

    # Both cluster members must be completed
    assert len(store.get_pending_processing_jobs(conn)) == 0


def test_regression_n3_stable_release_referencing_prerelease_in_body_is_not_prerelease():
    """N3: Stable release whose body says 'Upgrade from v1.2.3-beta.1' is classified as stable, never prerelease."""
    ev = {
        "title": "Release v1.2.3",
        "entry_id": "v1.2.3",
        "raw_content": "Upgrade guide: Migration from v1.2.3-beta.1 to stable v1.2.3 release.",
    }
    t_type, prio = triage.classify_event(ev)
    assert t_type != "prerelease"
    assert prio != "ignore"
    assert t_type == "sdk_patch"
    assert prio == "digest"


def test_regression_n4_removed_recipient_not_contacted_on_retry(tmp_path: Path, monkeypatch):
    """N4: A recipient removed from configuration is NOT contacted on retry."""
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    out_id = store.add_to_outbox(conn, "ev-n4", "high", "Alert text")
    monkeypatch.setenv("BOT_TOKEN", "mock-token")

    # Initial delivery attempt: chat-ok succeeds, chat-removed fails
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-ok", "chat-removed"])
    def mock_send_1(token, chat_id, text, timeout=15):
        if chat_id == "chat-removed":
            raise RuntimeError("Temporary Telegram outage for chat-removed")

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_1)
    delivery.deliver("Alert text", dry_run=False, conn=conn, outbox_id=out_id)

    # Configuration changes: chat-removed is removed from config!
    monkeypatch.setattr(delivery, "get_target_chat_ids", lambda: ["chat-ok"])

    sent_on_retry = []
    def mock_send_retry(token, chat_id, text, timeout=15):
        sent_on_retry.append(chat_id)

    monkeypatch.setattr(delivery, "send_to_telegram", mock_send_retry)

    # Retry delivery
    success, err = delivery.deliver("Alert text", dry_run=False, conn=conn, outbox_id=out_id)
    assert success is True
    # chat-removed must NOT be contacted!
    assert "chat-removed" not in sent_on_retry
    assert sent_on_retry == []


def test_regression_n5_legacy_db_2401697_upgrade_unchanged_feed_zero_new_events(tmp_path: Path):
    """N5: An existing DB created by commit 2401697 is upgraded without replaying seen entries when parsing an unchanged feed."""
    import sqlite3
    db_file = tmp_path / "legacy_2401697.db"

    # Create exact 2401697 legacy DB with an Atom feed entry parsed by 2401697
    atom_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>tag:github.com,2008:Repository/12345/v1.0.0</id>
        <title>Release v1.0.0</title>
        <link href="https://example.com/v1"/>
        <summary type="html">&lt;p&gt;Initial summary text with html&lt;/p&gt;</summary>
      </entry>
    </feed>"""

    # In 2401697, raw_content was '<p>Initial summary text with html</p>' and title 'Release v1.0.0'
    legacy_content = "<p>Initial summary text with html</p>"
    legacy_hash = store.make_content_hash("Release v1.0.0", legacy_content)
    event_id = "src-atom:tag:github.com,2008:Repository/12345/v1.0.0"

    conn = sqlite3.connect(str(db_file))
    conn.executescript(f"""
        CREATE TABLE sources_meta (
            source_id TEXT PRIMARY KEY,
            etag TEXT DEFAULT '',
            last_modified TEXT DEFAULT '',
            last_fetched_at TEXT DEFAULT '',
            last_status INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            is_baseline_done INTEGER DEFAULT 1
        );
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
            status TEXT DEFAULT 'baseline',
            first_seen_at TEXT,
            last_seen_at TEXT
        );
        CREATE TABLE outbox (
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
        INSERT INTO sources_meta (source_id, is_baseline_done) VALUES ('src-atom', 1);
        INSERT INTO events (id, source_id, entry_id, title, content_hash, raw_content, status)
        VALUES ('{event_id}', 'src-atom', 'tag:github.com,2008:Repository/12345/v1.0.0', 'Release v1.0.0', '{legacy_hash}', '{legacy_content}', 'baseline');
    """)
    conn.commit()
    conn.close()

    # Open and upgrade DB with new code
    upgraded_conn = store.get_connection(db_file)

    # Parse the exact same unchanged feed XML using current parser
    entries = parser.parse_feed(atom_xml)
    source = {"id": "src-atom", "vendor": "Anthropic", "product": "Claude Code"}

    # Must produce ZERO new events or revisions
    unseen = store.record_unseen_entries(upgraded_conn, source, entries)
    assert len(unseen) == 0

    # Ensure no processing jobs were queued
    jobs = store.get_pending_processing_jobs(upgraded_conn)
    assert len(jobs) == 0


def test_regression_astra_p1_revision_outbox_not_superseded_by_old_revision(tmp_path: Path, monkeypatch):
    """P1 / Topilma 1: Rev 1 sent bo'lgach, rev 2 xavfsizlik tuzatishi ham alohida outbox yaratib yuboriladi."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    source = {"id": "src-rev-test", "vendor": "Anthropic", "product": "Claude Code"}
    ev_rev1 = parser.FeedEntry(
        entry_id="v2.0.0",
        title="Claude Code v2.0.0",
        link="https://example.com/v200",
        published_at="2026-10-28T00:00:00Z",
        summary="Major release",
        raw_content="Breaking changes in API.",
    )
    store.record_unseen_entries(conn, source, [ev_rev1])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    # 1-run: Rev 1 is sent
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 1
    assert "v2.0.0" in delivered[0]

    # Now feed updates to Rev 2 with a critical security fix
    ev_rev2 = parser.FeedEntry(
        entry_id="v2.0.0",
        title="Claude Code v2.0.0 (Security Patch)",
        link="https://example.com/v200-sec",
        published_at="2026-10-28T02:00:00Z",
        summary="Security update",
        raw_content="Critical security fix for CVE-2026-9999 vulnerability.",
    )
    store.record_unseen_entries(conn, source, [ev_rev2])

    delivered.clear()
    # 2-run: Rev 2 must NOT be skipped as already sent!
    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) == 1
    assert "CVE-2026-9999" in delivered[0] or "Xavfsizlik" in delivered[0] or "security" in delivered[0].lower()


def test_regression_astra_p3_p4_escaped_ampersand_and_quote_url_bounded():
    """P3/P4 / Topilma 3: 471 belgili '&' va '\"' ko'p URL bilan high, merged va digest xabarlari <= 3900 chars va valid HTML."""
    base_prefix = "https://example.com/search?q="
    repeats = 25
    query_part = "".join(f'tag%22{i}%22=&param_{i}="val_{i}"&' for i in range(repeats))
    long_url = (base_prefix + query_part)[:471]
    assert len(long_url) == 471
    assert "&" in long_url and '"' in long_url

    ev = {
        "id": "ev-url-bound",
        "vendor": "Anthropic",
        "product": "Claude Code",
        "title": 'Release v2.0.0 "Major & Breaking"',
        "link": long_url,
        "links": [long_url],
        "triage_type": "security",
        "priority": "high",
    }
    enr = {
        "uz_title": 'Claude Code Yangilanishi "Xavfsizlik & Barqarorlik"',
        "uz_summary": 'Xavfsizlik bo\'yicha "muhim" yangilanish & tuzatishlar kiritildi.',
        "uz_benefit": 'Foydalanuvchi tizimi himoyalanadi & barqaror ishlaydi.',
        "uz_how_to_use": 'claude update --force="all" & restart',
    }

    # 1. High priority single message
    high_msg = renderer.render_high_priority_message(ev, enr)
    assert len(high_msg) <= 3900
    assert high_msg.count("<a href=") == high_msg.count("</a>")
    assert high_msg.count("<b>") == high_msg.count("</b>")

    # 2. Merged high priority messages
    merged_items = [(ev, enr) for _ in range(3)]
    merged_msgs = renderer.render_merged_high_priority(merged_items)
    for m in merged_msgs:
        assert len(m) <= 3900
        assert m.count("<a href=") == m.count("</a>")
        assert m.count("<b>") == m.count("</b>")

    # 3. Digest messages
    digest_items = [(ev, enr) for _ in range(5)]
    digest_msgs = renderer.render_digest_messages(digest_items)
    for d in digest_msgs:
        assert len(d) <= 3900
        assert d.count("<a href=") == d.count("</a>")
        assert d.count("<b>") == d.count("</b>")


def test_regression_astra_n1_digest_crash_restart_does_not_resend(tmp_path: Path, monkeypatch):
    """N1 / Topilma 2: Digest yuborilgach, job yopilishidan oldin crash bo'lsa, restartda qayta yubormaydi."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    ev1 = parser.FeedEntry(
        entry_id="patch-1",
        title="OpenAI Python SDK v1.50.1",
        link="https://example.com/p1",
        published_at="2026-10-28T01:00:00Z",
        summary="SDK patch 1",
        raw_content="Routine patch 1.",
    )
    ev2 = parser.FeedEntry(
        entry_id="patch-2",
        title="Google GenAI Python SDK v0.4.1",
        link="https://example.com/p2",
        published_at="2026-10-28T02:00:00Z",
        summary="SDK patch 2",
        raw_content="Routine patch 2.",
    )
    store.record_unseen_entries(conn, {"id": "src-1", "vendor": "OpenAI", "product": "SDK"}, [ev1])
    store.record_unseen_entries(conn, {"id": "src-2", "vendor": "Google", "product": "SDK"}, [ev2])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    sent_messages = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (sent_messages.append(text) or True, ""))

    # Simulate: digest is rendered and sent, outbox is updated to 'sent', but crash occurs before mark_processing_job_done
    digest_pages = renderer.render_digest_pages([
        ({"id": "src-1:patch-1", "vendor": "OpenAI", "product": "SDK", "title": ev1.title, "link": ev1.link, "links": [ev1.link]}, {"uz_title": ev1.title, "uz_summary": ev1.summary}),
        ({"id": "src-2:patch-2", "vendor": "Google", "product": "SDK", "title": ev2.title, "link": ev2.link, "links": [ev2.link]}, {"uz_title": ev2.title, "uz_summary": ev2.summary}),
    ])
    for d_msg, page_items in digest_pages:
        page_events = [it[0] for it in page_items]
        out_id = store.add_to_outbox(conn, "digest", "digest", d_msg)
        store.link_outbox_events(conn, out_id, page_events)
        store.update_outbox_status(conn, out_id, "sent")

    assert len(store.get_pending_processing_jobs(conn)) == 2

    sent_messages.clear()
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)

    # Must NOT re-send digest message!
    assert len(sent_messages) == 0
    # Jobs must now be completed
    assert len(store.get_pending_processing_jobs(conn)) == 0


def test_regression_astra_p6_failed_digest_retried_sent_cross_run_dedup(tmp_path: Path, monkeypatch):
    """P6 / Topilma 5: Dastlab failed bo'lgan digest retryda sent bo'lsa, event_notifications 'sent' bo'ladi va boshqa manbadagi ayni reliz qayta yuborilmaydi."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    ev1 = parser.FeedEntry(
        entry_id="v1.60.0",
        title="Release v1.60.0",
        link="https://example.com/v160",
        published_at="2026-10-28T01:00:00Z",
        summary="SDK minor patch",
        raw_content="SDK patch v1.60.0 release notes.",
    )
    src1 = {"id": "src-p6-a", "vendor": "OpenAI", "product": "OpenAI Python SDK"}
    store.record_unseen_entries(conn, src1, [ev1])

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Run 1: Telegram send FAILS
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (False, "Network error"))
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)

    cur = conn.cursor()
    cur.execute("SELECT status FROM event_notifications WHERE event_id = 'src-p6-a:v1.60.0'")
    row = cur.fetchone()
    assert row is not None and row[0] == "failed"

    # Run 2: Retry succeeds!
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)
    assert len(delivered) == 1

    cur.execute("SELECT status FROM event_notifications WHERE event_id = 'src-p6-a:v1.60.0'")
    row = cur.fetchone()
    assert row is not None and row[0] == "sent"

    # Run 3: Same release announced from another source 3 hours later
    ev2 = parser.FeedEntry(
        entry_id="news-v1.60.0",
        title="OpenAI Python SDK v1.60.0",
        link="https://example.com/news-160",
        published_at="2026-10-28T04:00:00Z",
        summary="SDK update",
        raw_content="v1.60.0 announcement.",
    )
    src2 = {"id": "src-p6-b", "vendor": "OpenAI", "product": "OpenAI Python SDK"}
    store.record_unseen_entries(conn, src2, [ev2])

    delivered.clear()
    runner.run_cycle(db_path=db_path, dry_run=False, digest_mode=True, once=True)

    # Must be deduplicated against the retried 'sent' notification!
    assert len(delivered) == 0


def test_regression_astra_p6_multipage_digest_per_page_notification_status(tmp_path: Path, monkeypatch):
    """Topilma 6: Multipage digestda har sahifaning yetkazilish natijasi faqat o'z a'zolariga yoziladi."""
    from src.vendor_watch import runner
    db_path = tmp_path / "test.db"
    conn = store.get_connection(db_path)

    # Ingest 20 entries with substantial summaries to force multiple digest pages
    for i in range(20):
        ev = parser.FeedEntry(
            entry_id=f"entry-{i}",
            title=f"Release v{i}.0.0 with multiple new features and migration updates",
            link=f"https://example.com/entry/{i}",
            published_at="2026-10-28T01:00:00Z",
            summary="Summary text " * 30,
            raw_content="Detailed content " * 40,
        )
        store.record_unseen_entries(conn, {"id": f"src-multi-{i}", "vendor": f"Vendor {i}", "product": "SDK"}, [ev])

    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Delivery mock: page 1 fails, page 2 succeeds
    delivery_attempts = []
    def mock_deliver(text, dry_run=False, conn=None, outbox_id=None):
        idx = len(delivery_attempts)
        delivery_attempts.append(outbox_id)
        if idx == 0:
            return False, "Page 1 temporary failure"
        return True, ""

    monkeypatch.setattr(delivery, "deliver", mock_deliver)

    runner.run_cycle(db_path=db_path, dry_run=False, fixtures=True, digest_mode=True, once=True)

    assert len(delivery_attempts) >= 2

    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM event_notifications GROUP BY status")
    counts = dict(cur.fetchall())
    assert "failed" in counts
    assert "sent" in counts
    assert counts["failed"] > 0
    assert counts["sent"] > 0
    assert counts["failed"] + counts["sent"] == 20


def test_regression_astra_n5_atom_and_rss_summary_before_content_legacy_hash_upgrade(tmp_path: Path):
    """N5 / Topilma 4: Haqiqiy Atom (<summary> oldin) va RSS (<description> oldin) feedlari 2401697 DB bilan 0 yangi event beradi."""
    import sqlite3
    db_file = tmp_path / "legacy_2401697_real.db"

    atom_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>tag:github.com,2008:Repository/atom1</id>
        <title>Release v1.0.0</title>
        <link href="https://example.com/atom1"/>
        <summary>Short summary text</summary>
        <content>Full detailed content of the release notes</content>
      </entry>
    </feed>"""

    rss_xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel>
        <title>Test RSS</title>
        <item>
          <guid>rss-guid-1</guid>
          <title>RSS Release v2.0.0</title>
          <link>https://example.com/rss1</link>
          <description>Short description text</description>
          <content:encoded>Full encoded content in CDATA or text</content:encoded>
        </item>
      </channel>
    </rss>"""

    atom_legacy_content = "Short summary text"
    atom_legacy_hash = store.make_content_hash("Release v1.0.0", atom_legacy_content)
    atom_event_id = "src-atom:tag:github.com,2008:Repository/atom1"

    rss_legacy_content = "Short description text"
    rss_legacy_hash = store.make_content_hash("RSS Release v2.0.0", rss_legacy_content)
    rss_event_id = "src-rss:rss-guid-1"

    conn = sqlite3.connect(str(db_file))
    conn.executescript(f"""
        CREATE TABLE sources_meta (
            source_id TEXT PRIMARY KEY,
            etag TEXT DEFAULT '',
            last_modified TEXT DEFAULT '',
            last_fetched_at TEXT DEFAULT '',
            last_status INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            is_baseline_done INTEGER DEFAULT 1
        );
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
            status TEXT DEFAULT 'baseline',
            first_seen_at TEXT,
            last_seen_at TEXT
        );
        CREATE TABLE outbox (
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
        INSERT INTO sources_meta (source_id, is_baseline_done) VALUES ('src-atom', 1), ('src-rss', 1);
        INSERT INTO events (id, source_id, entry_id, title, content_hash, raw_content, status)
        VALUES ('{atom_event_id}', 'src-atom', 'tag:github.com,2008:Repository/atom1', 'Release v1.0.0', '{atom_legacy_hash}', '{atom_legacy_content}', 'baseline'),
               ('{rss_event_id}', 'src-rss', 'rss-guid-1', 'RSS Release v2.0.0', '{rss_legacy_hash}', '{rss_legacy_content}', 'baseline');
    """)
    conn.commit()
    conn.close()

    upgraded_conn = store.get_connection(db_file)

    atom_entries = parser.parse_feed(atom_xml)
    atom_unseen = store.record_unseen_entries(upgraded_conn, {"id": "src-atom", "vendor": "Anthropic", "product": "SDK"}, atom_entries)
    assert len(atom_unseen) == 0

    rss_entries = parser.parse_feed(rss_xml)
    rss_unseen = store.record_unseen_entries(upgraded_conn, {"id": "src-rss", "vendor": "Google", "product": "SDK"}, rss_entries)
    assert len(rss_unseen) == 0

    assert len(store.get_pending_processing_jobs(upgraded_conn)) == 0

