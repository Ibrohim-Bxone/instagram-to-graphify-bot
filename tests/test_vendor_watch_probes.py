"""Tests for Tier-3 Account Probes in Vendor Watch.

Covers:
1. Baseline on first successful probe.
2. Model added detection and high-priority Uzbek notification.
3. Model removed detection and high-priority Uzbek notification.
4. Probe failure (timeout, network, auth) does NOT remove models.
5. Changelog new version detection (digest priority).
6. CLI outdated vs latest release (digest priority).
7. Vertex AI 404 -> 200 transition (model newly available).
8. Vertex AI failure (timeout / 500) does not produce removal.
9. Full runner integration with --probes-only and --once.
"""

import html
from pathlib import Path
import pytest

from src.vendor_watch import delivery, enricher, parser, probes, renderer, runner, store, triage


@pytest.fixture(autouse=True)
def _block_real_vertex(monkeypatch):
    # Testlar haqiqiy Vertex'ga chiqmasin; kerakli testlar o'z mock'ini ustiga qo'yadi
    from src import vertex

    def _blocked(*args, **kwargs):
        raise RuntimeError("Real Vertex call blocked in tests")

    monkeypatch.setattr(vertex, "chat_json", _blocked)


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_probes.db"
    conn = store.get_connection(db_file)
    yield conn
    conn.close()


def test_probe_baseline(test_db):
    """First probe execution establishes baseline in SQLite without generating spurious events."""
    canned_config = {
        "agy_models": {"enabled": True},
        "agy_changelog": {"enabled": True},
        "cli_versions": {"enabled": True},
        "vertex_models": {
            "enabled": True,
            "project": "project-test",
            "location": "global",
            "candidate_models": ["gemini-2.5-flash", "gemini-4-argon"],
        },
    }

    # First run: should establish baseline and return 0 events
    events = probes.run_all_probes(test_db, config=canned_config, fixtures=True)
    assert events == []

    # Verify snapshots exist in SQLite
    snap_models = store.get_latest_probe_snapshot(test_db, "agy_models")
    assert snap_models is not None
    assert "gemini-3.8-flash-high" in snap_models["data"]["model_ids"]

    snap_vertex = store.get_latest_probe_snapshot(test_db, "vertex_models")
    assert snap_vertex is not None
    assert "gemini-4-argon" in snap_vertex["data"]["available"]


def test_probe_model_added(test_db):
    """When a new model is listed in agy models, high-priority account_change event is emitted."""
    # Pre-seed baseline snapshot with 2 models
    baseline_data = {
        "models": {
            "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
            "claude-sonnet-4-6": "Claude Sonnet 4.6",
        },
        "model_ids": ["claude-sonnet-4-6", "gemini-3.8-flash-medium"],
    }
    store.save_probe_snapshot(test_db, "agy_models", {"status": "success", "data": baseline_data})

    # Current probe returns 3 models (gemini-4-argon added)
    current_data = {
        "models": {
            "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
            "claude-sonnet-4-6": "Claude Sonnet 4.6",
            "gemini-4-argon": "Gemini 4 Argon",
        },
        "model_ids": ["claude-sonnet-4-6", "gemini-3.8-flash-medium", "gemini-4-argon"],
    }

    events = probes.diff_agy_models({"status": "success", "data": current_data}, baseline_data)
    assert len(events) == 1
    ev = events[0]

    assert ev["triage_type"] == "account_change"
    assert ev["priority"] == "high"
    assert "Sizning akkauntingizda: gemini-4-argon paydo bo'ldi" in ev["title"]
    assert "agy --model gemini-4-argon" in ev["command"]

    # Render message and verify Uzbek structure and stack impact
    rendered = renderer.render_account_change_message(ev)
    unescaped = html.unescape(rendered)
    assert "🔔 <b>Sizning akkauntingizda: gemini-4-argon paydo bo'ldi</b>" in unescaped
    assert "argon-ish.py" in rendered
    assert "<code>agy --model gemini-4-argon</code>" in rendered
    assert len(rendered) <= 3900


def test_probe_model_removed(test_db):
    """When a previously available model disappears, high-priority removal event is emitted."""
    baseline_data = {
        "models": {
            "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
            "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
        },
        "model_ids": ["gemini-3.1-pro-high", "gemini-3.8-flash-high"],
    }
    store.save_probe_snapshot(test_db, "agy_models", {"status": "success", "data": baseline_data})

    # Current probe has only gemini-3.8-flash-high
    current_data = {
        "models": {"gemini-3.8-flash-high": "Gemini 3.8 Flash (High)"},
        "model_ids": ["gemini-3.8-flash-high"],
    }

    events = probes.diff_agy_models({"status": "success", "data": current_data}, baseline_data)
    assert len(events) == 1
    ev = events[0]

    assert ev["triage_type"] == "account_change"
    assert ev["priority"] == "high"
    assert "Sizning akkauntingizda: gemini-3.1-pro-high yo'qoldi" in ev["title"]

    rendered = renderer.render_account_change_message(ev)
    unescaped = html.unescape(rendered)
    assert "🔔 <b>Sizning akkauntingizda: gemini-3.1-pro-high yo'qoldi</b>" in unescaped
    assert "Antigravity Switcher" in rendered or "agy-run.py" in rendered
    assert len(rendered) <= 3900


def test_probe_failure_does_not_remove_model(test_db, monkeypatch):
    """A subprocess timeout, network failure or auth error NEVER produces 'removed' events."""
    baseline_data = {
        "models": {
            "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
            "claude-opus-4-6-thinking": "Claude Opus 4.6 Thinking",
        },
        "model_ids": ["claude-opus-4-6-thinking", "gemini-3.8-flash-high"],
    }
    store.save_probe_snapshot(test_db, "agy_models", {"status": "success", "data": baseline_data})

    # Simulate command timeout / failure
    def mock_run_command(*args, **kwargs):
        return -1, "", "Command timed out after 15s: agy models"

    monkeypatch.setattr(probes, "run_command", mock_run_command)
    monkeypatch.setattr(
        probes,
        "check_vertex_model_count_tokens",
        lambda *args, **kwargs: (500, "Vertex connection timeout"),
    )

    res = probes.probe_agy_models()
    assert res["status"] == "error"

    # Diff with failed result must not emit any removal events
    events = probes.diff_agy_models(res, baseline_data)
    assert events == []

    # In full orchestration, failure is recorded as status='error' without overwriting good baseline
    store.save_probe_snapshot(test_db, "agy_models", res, status="error")
    latest_good = store.get_latest_probe_snapshot(test_db, "agy_models", only_successful=True)
    assert latest_good is not None
    assert set(latest_good["data"]["model_ids"]) == {"claude-opus-4-6-thinking", "gemini-3.8-flash-high"}

    # In full run_all_probes execution, failing probe does not emit removal events
    events_all = probes.run_all_probes(test_db, fixtures=False)
    agy_removals = [e for e in events_all if e.get("source_id") == "probe:agy_models"]
    assert agy_removals == []



def test_probe_changelog_new_version(test_db):
    """New version detected in agy changelog generates a digest-priority event."""
    baseline_data = {
        "versions": ["1.4.7"],
        "latest_version": "1.4.7",
        "notes": {"1.4.7": "· Initial release"},
    }
    store.save_probe_snapshot(test_db, "agy_changelog", {"status": "success", "data": baseline_data})

    current_data = {
        "versions": ["1.4.8", "1.4.7"],
        "latest_version": "1.4.8",
        "notes": {
            "1.4.8": "· Fix terminal resize bug\n· Update model fallback order",
            "1.4.7": "· Initial release",
        },
    }

    events = probes.diff_agy_changelog({"status": "success", "data": current_data}, baseline_data)
    assert len(events) == 1
    ev = events[0]

    assert ev["triage_type"] == "account_change"
    assert ev["priority"] == "digest"
    assert "Antigravity (agy) yangi versiyasi: 1.4.8" in ev["title"]
    assert "Fix terminal resize bug" in ev["raw_content"]


def test_probe_cli_outdated_vs_latest_release(test_db):
    """CLI outdated vs parsed latest release from Atom feed emits digest-priority notification."""
    # Pre-seed baseline snapshot where codex was 0.156.1 and latest was 0.156.1 (up to date)
    baseline_data = {
        "tools": {
            "codex": {
                "installed": "0.156.1",
                "latest": "0.156.1",
                "outdated": False,
                "vendor": "OpenAI",
                "product": "Codex CLI",
            }
        }
    }
    store.save_probe_snapshot(test_db, "cli_versions", {"status": "success", "data": baseline_data})

    # Now Atom feed reveals v2.1.0 release for Codex
    latest_releases = {"openai-codex": "2.1.0"}

    # Current probe observes installed 0.156.1 vs latest 2.1.0 -> outdated
    current_data = {
        "tools": {
            "codex": {
                "installed": "0.156.1",
                "latest": "2.1.0",
                "outdated": True,
                "vendor": "OpenAI",
                "product": "Codex CLI",
            }
        }
    }

    events = probes.diff_cli_versions({"status": "success", "data": current_data}, baseline_data)
    assert len(events) == 1
    ev = events[0]

    assert ev["triage_type"] == "account_change"
    assert ev["priority"] == "digest"
    assert "Codex CLI yangilanishi mavjud" in ev["title"]
    assert "0.156.1" in ev["title"]
    assert "2.1.0" in ev["title"]
    assert "pip install -U codex" in ev["command"]


def test_probe_vertex_404_to_200_transition(test_db, monkeypatch):
    """Vertex model transitioning from 404 to 200 is detected as newly available (high priority)."""
    # Pre-seed baseline where gemini-4-argon was 404 (unavailable)
    baseline_data = {
        "available": ["gemini-2.5-flash", "gemini-3.7-flash"],
        "unavailable": ["gemini-4-argon"],
        "errors": {},
        "project": "project-92b77c1d-c511-47ce-965",
        "location": "global",
    }
    store.save_probe_snapshot(test_db, "vertex_models", {"status": "success", "data": baseline_data})

    # In current run, count_tokens returns 200 for gemini-4-argon
    current_data = {
        "available": ["gemini-2.5-flash", "gemini-3.7-flash", "gemini-4-argon"],
        "unavailable": [],
        "errors": {},
        "project": "project-92b77c1d-c511-47ce-965",
        "location": "global",
    }

    events = probes.diff_vertex_models({"status": "success", "data": current_data}, baseline_data)
    assert len(events) == 1
    ev = events[0]

    assert ev["triage_type"] == "account_change"
    assert ev["priority"] == "high"
    assert "Sizning akkauntingizda: gemini-4-argon paydo bo'ldi" in ev["title"]
    assert "count_tokens 200 OK" in ev["raw_content"]
    assert "gemini-4-argon" in ev["command"]

    rendered = renderer.render_account_change_message(ev)
    unescaped = html.unescape(rendered)
    assert "🔔 <b>Sizning akkauntingizda: gemini-4-argon paydo bo'ldi</b>" in unescaped
    assert "argon-ish.py" in rendered
    assert "gcloud ai models describe gemini-4-argon" in rendered
    assert len(rendered) <= 3900


def test_probe_vertex_failure_does_not_remove(test_db):
    """Network failure or quota error on Vertex count_tokens does not mark models as removed."""
    baseline_data = {
        "available": ["gemini-2.5-flash", "gemini-4-argon"],
        "unavailable": ["gemini-3.1-pro-high"],
        "errors": {},
    }
    store.save_probe_snapshot(test_db, "vertex_models", {"status": "success", "data": baseline_data})

    # During check, gemini-4-argon encountered a timeout error (not 404)
    current_data = {
        "available": ["gemini-2.5-flash"],
        "unavailable": ["gemini-3.1-pro-high"],
        "errors": {"gemini-4-argon": "HTTP 504 Gateway Timeout"},
    }

    # Diff must NOT report gemini-4-argon as removed because it's an error, not 404!
    events = probes.diff_vertex_models({"status": "success", "data": current_data}, baseline_data)
    assert events == []


def test_runner_probes_only_cycle(tmp_path, monkeypatch):
    """Runner --probes-only executes only account probes and skips feed ingestion."""
    db_file = tmp_path / "probes_only.db"
    conn = store.get_connection(db_file)

    # Pre-seed baseline for agy_models
    store.save_probe_snapshot(conn, "agy_models", {
        "status": "success",
        "data": {
            "models": {"gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)"},
            "model_ids": ["gemini-3.8-flash-low"],
        },
    })
    conn.close()

    # Run cycle with probes_only=True and fixtures=True
    runner.run_cycle(
        db_path=db_file,
        fixtures=True,
        dry_run=True,
        once=True,
        probes_only=True,
    )

    # In dry-run mode, disk DB wasn't mutated, but the logic ran without error
    check_conn = store.get_connection(db_file)
    cur = check_conn.cursor()
    # Confirm feeds were NOT ingested into disk DB (0 sources fetched)
    cur.execute("SELECT COUNT(*) FROM sources_meta")
    assert cur.fetchone()[0] == 0
    check_conn.close()


def test_get_latest_release_versions_highest_semver(test_db):
    """get_latest_release_versions extracts highest semver even if older patch published later."""
    now_iso = "2026-10-28T10:00:00"
    older_iso = "2026-10-20T10:00:00"
    with test_db:
        # Event 1: older date, but higher semver 2.1.0
        test_db.execute(
            """
            INSERT INTO events (id, source_id, entry_id, vendor, product, title, link, published_at, content_hash, raw_content, triage_type, priority, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("rel-1", "openai-codex", "codex-2.1.0", "OpenAI", "Codex CLI", "Release v2.1.0", "https://github.com/openai/codex/releases/tag/v2.1.0", older_iso, "h1", "v2.1.0", "feature", "digest", "unseen"),
        )
        # Event 2: newer published_at date, but lower semver (backport patch 1.56.2)
        test_db.execute(
            """
            INSERT INTO events (id, source_id, entry_id, vendor, product, title, link, published_at, content_hash, raw_content, triage_type, priority, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("rel-2", "openai-codex", "codex-1.56.2", "OpenAI", "Codex CLI", "Release v1.56.2", "https://github.com/openai/codex/releases/tag/v1.56.2", now_iso, "h2", "v1.56.2 backport", "sdk_patch", "digest", "unseen"),
        )

    versions = store.get_latest_release_versions(test_db)
    assert versions.get("openai-codex") == "2.1.0"


def test_runner_run_probes_default_false(tmp_path, monkeypatch):
    """run_cycle defaults to run_probes=False; CLI enables it for --once and --probes-only."""
    db_file = tmp_path / "probes_flag.db"
    conn = store.get_connection(db_file)
    conn.close()

    called = []
    monkeypatch.setattr(probes, "run_all_probes", lambda conn, fixtures=False: called.append("run_all_probes") or [])
    monkeypatch.setattr(runner, "load_sources", lambda: [])

    # Default run_cycle -> run_probes is False
    runner.run_cycle(db_path=db_file, dry_run=True, once=True)
    assert called == []

    # run_probes=True -> runs probes
    runner.run_cycle(db_path=db_file, dry_run=True, once=True, run_probes=True)
    assert called == ["run_all_probes"]

    called.clear()
    # probes_only=True -> runs probes
    runner.run_cycle(db_path=db_file, dry_run=True, once=True, probes_only=True)
    assert called == ["run_all_probes"]


def test_cluster_dedup_account_change_not_suppressed_by_feed(tmp_path, monkeypatch):
    """account_change rank in SEVERITY_ORDER prevents suppression by earlier feed items."""
    assert "account_change" in triage.SEVERITY_ORDER
    assert triage.SEVERITY_ORDER["account_change"] > triage.SEVERITY_ORDER["new_model"]

    db_path = tmp_path / "cluster_dedup.db"
    conn = store.get_connection(db_path)

    # 1. Run 1: Feed item announces Gemini 4 Argon (new_model, rank 70)
    ev_feed = parser.FeedEntry(
        entry_id="feed-gemini-4-argon",
        title="Google introduces Gemini 4 Argon",
        link="https://blog.google/gemini-4-argon",
        published_at="2026-10-28T01:00:00Z",
        summary="Introducing Gemini 4 Argon foundation model.",
        raw_content="Introducing Gemini 4 Argon foundation model.",
    )
    src_feed = {"id": "google-blog", "vendor": "Google", "product": "Google Blog"}
    store.record_unseen_entries(conn, src_feed, [ev_feed])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )
    delivered = []
    monkeypatch.setattr(delivery, "deliver", lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""))

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) >= 1
    delivered.clear()

    # 2. Run 2 (3 hours later): Probe detects gemini-4-argon on user's account (account_change, rank 85)
    probe_event = {
        "id": "probe:vertex_models:gemini-4-argon:avail",
        "source_id": "probe:vertex_models",
        "entry_id": "gemini-4-argon",
        "vendor": "Google",
        "product": "Google Vertex AI",
        "title": "Sizning akkauntingizda: gemini-4-argon paydo bo'ldi",
        "link": "https://cloud.google.com/vertex-ai/docs/models",
        "published_at": "2026-10-28T04:00:00Z",
        "raw_content": "gemini-4-argon Vertex AI loyihangizda faollashdi.",
        "triage_type": "account_change",
        "priority": "high",
        "status": "unseen",
        "command": "gcloud vertex ...",
        "profile_impact": "gemini-4-argon loyihangizda mavjud.",
    }
    monkeypatch.setattr(probes, "run_all_probes", lambda conn, fixtures=False: [probe_event])

    runner.run_cycle(db_path=db_path, dry_run=False, once=True, run_probes=True)

    # The account_change must NOT be suppressed by the prior feed item!
    assert len(delivered) >= 1
    assert any("gemini-4-argon" in d for d in delivered)


def test_cli_versions_outdated_upgraded_uptodate_progression():
    """Progression: up-to-date -> outdated -> upgraded-but-still-outdated -> up-to-date.

    Ensures that an upgraded CLI produces its 'updated' (bump) event even when still outdated.
    """
    baseline_data = {
        "tools": {
            "codex": {
                "installed": "0.156.1",
                "latest": "0.156.1",
                "outdated": False,
                "vendor": "OpenAI",
                "product": "Codex CLI",
            }
        }
    }

    # Step 1: Upstream release v2.1.0 published -> CLI becomes outdated
    state_outdated = {
        "tools": {
            "codex": {
                "installed": "0.156.1",
                "latest": "2.1.0",
                "outdated": True,
                "vendor": "OpenAI",
                "product": "Codex CLI",
            }
        }
    }
    events1 = probes.diff_cli_versions({"status": "success", "data": state_outdated}, baseline_data)
    assert len(events1) == 1
    assert events1[0]["entry_id"] == "outdated:codex:0.156.1"
    assert "Codex CLI yangilanishi mavjud" in events1[0]["title"]

    # Step 2: User partially upgrades CLI from 0.156.1 to 1.56.2 (still outdated vs 2.1.0)
    # Must produce 'updated' (bump) event, NOT shadowed by the outdated branch!
    state_upgraded_partial = {
        "tools": {
            "codex": {
                "installed": "1.56.2",
                "latest": "2.1.0",
                "outdated": True,
                "vendor": "OpenAI",
                "product": "Codex CLI",
            }
        }
    }
    events2 = probes.diff_cli_versions({"status": "success", "data": state_upgraded_partial}, state_outdated)
    assert len(events2) == 1
    assert events2[0]["entry_id"] == "bump:codex:1.56.2"
    assert "Codex CLI yangilandi: 1.56.2" in events2[0]["title"]

    # Step 3: User completes upgrade to 2.1.0 (now fully up-to-date)
    state_uptodate = {
        "tools": {
            "codex": {
                "installed": "2.1.0",
                "latest": "2.1.0",
                "outdated": False,
                "vendor": "OpenAI",
                "product": "Codex CLI",
            }
        }
    }
    events3 = probes.diff_cli_versions({"status": "success", "data": state_uptodate}, state_upgraded_partial)
    assert len(events3) == 1
    assert events3[0]["entry_id"] == "bump:codex:2.1.0"
    assert "Codex CLI yangilandi: 2.1.0" in events3[0]["title"]

    # Step 4: Next cycle with no changes produces 0 events
    events4 = probes.diff_cli_versions({"status": "success", "data": state_uptodate}, state_uptodate)
    assert len(events4) == 0


def test_regression_resumed_probe_job_preserves_account_change_not_suppressed(tmp_path, monkeypatch):
    """Regression: unfinished probe job retains account_change on resume and avoids suppression by feed."""
    db_path = tmp_path / "resumed_probe.db"
    conn = store.get_connection(db_path)

    # 1. Run 1: Prior feed item announces Gemini 4 Argon (new_model, rank 70) and is notified
    ev_feed = parser.FeedEntry(
        entry_id="feed-gemini-4-argon",
        title="Google introduces Gemini 4 Argon",
        link="https://blog.google/gemini-4-argon",
        published_at="2026-10-28T01:00:00Z",
        summary="Introducing Gemini 4 Argon foundation model.",
        raw_content="Introducing Gemini 4 Argon foundation model.",
    )
    src_feed = {"id": "google-blog", "vendor": "Google", "product": "Google Blog"}
    store.record_unseen_entries(conn, src_feed, [ev_feed])

    monkeypatch.setattr(runner, "load_sources", lambda: [])
    monkeypatch.setattr(
        enricher,
        "enrich_event",
        lambda ev, profile_text="", use_stub=False: enricher.stub_enrich(ev, profile_text),
    )
    delivered = []
    monkeypatch.setattr(
        delivery,
        "deliver",
        lambda text, dry_run=False, conn=None, outbox_id=None: (delivered.append(text) or True, ""),
    )

    runner.run_cycle(db_path=db_path, dry_run=False, once=True)
    assert len(delivered) >= 1
    delivered.clear()

    # 2. A probe detects gemini-4-argon on user's account and records it, but the job is left unfinished (pending)
    probe_event = {
        "id": "probe:vertex_models:gemini-4-argon:avail",
        "source_id": "probe:vertex_models",
        "entry_id": "gemini-4-argon",
        "vendor": "Google",
        "product": "Google Vertex AI",
        "title": "Sizning akkauntingizda: gemini-4-argon paydo bo'ldi",
        "link": "https://cloud.google.com/vertex-ai/docs/models",
        "published_at": "2026-10-28T04:00:00Z",
        "raw_content": "gemini-4-argon Vertex AI loyihangizda faollashdi.",
        "triage_type": "account_change",
        "priority": "high",
        "status": "unseen",
        "command": "gcloud vertex ...",
        "profile_impact": "gemini-4-argon loyihangizda mavjud.",
    }
    store.record_probe_events(conn, [probe_event])

    pending_before = store.get_pending_processing_jobs(conn)
    assert len(pending_before) == 1
    assert pending_before[0]["event_id"] == probe_event["id"]
    assert pending_before[0]["triage_type"] == "account_change"

    # 3. Next run resumes the unfinished job (probes return empty)
    monkeypatch.setattr(probes, "run_all_probes", lambda conn, fixtures=False: [])
    runner.run_cycle(db_path=db_path, dry_run=False, once=True, run_probes=False)

    # Job must be completed, not left pending
    pending_after = store.get_pending_processing_jobs(conn)
    assert len(pending_after) == 0

    # Resumed account_change must NOT be suppressed by the prior feed item!
    assert len(delivered) >= 1
    assert any("gemini-4-argon" in d for d in delivered)
    assert any("paydo bo'ldi" in html.unescape(d) for d in delivered)



