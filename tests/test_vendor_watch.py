"""Unit and integration tests for vendor watch MVP."""

from pathlib import Path
import pytest

from src.vendor_watch import delivery, enricher, fetcher, parser, renderer, store, triage


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
