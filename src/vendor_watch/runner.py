"""Core runner orchestration for vendor watch cycles."""

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3

from . import delivery, enricher, fetcher, parser, probes, renderer, store, triage

log = logging.getLogger("vendor_watch")


def load_sources() -> list[dict]:
    sources_path = Path(__file__).parent / "sources.json"
    if not sources_path.is_file():
        raise FileNotFoundError(f"sources.json topilmadi: {sources_path}")
    return json.loads(sources_path.read_text(encoding="utf-8"))


def _mark_all_done(conn: sqlite3.Connection, ev: dict) -> None:
    member_ids = ev.get("member_ids") or [ev["id"]]
    for mid in member_ids:
        store.mark_processing_job_done(conn, mid)


def _mark_all_notified(conn: sqlite3.Connection, ev: dict, status: str = "sent") -> None:
    member_ids = ev.get("member_ids") or [ev["id"]]
    for mid in member_ids:
        store.mark_event_notified(conn, mid, status)


def run_cycle(
    db_path: Path | str,
    fixtures: bool = False,
    dry_run: bool = False,
    once: bool = True,
    digest_mode: bool = False,
    baseline_digest: bool = False,
    fixtures_dir: Path | None = None,
    probes_only: bool = False,
    run_probes: bool = False,
) -> None:
    if dry_run:
        # Dry-run must not mutate persistent state. Work on an in-memory copy.
        if str(db_path) != ":memory:" and Path(db_path).is_file():
            try:
                disk_conn = sqlite3.connect(
                    f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True
                )
            except Exception:
                disk_conn = sqlite3.connect(str(db_path))
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            disk_conn.backup(conn)
            disk_conn.close()
            store._init_schema(conn)
        else:
            conn = store.get_connection(":memory:")
    else:
        conn = store.get_connection(db_path)

    sources = load_sources()
    profile_text = enricher.load_profile()

    today_str = datetime.now().date().isoformat()
    last_digest_date = store.get_app_state(conn, "last_digest_date", "")
    is_daily_due = (last_digest_date != today_str)

    if fixtures and not fixtures_dir:
        fixtures_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "tests"
            / "fixtures"
            / "vendor_watch"
        )

    # 1. Retry pending or previously failed outbox items
    pending_outbox = store.get_pending_outbox(conn)
    for out_item in pending_outbox:
        is_digest = (out_item["priority"] == "digest")
        should_send = (out_item["priority"] == "high") or digest_mode or (is_digest and is_daily_due)
        if should_send:
            success, err = delivery.deliver(
                out_item["rendered_text"],
                dry_run=dry_run,
                conn=conn,
                outbox_id=out_item["id"],
            )
            store.update_outbox_status(
                conn,
                out_item["id"],
                "sent" if success else "failed",
                error_message=err,
            )

    # Resume unfinished processing jobs independently of fetching
    unfinished_jobs = store.get_pending_processing_jobs(conn)

    new_events: list[dict] = []
    baseline_digest_items: list[dict] = []

    # 2. Ingest feeds from each source (unless probes_only)
    if not probes_only:
        for src in sources:
            sid = src["id"]
            meta = store.get_source_meta(conn, sid)
            fetch_res = fetcher.fetch_feed(
                src,
                snapshot=meta,
                fixtures_dir=fixtures_dir if fixtures else None,
            )

            if fetch_res.not_modified:
                continue

            if fetch_res.status != 200 or not fetch_res.content:
                log.warning("Source %s fetch xatosi: %s", sid, fetch_res.error)
                store.mark_source_failure(conn, sid, status_code=fetch_res.status)
                continue

            try:
                entries = parser.parse_feed(fetch_res.content)
            except parser.FeedParseError as e:
                log.warning("Source %s parse xatosi: %s (snapshot saqlandi)", sid, e)
                store.mark_source_failure(conn, sid, status_code=500)
                continue

            is_baseline_done = bool(meta and meta.get("is_baseline_done"))

            if not is_baseline_done:
                if fixtures:
                    # In fixtures mode on fresh DB: seed older entries, treat latest as new
                    if len(entries) > 1:
                        older_entries = entries[1:]
                        latest_entries = entries[:1]
                        store.record_baseline(
                            conn,
                            src,
                            older_entries,
                            etag=fetch_res.etag,
                            last_modified=fetch_res.last_modified,
                        )
                        unseen = store.record_unseen_entries(
                            conn,
                            src,
                            latest_entries,
                            etag=fetch_res.etag,
                            last_modified=fetch_res.last_modified,
                        )
                        new_events.extend(unseen)
                    else:
                        store.record_baseline(
                            conn,
                            src,
                            entries,
                            etag=fetch_res.etag,
                            last_modified=fetch_res.last_modified,
                        )
                else:
                    store.record_baseline(
                        conn,
                        src,
                        entries,
                        etag=fetch_res.etag,
                        last_modified=fetch_res.last_modified,
                    )
            else:
                unseen = store.record_unseen_entries(
                    conn,
                    src,
                    entries,
                    etag=fetch_res.etag,
                    last_modified=fetch_res.last_modified,
                )
                new_events.extend(unseen)

    # 2b. Run Tier-3 account probes
    if run_probes or probes_only:
        probe_events = probes.run_all_probes(conn, fixtures=fixtures)
        new_events.extend(probe_events)

    if baseline_digest:
        baseline_digest_items = store.get_recent_baseline_entries(conn, days=7)

    # 3. Combine newly discovered events with resumed unfinished jobs
    events_by_id: dict[str, dict] = {}
    for uj in unfinished_jobs:
        if uj.get("triage_type") == "account_change":
            u_type = "account_change"
            u_prio = uj.get("priority", "high")
        else:
            u_type, u_prio = triage.classify_event(uj)
        uj["triage_type"] = u_type
        uj["priority"] = u_prio
        events_by_id[uj["id"]] = uj

    for ne in new_events:
        nid = ne["id"]
        if ne.get("triage_type") == "account_change":
            n_type = "account_change"
            n_prio = ne.get("priority", "high")
        else:
            n_type, n_prio = triage.classify_event(ne)
            ne["triage_type"] = n_type
            ne["priority"] = n_prio

        if nid not in events_by_id:
            events_by_id[nid] = ne
        else:
            existing = events_by_id[nid]
            latest = ne if ne.get("revision", 1) >= existing.get("revision", 1) else existing
            e_rank = triage.SEVERITY_ORDER.get(existing.get("triage_type"), 10)
            n_rank = triage.SEVERITY_ORDER.get(ne.get("triage_type"), 10)
            best_type = ne.get("triage_type") if n_rank >= e_rank else existing.get("triage_type")
            best_prio = ne.get("priority") if n_rank >= e_rank else existing.get("priority")
            merged_item = dict(latest)
            merged_item["triage_type"] = best_type
            merged_item["priority"] = best_prio
            events_by_id[nid] = merged_item

    events_to_process = list(events_by_id.values())

    # Cluster duplicates across sources within 24h
    clustered = triage.cluster_duplicates(events_to_process)
    recent_persisted = store.get_recent_events_for_clustering(conn, hours=24)

    filtered_events = []
    for ev in clustered:
        ev_vendor = (ev.get("vendor") or "").strip().lower()
        ev_key = triage._extract_entity_key(ev.get("title", ""), ev.get("raw_content", ""), ev.get("product", ""))
        ev_t = triage._parse_time(ev.get("published_at", ""))

        t_type = ev.get("triage_type")
        prio = ev.get("priority")
        if t_type != "account_change":
            if not t_type or not prio:
                t_type, prio = triage.classify_event(ev)
                ev["triage_type"] = t_type
                ev["priority"] = prio
            else:
                c_type, c_prio = triage.classify_event(ev)
                if triage.SEVERITY_ORDER.get(c_type, 10) > triage.SEVERITY_ORDER.get(t_type, 10):
                    ev["triage_type"] = c_type
                    ev["priority"] = c_prio
                    t_type, prio = c_type, c_prio

        # Compare with persisted clusters from last 24h
        already_notified = False
        for p_ev in recent_persisted:
            if p_ev["id"] == ev["id"]:
                continue
            p_vendor = (p_ev.get("vendor") or "").strip().lower()
            p_key = triage._extract_entity_key(p_ev.get("title", ""), p_ev.get("raw_content", ""), p_ev.get("product", ""))
            p_t = triage._parse_time(p_ev.get("published_at", ""))
            if ev_vendor == p_vendor and ev_key and ev_key == p_key and abs(ev_t - p_t) <= 86400:
                if p_ev.get("outbox_status") in ("sent", "pending"):
                    p_type = p_ev.get("triage_type")
                    if not p_type:
                        p_type, _ = triage.classify_event(p_ev)
                    ev_rank = triage.SEVERITY_ORDER.get(t_type, 10)
                    p_rank = triage.SEVERITY_ORDER.get(p_type, 10)
                    if ev_rank <= p_rank:
                        already_notified = True
                        break

        if already_notified:
            _mark_all_done(conn, ev)
            continue

        filtered_events.append(ev)

    # 4. Triage and enrichment
    high_priority_items: list[tuple[dict, dict]] = []
    digest_items: list[tuple[dict, dict]] = []

    for ev in filtered_events:
        t_type = ev.get("triage_type")
        prio = ev.get("priority")
        if t_type != "account_change":
            if not t_type or not prio:
                t_type, prio = triage.classify_event(ev)
                ev["triage_type"] = t_type
                ev["priority"] = prio

        # Pre-releases with priority 'ignore' are noise (excluded from digest and immediate)
        if prio == "ignore" or t_type == "prerelease":
            _mark_all_done(conn, ev)
            continue

        if t_type == "account_change":
            enrichment = {
                "uz_title": ev.get("title", ""),
                "uz_summary": ev.get("summary") or ev.get("raw_content", ""),
                "uz_benefit": ev.get("profile_impact", ""),
                "uz_how_to_use": ev.get("command", ""),
                "possible_overlap_with_custom_tool": None,
                "is_raw_mode": False,
            }
        else:
            enrichment = enricher.enrich_event(
                ev,
                profile_text=profile_text,
                use_stub=fixtures,
            )

        if prio == "high":
            high_priority_items.append((ev, enrichment))
        else:
            digest_items.append((ev, enrichment))

    # 5. Process high-priority events
    today_high_sent = store.count_today_high_priority(conn)
    immediate_items = []
    overflow_items = []

    for item in high_priority_items:
        if today_high_sent < 5:
            immediate_items.append(item)
            today_high_sent += 1
        else:
            overflow_items.append(item)

    for ev, enr in immediate_items:
        rev = ev.get("revision", 1)
        existing_out = store.get_outbox_for_event(conn, ev["id"], revision=rev)
        if existing_out and existing_out["status"] == "sent":
            _mark_all_notified(conn, ev, "sent")
            _mark_all_done(conn, ev)
            continue

        msg = renderer.render_high_priority_message(ev, enr)
        if existing_out:
            out_id = existing_out["id"]
        else:
            out_id = store.add_to_outbox(conn, ev["id"], "high", msg, revision=rev)
            store.link_outbox_events(conn, out_id, [ev])

        success, err = delivery.deliver(msg, dry_run=dry_run, conn=conn, outbox_id=out_id)
        store.update_outbox_status(conn, out_id, "sent" if success else "failed", err)
        _mark_all_notified(conn, ev, "sent" if success else "failed")
        _mark_all_done(conn, ev)

    if overflow_items:
        merged_pages = renderer.render_merged_pages(overflow_items)
        for m_msg, page_items in merged_pages:
            page_events = [it[0] for it in page_items]
            existing_out = store.find_existing_outbox_for_events(conn, page_events)
            if existing_out and existing_out["status"] == "sent":
                for p_ev in page_events:
                    _mark_all_notified(conn, p_ev, "sent")
                    _mark_all_done(conn, p_ev)
                continue

            if existing_out:
                out_id = existing_out["id"]
            else:
                out_id = store.add_to_outbox(conn, "merged_high", "high", m_msg)
                store.link_outbox_events(conn, out_id, page_events)
                for p_ev in page_events:
                    _mark_all_notified(conn, p_ev, "pending")

            success, err = delivery.deliver(m_msg, dry_run=dry_run, conn=conn, outbox_id=out_id)
            store.update_outbox_status(conn, out_id, "sent" if success else "failed", err)
            for p_ev in page_events:
                _mark_all_notified(conn, p_ev, "sent" if success else "failed")
                _mark_all_done(conn, p_ev)

    # 6. Process digest (daily or --digest or --fixtures or --baseline-digest)
    should_send_digest = digest_mode or is_daily_due or (fixtures and digest_items) or (baseline_digest and baseline_digest_items)

    if should_send_digest:
        all_for_digest = list(digest_items)
        if baseline_digest and baseline_digest_items:
            for b_ev in baseline_digest_items:
                b_type, b_prio = triage.classify_event(b_ev)
                if b_prio == "ignore" or b_type == "prerelease":
                    continue
                b_ev["triage_type"] = b_type
                b_ev["priority"] = b_prio
                b_enr = enricher.enrich_event(b_ev, profile_text=profile_text, use_stub=fixtures)
                all_for_digest.append((b_ev, b_enr))

        if all_for_digest:
            digest_pages = renderer.render_digest_pages(all_for_digest)
            for d_msg, page_items in digest_pages:
                page_events = [it[0] for it in page_items]
                existing_out = store.find_existing_outbox_for_events(conn, page_events)
                if existing_out and existing_out["status"] == "sent":
                    for p_ev in page_events:
                        _mark_all_notified(conn, p_ev, "sent")
                        _mark_all_done(conn, p_ev)
                    continue

                if existing_out:
                    out_id = existing_out["id"]
                else:
                    out_id = store.add_to_outbox(conn, "digest", "digest", d_msg)
                    store.link_outbox_events(conn, out_id, page_events)
                    for p_ev in page_events:
                        _mark_all_notified(conn, p_ev, "pending")

                success, err = delivery.deliver(d_msg, dry_run=dry_run, conn=conn, outbox_id=out_id)
                store.update_outbox_status(conn, out_id, "sent" if success else "failed", err)
                for p_ev in page_events:
                    _mark_all_notified(conn, p_ev, "sent" if success else "failed")
                    _mark_all_done(conn, p_ev)

        if not dry_run and (is_daily_due or digest_mode):
            store.set_app_state(conn, "last_digest_date", today_str)
    elif digest_items:
        digest_pages = renderer.render_digest_pages(digest_items)
        for d_msg, page_items in digest_pages:
            page_events = [it[0] for it in page_items]
            existing_out = store.find_existing_outbox_for_events(conn, page_events)
            if not existing_out:
                out_id = store.add_to_outbox(conn, "digest", "digest", d_msg)
                store.link_outbox_events(conn, out_id, page_events)
            for p_ev in page_events:
                _mark_all_notified(conn, p_ev, "pending")
                _mark_all_done(conn, p_ev)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Vendor updates watcher (OpenAI / Anthropic / Google)")
    p.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="Print messages to stdout without calling Telegram API")
    p.add_argument("--fixtures", action="store_true", help="Read feeds from tests/fixtures/vendor_watch/ and use stub enricher")
    p.add_argument("--digest", action="store_true", help="Generate and send daily digest")
    p.add_argument("--baseline-digest", action="store_true", help="Generate 7-day digest on initial baseline")
    p.add_argument("--probes-only", action="store_true", help="Run only account and tool probes without fetching feeds")
    p.add_argument("--db", type=str, default="data/vendor_watch.db", help="Path to SQLite database")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    run_cycle(
        db_path=args.db,
        fixtures=args.fixtures,
        dry_run=args.dry_run,
        once=args.once,
        digest_mode=args.digest,
        baseline_digest=args.baseline_digest,
        probes_only=args.probes_only,
        run_probes=bool(args.once or args.probes_only),
    )

