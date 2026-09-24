"""Tests for bot boshqaruv, delivery, timeouts, deferred quota, inactive users, and admin safety."""

import asyncio
import time
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from src import archive, bot, config, queue_db, users, worker
import soqchi


def test_archive_read_missing_returns_none(tmp_path):
    """A3: archive.read returns None if file does not exist, never raising FileNotFoundError."""
    missing = tmp_path / "not_found.md"
    assert archive.read(missing) is None

    # Normal read still works
    rec = {"shortcode": "TST1", "title_en": "Sample", "usable": True}
    rendered = archive.render(rec)
    f = tmp_path / "TST1.md"
    f.write_text(rendered, encoding="utf-8")
    loaded = archive.read(f)
    assert loaded is not None
    assert loaded["shortcode"] == "TST1"


def test_deliver_existing_queued_or_running(tmp_path, monkeypatch):
    """A3: _deliver_existing on queued/running job saves sender to job_subscribers and notifies."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        queue_db.init()

        job_id = "ig:XYZ1"
        queue_db.enqueue(job_id, "XYZ1", "url", "https://instagram.com/p/XYZ1", chat_id=111, message_id=222)

        status = AsyncMock()
        status.chat.id = 333
        status.message_id = 444

        await bot._deliver_existing(status, "XYZ1")

        status.edit_text.assert_awaited_once_with("⏳ hozir ishlanmoqda, tayyor bo'lganda natija yuboriladi")
        subs = queue_db.get_subscribers(job_id)
        assert len(subs) == 1
        assert subs[0]["chat_id"] == 333
        assert subs[0]["message_id"] == 444

    asyncio.run(_test())


def test_deliver_existing_failed_requeues(tmp_path, monkeypatch):
    """A3: _deliver_existing on failed job requeues it with attempts=0, error=NULL and notifies."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        queue_db.init()

        job_id = "ig:FAIL1"
        queue_db.enqueue(job_id, "FAIL1", "url", "https://instagram.com/p/FAIL1", chat_id=111, message_id=222)
        queue_db.fail(job_id, "some transient failure", retry=False)

        failed_job = queue_db.get(job_id)
        assert failed_job["status"] == "failed"

        status = AsyncMock()
        status.chat.id = 555
        status.message_id = 666

        await bot._deliver_existing(status, "FAIL1")

        status.edit_text.assert_awaited_once_with("🔁 qayta navbatga qo'ydim")
        requeued = queue_db.get(job_id)
        assert requeued["status"] == "queued"
        assert requeued["attempts"] == 0
        assert requeued["error"] is None

    asyncio.run(_test())


def test_deliver_existing_done_with_archive(tmp_path, monkeypatch):
    """A3: _deliver_existing on done job with archive file displays the record."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path)
        queue_db.init()

        rec = {"shortcode": "DONE1", "title_en": "Saved Title", "usable": True}
        archive.write(rec)

        status = AsyncMock()
        status.chat.id = 777
        status.message_id = 888

        await bot._deliver_existing(status, "DONE1")
        assert status.edit_text.await_count == 1
        call_args = status.edit_text.call_args[0][0]
        assert "♻️ Bu allaqachon bazada" in call_args
        assert "Saved Title" in call_args

    asyncio.run(_test())


def test_deliver_existing_done_without_archive(tmp_path, monkeypatch):
    """A3: _deliver_existing on done job without archive file gives polite message."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path)
        queue_db.init()

        status = AsyncMock()
        status.chat.id = 777
        status.message_id = 888

        await bot._deliver_existing(status, "NOARCH1")
        assert status.edit_text.await_count == 1
        call_args = status.edit_text.call_args[0][0]
        assert "arxiv fayli topilmadi" in call_args

    asyncio.run(_test())


def test_contract_job_deferred_and_defer(tmp_path, monkeypatch):
    """SHARTNOMA (I2): queue_db.JobDeferred, defer_count, not_before, claim_next, defer()."""
    db_file = tmp_path / "jobs.sqlite3"
    monkeypatch.setattr(config, "QUEUE_DB", db_file)
    queue_db.init()

    exc = queue_db.JobDeferred(retry_after_sec=450)
    assert exc.retry_after_sec == 450
    assert "450" in str(exc)

    job_id = "ig:DEF1"
    queue_db.enqueue(job_id, "DEF1", "url", "https://instagram.com/p/DEF1", chat_id=10, message_id=20)

    # Claim next sets status=running and attempts=1
    job = queue_db.claim_next()
    assert job is not None
    assert job["id"] == job_id
    assert job["defer_count"] == 0
    assert job["attempts"] == 1

    # defer(job_id, 300) -> status='queued', defer_count=1, not_before=now+300, attempts reverted to 0
    now = time.time()
    queue_db.defer(job_id, 300)

    j = queue_db.get(job_id)
    assert j["status"] == "queued"
    assert j["defer_count"] == 1
    assert j["attempts"] == 0  # attempts oshmaydi
    assert j["not_before"] >= now + 290

    # claim_next ignores jobs with not_before > now
    assert queue_db.claim_next() is None

    # With future time claim_next picks it up
    with patch("time.time", return_value=now + 350):
        claimed = queue_db.claim_next()
        assert claimed is not None
        assert claimed["id"] == job_id
        assert claimed["attempts"] == 1


def test_worker_handles_job_deferred(tmp_path, monkeypatch):
    """SHARTNOMA (I2): worker catches JobDeferred, defers job and edits status message."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "QUOTA_MAX_DEFERS", 3)
        queue_db.init()

        job_id = "ig:KVOTA1"
        queue_db.enqueue(job_id, "KVOTA1", "url", "https://instagram.com/p/KVOTA1", chat_id=10, message_id=20)

        bot_mock = AsyncMock()

        # pipeline.process raises JobDeferred
        def fake_process(job, progress, keep_media=True):
            assert "defer_count" in job
            raise queue_db.JobDeferred(retry_after_sec=600)

        monkeypatch.setattr(worker.pipeline, "process", fake_process)

        # Run worker loop via create_task to exercise real worker() code
        worker_task = asyncio.create_task(worker.worker(bot_mock))
        try:
            for _ in range(50):
                if bot_mock.edit_message_text.await_count > 0:
                    break
                await asyncio.sleep(0.02)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        bot_mock.edit_message_text.assert_awaited_once_with(
            chat_id=10, message_id=20, text="⏳ Vertex kvotasi band — 10 daqiqadan keyin (1/3)"
        )
        deferred_job = queue_db.get(job_id)
        assert deferred_job["status"] == "queued"
        assert deferred_job["defer_count"] == 1

    asyncio.run(_test())


def test_worker_delivers_to_subscribers(tmp_path, monkeypatch):
    """A3: worker delivers result to job subscribers upon completion, including resubmissions with new message_id."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        queue_db.init()

        job_id = "ig:SUB1"
        queue_db.enqueue(job_id, "SUB1", "url", "https://instagram.com/p/SUB1", chat_id=10, message_id=20)
        # Author resubmits the same link later in the same chat -> different message_id
        queue_db.add_subscriber(job_id, chat_id=10, message_id=99)
        queue_db.add_subscriber(job_id, chat_id=30, message_id=40)
        queue_db.add_subscriber(job_id, chat_id=50, message_id=None)

        bot_mock = AsyncMock()
        record = {
            "shortcode": "SUB1",
            "title_en": "Subscriber Test",
            "usable": True,
            "media_paths": [],
            "kb_rows": 1,
            "md_path": "SUB1.md",
        }

        monkeypatch.setattr(worker.pipeline, "process", lambda job, progress, keep_media=True: dict(record))
        monkeypatch.setattr(worker, "_broadcast", AsyncMock())

        # Run worker loop via create_task to exercise real worker() code
        worker_task = asyncio.create_task(worker.worker(bot_mock))
        try:
            for _ in range(50):
                if len(queue_db.get_subscribers(job_id)) == 0 and bot_mock.send_message.await_count > 0:
                    break
                await asyncio.sleep(0.02)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        # Chat 10 (msg 99) and Chat 30 (msg 40) get edit_message_text; Chat 50 gets send_message
        assert bot_mock.edit_message_text.await_count == 3
        assert bot_mock.send_message.await_count == 1
        assert len(queue_db.get_subscribers(job_id)) == 0

        edited_msgs = [call.kwargs for call in bot_mock.edit_message_text.await_args_list]
        assert any(k.get("chat_id") == 10 and k.get("message_id") == 20 for k in edited_msgs)
        assert any(k.get("chat_id") == 10 and k.get("message_id") == 99 for k in edited_msgs)
        assert any(k.get("chat_id") == 30 and k.get("message_id") == 40 for k in edited_msgs)

        sent_msgs = [call.kwargs for call in bot_mock.send_message.await_args_list]
        assert any(k.get("chat_id") == 50 for k in sent_msgs)

    asyncio.run(_test())


def test_worker_timeout(tmp_path, monkeypatch):
    """B8: worker wait_for timeout marks job failed and moves to next."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "JOB_TIMEOUT_SEC", 0.05)
        queue_db.init()

        job_id = "ig:TO1"
        queue_db.enqueue(job_id, "TO1", "url", "https://instagram.com/p/TO1", chat_id=10, message_id=20)

        bot_mock = AsyncMock()

        def slow_process(job, progress, keep_media=True):
            time.sleep(0.3)
            return {}

        monkeypatch.setattr(worker.pipeline, "process", slow_process)

        # Run worker loop via create_task to exercise real worker() code
        worker_task = asyncio.create_task(worker.worker(bot_mock))
        try:
            for _ in range(50):
                j = queue_db.get(job_id)
                if j and j["status"] == "failed":
                    break
                await asyncio.sleep(0.02)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        failed_job = queue_db.get(job_id)
        assert failed_job["status"] == "failed"
        assert "timed out" in failed_job["error"].lower()
        bot_mock.edit_message_text.assert_awaited()
        call_text = bot_mock.edit_message_text.call_args.kwargs.get("text", "")
        assert "to'xtatildi" in call_text

    asyncio.run(_test())


def test_soqchi_checks_heartbeat_and_safe_pid_kill(tmp_path, monkeypatch):
    """B8: soqchi checks heartbeat; kills PID only if alive and cmdline contains 'src.bot'."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_file = data_dir / "worker.heartbeat"
    lock_file = data_dir / "bot.lock"

    # 1. Heartbeat is fresh -> nothing happens
    heartbeat_file.write_text(str(time.time()), encoding="utf-8")
    lock_file.write_text("12345", encoding="utf-8")

    terminated = []
    monkeypatch.setattr(soqchi, "terminate_process", lambda pid: terminated.append(pid) or True)

    soqchi.check_worker_heartbeat(tmp_path, chegara_min=10.0)
    assert len(terminated) == 0

    # 2. Heartbeat is stale (30 min ago)
    heartbeat_file.write_text(str(time.time() - 1800), encoding="utf-8")

    # PID cmdline does NOT have 'src.bot' -> NOT killed!
    monkeypatch.setattr(soqchi, "get_process_cmdline", lambda pid: "python other_script.py")
    soqchi.check_worker_heartbeat(tmp_path, chegara_min=10.0)
    assert len(terminated) == 0

    # PID cmdline DOES have 'src.bot' -> KILLED!
    monkeypatch.setattr(soqchi, "get_process_cmdline", lambda pid: "python -m src.bot")
    soqchi.check_worker_heartbeat(tmp_path, chegara_min=10.0)
    assert terminated == [12345]


def test_broadcast_inactivity_handling(tmp_path, monkeypatch):
    """A8: broadcast errors mark user inactive and exclude from future broadcasts; /start reactivates."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "ADMIN_USER_IDS", {1})
        monkeypatch.setattr(config, "ALLOWED_USER_IDS", {1, 2, 3})
        monkeypatch.setattr(users, "_members", set())
        queue_db.init()

        # All active initially
        assert users.recipients() == {1, 2, 3}

        bot_mock = AsyncMock()

        async def fake_send_message(uid, text, **kwargs):
            if uid == 2:
                raise Exception("TelegramBadRequest: Bad Request: chat not found")
            if uid == 3:
                raise Exception("TelegramForbiddenError: Forbidden: bot was blocked by the user")
            return MagicMock()

        bot_mock.send_message.side_effect = fake_send_message

        record = {"shortcode": "BR1", "title_en": "Broadcast Title", "usable": True}
        await worker._broadcast(bot_mock, record, exclude_chat_id=1, author="Tester")

        # 2 and 3 should now be inactive
        assert not users.is_active(2)
        assert not users.is_active(3)
        assert users.is_active(1)

        # recipients() now excludes inactive users
        assert users.recipients() == {1}

        # User 2 runs /start -> reactivated!
        msg = MagicMock()
        msg.from_user.id = 2
        msg.answer = AsyncMock()
        await bot.cmd_start(msg)

        assert users.is_active(2)
        assert users.recipients() == {1, 2}

    asyncio.run(_test())


def test_admin_inactive_alert_7_days(tmp_path, monkeypatch):
    """A8: inactive users alert sent to admin once every 7 days; last sent time saved in DB."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        monkeypatch.setattr(config, "ADMIN_USER_IDS", {999})
        queue_db.init()

        queue_db.mark_user_inactive(101, "chat not found")
        bot_mock = AsyncMock()

        # First call: alert is sent to admin 999
        await worker.maybe_alert_inactive_users(bot_mock)
        assert bot_mock.send_message.await_count == 1
        sent_text = bot_mock.send_message.call_args[0][1]
        assert "101" in sent_text
        assert "chat not found" in sent_text

        last_saved = queue_db.get_setting("last_inactive_alert_at")
        assert last_saved is not None

        # Immediate second call: < 7 days, so not sent again
        bot_mock.send_message.reset_mock()
        await worker.maybe_alert_inactive_users(bot_mock)
        assert bot_mock.send_message.await_count == 0

    asyncio.run(_test())


def test_b16_admin_empty_defaults_to_no_admin(monkeypatch):
    """B16: empty ADMIN_USER_IDS means no admins (does not fall back to ALLOWED_USER_IDS)."""
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {111, 222})
    monkeypatch.setattr(config, "ADMIN_USER_IDS", set())

    assert users.admins() == set()
    assert not users.is_admin(111)
    assert not users.is_admin(222)


def test_config_new_variables():
    """Check that all required configuration variables are present with defaults."""
    assert config.JOB_TIMEOUT_SEC == 1800
    assert config.QUOTA_DEFER_SEC == 600
    assert config.QUOTA_MAX_DEFERS == 3
    assert config.MODEL_EXTRACT_FALLBACK == "qwen3.6"
    assert config.VERTEX_MAX_OUTPUT_TOKENS == 8192
    assert config.WHISPER_TURKIC_AS_UZ == "uz,kk,tr,az,tk,ky,tt,ba"
    assert config.VERTEX_LOCATION == "global"
    assert hasattr(config, "VERTEX_PROJECT")
    assert hasattr(config, "GCLOUD_PATH")


def test_worker_subscribers_notified_on_defer_timeout_and_fail(tmp_path, monkeypatch):
    """Review topilmasi: defer, timeout va terminal fail holatlarida ham obunachilarga xabar borishi shart."""
    async def _test():
        db_file = tmp_path / "jobs.sqlite3"
        monkeypatch.setattr(config, "QUEUE_DB", db_file)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "QUOTA_MAX_DEFERS", 3)
        monkeypatch.setattr(config, "JOB_TIMEOUT_SEC", 1800)
        queue_db.init()

        bot_mock = AsyncMock()

        # 1. Defer testi - haqiqiy worker orqali
        job_id = "ig:DEF_SUB"
        queue_db.enqueue(job_id, "DEF_SUB", "url", "https://instagram.com/p/DEF_SUB", chat_id=1, message_id=10)
        queue_db.add_subscriber(job_id, chat_id=2, message_id=20)

        def fake_defer_process(job, progress, keep_media=True):
            raise queue_db.JobDeferred(retry_after_sec=600)

        monkeypatch.setattr(worker.pipeline, "process", fake_defer_process)

        worker_task = asyncio.create_task(worker.worker(bot_mock))
        try:
            for _ in range(50):
                if bot_mock.edit_message_text.await_count >= 2:
                    break
                await asyncio.sleep(0.02)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        edited = [c.kwargs for c in bot_mock.edit_message_text.await_args_list]
        assert any(k.get("chat_id") == 1 and k.get("message_id") == 10 and "Vertex kvotasi" in k.get("text", "") for k in edited)
        assert any(k.get("chat_id") == 2 and k.get("message_id") == 20 and "Vertex kvotasi" in k.get("text", "") for k in edited)
        # Defer da obunachilar o'chirilmaydi
        assert len(queue_db.get_subscribers(job_id)) == 1

        # 2. Timeout testi - haqiqiy worker orqali
        bot_mock.reset_mock()
        monkeypatch.setattr(config, "JOB_TIMEOUT_SEC", 0.05)
        job_id2 = "ig:TO_SUB"
        queue_db.enqueue(job_id2, "TO_SUB", "url", "https://instagram.com/p/TO_SUB", chat_id=1, message_id=11)
        queue_db.add_subscriber(job_id2, chat_id=3, message_id=30)

        def slow_process(job, progress, keep_media=True):
            time.sleep(0.3)
            return {}

        monkeypatch.setattr(worker.pipeline, "process", slow_process)

        worker_task = asyncio.create_task(worker.worker(bot_mock))
        try:
            for _ in range(50):
                j2 = queue_db.get(job_id2)
                if j2 and j2["status"] == "failed":
                    break
                await asyncio.sleep(0.02)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        edited2 = [c.kwargs for c in bot_mock.edit_message_text.await_args_list]
        assert any(k.get("chat_id") == 1 and k.get("message_id") == 11 and "to'xtatildi" in k.get("text", "") for k in edited2)
        assert any(k.get("chat_id") == 3 and k.get("message_id") == 30 and "to'xtatildi" in k.get("text", "") for k in edited2)
        # Timeout da obunachilar o'chiriladi
        assert len(queue_db.get_subscribers(job_id2)) == 0

    asyncio.run(_test())

