"""Core runner orchestration for vendor watch cycles."""

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3

from . import delivery, enricher, fetcher, parser, renderer, store, triage

log = logging.getLogger("vendor_watch")


def load_sources() -> list[dict]:
    sources_path = Path(__file__).parent / "sources.json"
    if not sources_path.is_file():
        raise FileNotFoundError(f"sources.json topilmadi: {sources_path}")
    return json.loads(sources_path.read_text(encoding="utf-8"))


def run_cycle(
    db_path: Path | str,
    fixtures: bool = False,
    dry_run: bool = False,
    once: bool = True,
    digest_mode: bool = False,
    baseline_digest: bool = False,
    fixtures_dir: Path | None = None,
) -> None:
    if dry_run:
        # Dry-run must not mutate persistent state. Work on an in-memory copy.
        if str(db_path) != ":memory:" and Path(db_path).is_file():
            disk_conn = store.get_connection(db_path)
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            disk_conn.backup(conn)
            disk_conn.close()
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

    # 2. Ingest feeds from each source
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

    if baseline_digest:
        baseline_digest_items = store.get_recent_baseline_entries(conn, days=7)

    # 3. Combine newly discovered events with resumed unfinished jobs
    events_to_process = list(unfinished_jobs)
    seen_ids = {e["id"] for e in events_to_process}
    for ne in new_events:
        if ne["id"] not in seen_ids:
            events_to_process.append(ne)
            seen_ids.add(ne["id"])

    # Cluster duplicates across sources within 24h
    clustered = triage.cluster_duplicates(events_to_process)
    recent_persisted = store.get_recent_events_for_clustering(conn, hours=24)

    filtered_events = []
    for ev in clustered:
        ev_vendor = (ev.get("vendor") or "").strip().lower()
        ev_key = triage._extract_entity_key(ev.get("title", ""), ev.get("raw_content", ""), ev.get("product", ""))
        ev_t = triage._parse_time(ev.get("published_at", ""))
        t_type, prio = triage.classify_event(ev)
        ev["triage_type"] = t_type
        ev["priority"] = prio

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
                    p_type, _ = triage.classify_event(p_ev)
                    ev_rank = triage.SEVERITY_ORDER.get(t_type, 10)
                    p_rank = triage.SEVERITY_ORDER.get(p_type, 10)
                    if ev_rank <= p_rank:
                        already_notified = True
                        break

        if already_notified:
            store.mark_processing_job_done(conn, ev["id"])
            continue

        filtered_events.append(ev)

    # 4. Triage and enrichment
    high_priority_items: list[tuple[dict, dict]] = []
    digest_items: list[tuple[dict, dict]] = []

    for ev in filtered_events:
        t_type = ev.get("triage_type")
        prio = ev.get("priority")
        if not t_type or not prio:
            t_type, prio = triage.classify_event(ev)
            ev["triage_type"] = t_type
            ev["priority"] = prio

        # Pre-releases with priority 'ignore' are noise (excluded from digest and immediate)
        if prio == "ignore" or t_type == "prerelease":
            store.mark_processing_job_done(conn, ev["id"])
            continue

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
        msg = renderer.render_high_priority_message(ev, enr)
        out_id = store.add_to_outbox(conn, ev["id"], "high", msg)
        success, err = delivery.deliver(msg, dry_run=dry_run, conn=conn, outbox_id=out_id)
        store.update_outbox_status(conn, out_id, "sent" if success else "failed", err)
        store.mark_processing_job_done(conn, ev["id"])

    if overflow_items:
        merged_msgs = renderer.render_merged_high_priority(overflow_items)
        for m_msg in merged_msgs:
            out_id = store.add_to_outbox(conn, "merged_high", "high", m_msg)
            success, err = delivery.deliver(m_msg, dry_run=dry_run, conn=conn, outbox_id=out_id)
            store.update_outbox_status(conn, out_id, "sent" if success else "failed", err)
        for o_ev, _ in overflow_items:
            store.mark_processing_job_done(conn, o_ev["id"])

    # 6. Process digest (daily or --digest or --fixtures or --baseline-digest)
    should_send_digest = digest_mode or is_daily_due or (fixtures and digest_items) or (baseline_digest and baseline_digest_items)

    if should_send_digest:
        all_for_digest = list(digest_items)
        if baseline_digest and baseline_digest_items:
            for b_ev in baseline_digest_items:
                b_enr = enricher.enrich_event(b_ev, profile_text=profile_text, use_stub=fixtures)
                all_for_digest.append((b_ev, b_enr))

        if all_for_digest:
            digest_msgs = renderer.render_digest_messages(all_for_digest)
            for d_msg in digest_msgs:
                out_id = store.add_to_outbox(conn, "digest", "digest", d_msg)
                success, err = delivery.deliver(d_msg, dry_run=dry_run, conn=conn, outbox_id=out_id)
                store.update_outbox_status(conn, out_id, "sent" if success else "failed", err)
            for d_ev, _ in digest_items:
                store.mark_processing_job_done(conn, d_ev["id"])

        if not dry_run and (is_daily_due or digest_mode):
            store.set_app_state(conn, "last_digest_date", today_str)
    elif digest_items:
        digest_msgs = renderer.render_digest_messages(digest_items)
        for d_msg in digest_msgs:
            store.add_to_outbox(conn, "digest", "digest", d_msg)
        for d_ev, _ in digest_items:
            store.mark_processing_job_done(conn, d_ev["id"])


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Vendor updates watcher (OpenAI / Anthropic / Google)")
    p.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="Print messages to stdout without calling Telegram API")
    p.add_argument("--fixtures", action="store_true", help="Read feeds from tests/fixtures/vendor_watch/ and use stub enricher")
    p.add_argument("--digest", action="store_true", help="Generate and send daily digest")
    p.add_argument("--baseline-digest", action="store_true", help="Generate 7-day digest on initial baseline")
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
    )
