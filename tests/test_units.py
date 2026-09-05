"""Unit tests for the pure logic: grounding checks, archive round-trip, id shape."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import archive, extract, ingest, kb  # noqa: E402

SOURCE = """### Spoken transcript
Here is the prompt I use every day: "Act as a senior reviewer and list only the
three riskiest lines in this diff." It works better than asking for a full review.
"""


def test_verify_item_accepts_verbatim_quote():
    assert extract.verify_item(
        'Act as a senior reviewer and list only the three riskiest lines in this diff.',
        SOURCE)


def test_verify_item_accepts_minor_whitespace_drift():
    assert extract.verify_item(
        'Act as a senior reviewer   and list only\nthe three riskiest lines in this diff.',
        SOURCE)


def test_verify_item_rejects_model_invention():
    assert not extract.verify_item(
        'You are an expert marketer. Write five taglines for a coffee brand.', SOURCE)


def test_shortcode_parsing():
    assert ingest.shortcode_from_url("https://www.instagram.com/reel/ABC123x_-/") == "ABC123x_-"
    assert ingest.shortcode_from_url("https://instagram.com/p/XYZ/?igsh=1") == "XYZ"
    assert ingest.shortcode_from_url("https://www.instagram.com/user/reel/QQ1/") == "QQ1"


def test_shortcode_falls_back_for_other_hosts():
    tiktok = "https://www.tiktok.com/@u/video/7412345678901234567"
    assert ingest.shortcode_from_url(tiktok) == ingest.shortcode_from_url(tiktok)
    assert ingest.shortcode_from_url(tiktok).startswith("tiktok-7412345678901234567-")
    # same trailing segment, different post -> different id
    assert ingest.shortcode_from_url("https://x.com/a/status/1") != \
        ingest.shortcode_from_url("https://x.com/b/status/1")


def test_url_routing_covers_common_video_hosts():
    for url in ("https://www.tiktok.com/@u/video/7412345678901234567",
                "https://x.com/foo/status/1889",
                "https://vimeo.com/76979871"):
        assert ingest.URL_RE.search(url), url
    # a plain article link must stay on the text path
    assert not ingest.URL_RE.search("https://example.com/blog/post")


def test_clean_url_drops_tracking_params():
    assert ingest.clean_url("https://www.instagram.com/reel/AB/?igsh=xyz") == \
        "https://www.instagram.com/reel/AB"


def test_archive_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(archive.config, "ARCHIVE_DIR", tmp_path)
    record = {
        "shortcode": "AB1", "url": "https://www.instagram.com/reel/AB1",
        "title_en": "Prompt chaining basics", "content_type": "prompt",
        "summary_uz": ["Ikki bosqichli prompt yozish usuli"],
        "tags_en": ["prompt-chaining", "llm"],
        "items": [{"kind": "prompt", "name_en": "Reviewer prompt",
                   "content": "Act as a senior reviewer.", "note_uz": "Kod ko'rish uchun",
                   "verified": True}],
        "apply_suggestions_uz": ["Graphify skilliga qo'shish mumkin"],
        "usable": True, "flags": [], "transcript": "...", "caption": "", "onscreen": "",
        "date": "2026-08-11",
    }
    path = archive.write(record)
    assert path.name == "AB1.md"
    back = archive.read(path)
    assert back == record


def test_kb_ids_are_deterministic():
    record = {"shortcode": "AB1", "title_en": "T", "content_type": "prompt", "date": "2026-08-11",
              "summary_uz": ["x"], "tags_en": ["t"], "apply_suggestions_uz": [],
              "items": [{"kind": "prompt", "name_en": "P", "content": "c",
                         "note_uz": "n", "verified": True}]}
    ids = [r[0] for r in kb.build_documents(record)]
    assert ids == ["ig:AB1:summary", "ig:AB1:item:0"]
    assert kb.build_documents(record)[0][2]["project"] == kb.config.PROJECT_LABEL


def test_bare_link_vs_forwarded_post():
    assert ingest.is_bare_link("https://habr.com/ru/articles/939534/")
    assert ingest.is_bare_link("  https://example.com/x  ")
    # a forwarded post carries body text around the link
    assert not ingest.is_bare_link("Yangi maqola: https://habr.com/ru/articles/939534/ o'qing")
    assert not ingest.is_bare_link("hech qanday link yo'q")


def test_users_tiers(tmp_path, monkeypatch):
    from src import users
    monkeypatch.setattr(users.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(users.config, "ADMIN_USER_IDS", {111})
    monkeypatch.setattr(users.config, "ALLOWED_USER_IDS", {111, 222})
    monkeypatch.setattr(users, "_members", None)

    assert users.is_admin(111)
    assert not users.is_admin(222)
    assert users.is_allowed(222)          # .env member, not admin
    assert not users.is_allowed(333)

    assert users.add(333)
    assert users.is_allowed(333)
    assert not users.is_admin(333)        # added users never become admins
    assert not users.add(333)             # idempotent
    assert not users.add(111)             # already an admin

    # survives a reload from disk
    monkeypatch.setattr(users, "_members", None)
    assert users.is_allowed(333)

    assert users.remove(333)
    assert not users.is_allowed(333)
    assert not users.remove(111)          # admins are not removable from chat


def test_corrupt_members_file_does_not_lock_out(tmp_path, monkeypatch):
    from src import users
    monkeypatch.setattr(users.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(users.config, "ADMIN_USER_IDS", {111})
    monkeypatch.setattr(users.config, "ALLOWED_USER_IDS", {111})
    monkeypatch.setattr(users, "_members", None)
    (tmp_path / "members.json").write_text("{not json", encoding="utf-8")
    assert users.is_allowed(111)
    assert users.members() == set()


def test_installer_rejects_shell_metacharacters():
    from src import installer
    text = ("npm i -g evil; rm -rf /\n"
            "pip install $(whoami)\n"
            "ollama run a&&b\n"
            "npm install ../../etc/passwd")
    names = [c.name for c in installer._simple_candidates(text)]
    assert all(";" not in n and "&" not in n and "$" not in n and "/" not in n.strip("@")
               for n in names), names


def test_installer_argv_is_a_list_never_a_shell_string():
    from src import installer
    for cand in installer._git_candidates("see github.com/owner/repo for more"):
        assert isinstance(cand.argv, list)
        assert cand.argv[0] == "git"
        assert "https://github.com/owner/repo.git" in cand.argv


def test_dedup_failure_does_not_lose_the_job(tmp_path, monkeypatch):
    """A Chroma read can fail after whisper/vision/extraction have already run.
    Losing that work over an optional duplicate flag is the wrong trade."""
    from src import pipeline

    monkeypatch.setattr(pipeline.archive.config, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(pipeline.queue_db, "set_stage", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.extract, "extract", lambda *a, **k: {"usable": True})
    monkeypatch.setattr(pipeline.kb, "search",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("Internal error: Error finding id")))
    monkeypatch.setattr(pipeline.kb, "upsert_record", lambda record, *a, **k: 7)

    record = {"shortcode": "ZZ9", "url": "https://www.instagram.com/reel/ZZ9",
              "title_en": "t", "content_type": "prompt", "summary_uz": ["s"],
              "tags_en": [], "items": [], "apply_suggestions_uz": [],
              "usable": True, "flags": [], "transcript": "x", "caption": "",
              "onscreen": "", "date": "2026-09-03"}

    out = pipeline._finish({"id": "j1", "shortcode": "ZZ9"}, record, {}, lambda *a, **k: None)

    assert "dedup_skipped" in out["flags"]
    assert out["kb_rows"] == 7          # ish saqlandi, bazaga yozildi


def test_recipients_covers_both_tiers(tmp_path, monkeypatch):
    from src import config, users
    monkeypatch.setattr(config, "ADMIN_USER_IDS", {1})
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {2})
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(users, "_members", {3})
    assert users.recipients() == {1, 2, 3}


def test_broadcast_notice_carries_the_whole_card():
    from src import bot
    rec = {"usable": True, "title_en": "Prompt chaining", "shortcode": "tg-txt-abc",
           "content_type": "technique", "url": "https://x.test/a", "kb_rows": 3,
           "summary_uz": ["Birinchi", "Ikkinchi", "Uchinchi"],
           "apply_suggestions_uz": ["Shuni qil"]}
    out = bot._render_broadcast(rec, "@aimexpert")
    assert "@aimexpert" in out
    assert "Prompt chaining" in out
    # To'liq kartochka: hamma band, tavsiya va manba ham ketadi.
    for piece in ("Birinchi", "Ikkinchi", "Uchinchi", "Shuni qil", "x.test"):
        assert piece in out, piece


def test_broadcast_truncates_on_a_line_boundary():
    """Yarim yozilgan HTML tegi Telegram'ga hech qachon bormasin."""
    from src import bot
    rec = {"usable": True, "title_en": "T", "shortcode": "s", "kb_rows": 1,
           "summary_uz": ["x" * 1000] * 6, "items": [], "apply_suggestions_uz": []}
    out = bot._render_broadcast(rec, "@a")
    assert len(out) <= bot.BROADCAST_MAX_CHARS + 120
    assert out.count("<b>") == out.count("</b>")
    assert "/search" in out


def test_users_panel_flags_unreachable_members(monkeypatch):
    """Ruxsat berilgan != xabar yetadi: /start bosmagan odam ko'rinib tursin."""
    from src import bot, config, users
    monkeypatch.setattr(users, "admins", lambda: {1})
    monkeypatch.setattr(users, "members", lambda: {3})
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {1, 2})
    text, _ = bot._users_panel(reachable={1})
    assert "/start bosmagan" in text
    assert "2 kishi botga hali /start bosmagan" in text
    plain, _ = bot._users_panel()
    assert "/start bosmagan" not in plain


def test_broadcast_falls_back_when_author_unknown():
    from src import bot
    out = bot._render_broadcast(
        {"usable": True, "title_en": "X", "shortcode": "s", "kb_rows": 1}, "")
    assert "Jamoa a'zosi" in out


def test_text_shortcode_is_content_addressed():
    """Bir xil matn -> bir xil id, kim yuborganidan qat'i nazar."""
    import hashlib
    text = "  Yangi   prompt   texnikasi  "
    other_sender_same_text = "Yangi prompt texnikasi"
    digest = lambda t: hashlib.sha1(" ".join(t.split()).encode("utf-8")).hexdigest()[:12]
    assert digest(text) == digest(other_sender_same_text)
    assert digest(text) != digest("Boshqa mutlaqo mavzu")


def test_result_card_omits_row_count_when_unknown():
    """Arxivdan qayta ko'rsatishda yozuvlar soni noma'lum - 0 deb yozilmasin."""
    from src import bot
    rec = {"usable": True, "title_en": "T", "shortcode": "s", "summary_uz": ["a"],
           "kb_rows": None}
    assert "Graphify:" not in bot._render_result(rec)
    rec["kb_rows"] = 3
    assert "Graphify: 3 ta yozuv" in bot._render_result(rec)


def test_timeout_retry_backoff_and_limit(tmp_path, monkeypatch):
    """Timeout bo'lganda darhol yopilmasdan, chegaralangan qayta urinish (backoff bilan)
    ishlashi va urinishlar tugagach retry=False bilan status='failed' bo'lishi kerak."""
    import time
    import pytest
    from src import config, pipeline, queue_db

    db_path = tmp_path / "test_jobs.sqlite3"
    monkeypatch.setattr(config, "QUEUE_DB", db_path)
    monkeypatch.setattr(queue_db.config, "QUEUE_DB", db_path)
    monkeypatch.setattr(config, "TIMEOUT_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "TIMEOUT_BACKOFF_SEC", 5)

    queue_db.init()
    job_id = "test:timeout_job"
    sc = "timeout_sc"
    assert queue_db.enqueue(job_id, sc, "text", "test source", 123, 456)

    sleep_history = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_history.append(s))
    monkeypatch.setattr(pipeline, "_process_photo_or_text",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("request timed out")))

    # 1-urinish (attempts=1): timeout bo'ladi, 1 <= 2 bo'lgani uchun backoff=5*1=5s va status='queued'
    job1 = queue_db.claim_next()
    assert job1 is not None
    with pytest.raises(TimeoutError):
        pipeline.process(job1)
    assert sleep_history == [5]
    row1 = queue_db.get(job_id)
    assert row1["status"] == "queued"
    assert row1["attempts"] == 1

    # 2-urinish (attempts=2): timeout bo'ladi, 2 <= 2 bo'lgani uchun backoff=5*2=10s va status='queued'
    job2 = queue_db.claim_next()
    assert job2 is not None
    with pytest.raises(TimeoutError):
        pipeline.process(job2)
    assert sleep_history == [5, 10]
    row2 = queue_db.get(job_id)
    assert row2["status"] == "queued"
    assert row2["attempts"] == 2

    # 3-urinish (attempts=3): 3 > 2 (chegara tugadi), shuning uchun retry=False va status='failed'
    job3 = queue_db.claim_next()
    assert job3 is not None
    with pytest.raises(TimeoutError):
        pipeline.process(job3)
    assert sleep_history == [5, 10]
    row3 = queue_db.get(job_id)
    assert row3["status"] == "failed"
    assert row3["attempts"] == 3


def test_ffmpeg_timeout_is_not_retried():
    """Buzuq fayl uchun qayta urinish - yana 600s osilish, foydasi yo'q."""
    import subprocess
    from src import queue_db
    exc = subprocess.TimeoutExpired(cmd=["ffmpeg", "-i", "x.mp4"], timeout=600)
    assert not queue_db.is_timeout_error(exc)
    # fail() xatoni MATN sifatida oladi, shuning uchun matn ham rad etilsin.
    assert not queue_db.is_timeout_error(f"{type(exc).__name__}: {exc}")


def test_network_timeout_is_still_retried():
    """Tarmoq kechikishi vaqtinchalik - u qayta urinishga loyiq."""
    import requests
    from src import queue_db
    assert queue_db.is_timeout_error(requests.exceptions.Timeout("read timed out"))
    assert queue_db.is_timeout_error(TimeoutError("operation timed out"))
    assert queue_db.is_timeout_error("HTTPError: request timeout after 900s")


def test_private_urls_are_refused(monkeypatch):
    """Bot o'zining Ollama'siga yoki bulut metadata'siga so'rov yubormasin."""
    import socket
    from src import ingest

    def fake(host, port, *a, **kw):
        table = {"localhost": "127.0.0.1", "metadata.test": "169.254.169.254",
                 "lan.test": "192.168.1.10", "ok.test": "93.184.216.34"}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    for bad in ("http://localhost:11434/api/tags", "http://metadata.test/latest",
                "http://lan.test/admin"):
        try:
            ingest._reject_private_url(bad)
        except ingest.ArticleError:
            continue
        raise AssertionError(f"rad etilmadi: {bad}")
    ingest._reject_private_url("https://ok.test/article")  # ochiq manzil o'tadi


def test_non_http_schemes_are_refused():
    from src import ingest
    for bad in ("file:///C:/Windows/win.ini", "ftp://example.test/x"):
        try:
            ingest._reject_private_url(bad)
        except ingest.ArticleError:
            continue
        raise AssertionError(f"rad etilmadi: {bad}")


def test_vision_retries_once_on_truncation(monkeypatch):
    """chat_json'da bor edi, vision'da yo'q edi - shu sabab kadrlar yo'qolardi."""
    from src import extract, llm
    calls = []

    def fake_chat(model, msg, images=None, **kw):
        calls.append(kw.get("num_predict"))
        if len(calls) == 1:
            raise llm.TruncatedResponseError("done_reason=length")
        return "kadrlardagi matn"

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert extract.read_frames(["a.jpg"]) == "kadrlardagi matn"
    assert len(calls) == 2
    assert calls[0] is None and calls[1] > 0


class _FakeResponse:
    """requests.Response o'rnini bosuvchi minimal soxta javob."""

    def __init__(self, status=200, headers=None, body=b"", location=None):
        self.status_code = status
        self.headers = {"content-type": "text/html"}
        self.headers.update(headers or {})
        if location:
            self.headers["location"] = location
        self._body = body
        self.encoding = "ISO-8859-1" if "charset=" not in self.headers["content-type"] else "utf-8"
        self.closed = False

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "location" in self.headers

    is_permanent_redirect = is_redirect

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return self._responses.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _patch_session(monkeypatch, session):
    import requests
    monkeypatch.setattr(requests, "Session", lambda: session)


def test_redirect_to_private_address_is_refused(monkeypatch):
    """Ochiq domen 302 bilan localhost'ga yo'naltirsa - himoya ushlashi shart."""
    import socket
    from src import ingest

    table = {"ok.test": "93.184.216.34", "localhost": "127.0.0.1"}

    def fake_addr(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_addr)
    session = _FakeSession([_FakeResponse(302, location="http://localhost:11434/api/tags")])
    _patch_session(monkeypatch, session)

    try:
        ingest._fetch_html("https://ok.test/article", {})
    except ingest.ArticleError as e:
        assert "ichki tarmoq" in str(e), e
    else:
        raise AssertionError("yo'naltirish orqali ichki manzilga borildi")


def test_body_without_charset_is_read_as_utf8(monkeypatch):
    """requests text/html uchun ISO-8859-1 ga tushadi - o'zbekcha matn buziladi."""
    import socket
    from src import ingest

    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))])
    body = "<p>O'zbekiston — ta'lim</p>".encode("utf-8")
    session = _FakeSession([_FakeResponse(200, body=body)])
    _patch_session(monkeypatch, session)

    html = ingest._fetch_html("https://ok.test/a", {})
    assert "O'zbekiston — ta'lim" in html


def test_oversized_body_is_refused_and_closed(monkeypatch):
    import socket
    from src import config, ingest

    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))])
    monkeypatch.setattr(config, "ARTICLE_MAX_BYTES", 1024)
    resp = _FakeResponse(200, body=b"x" * 5000)
    _patch_session(monkeypatch, _FakeSession([resp]))

    try:
        ingest._fetch_html("https://ok.test/a", {})
    except ingest.ArticleError as e:
        assert "juda katta" in str(e), e
    else:
        raise AssertionError("hajm chegarasi ishlamadi")
    assert resp.closed, "javob yopilmadi - soket sizadi"


def test_permanent_media_error_blocks_every_retry():
    """is_timeout_error False qaytarishi yetmaydi - u oddiy retry yo'liga tushardi."""
    import subprocess
    from src import queue_db
    exc = subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=600)
    assert queue_db.is_permanent_media_error(exc)
    assert queue_db.is_permanent_media_error(f"{type(exc).__name__}: {exc}")

    class Wrapped(Exception):
        pass

    wrapped = Wrapped("pipeline failed")
    wrapped.__cause__ = exc
    assert queue_db.is_permanent_media_error(wrapped), "o'ralgan holat o'tkazib yuborildi"
    assert not queue_db.is_permanent_media_error(TimeoutError("read timed out"))


def test_only_author_or_admin_may_delete(monkeypatch, tmp_path):
    from src import archive, bot
    monkeypatch.setattr(bot.users, "is_admin", lambda uid: uid == 1)
    monkeypatch.setattr(archive, "path_for", lambda sc: tmp_path / f"{sc}.md")
    monkeypatch.setattr(archive, "read", lambda p: {"author_id": "42"} if p.name == "abc.md" else None)

    assert bot._may_modify(1, "abc")        # admin
    assert bot._may_modify(42, "abc")       # muallif
    assert not bot._may_modify(99, "abc")   # boshqa a'zo
    assert not bot._may_modify(99, "yoq")   # arxiv yo'q -> rad


def test_word_timeout_alone_is_not_a_timeout():
    """Matnda 'timeout' so'zi bo'lishi hali vaqtinchalik nosozlik degani emas."""
    from src import queue_db
    for text in ("DownloadError: https://x.test/post-timeout-issue 404",
                 "ValueError: invalid option timeout",
                 "FileNotFoundError: timeout.mp4"):
        assert not queue_db.is_timeout_error(text), text


def test_real_timeout_phrases_still_match():
    from src import queue_db
    for text in ("LLMError: Ollama request failed: HTTPConnectionPool: Read timed out.",
                 "ReadTimeout: too slow",
                 "ConnectTimeout: no route",
                 "HTTPError: request timeout after 900s"):
        assert queue_db.is_timeout_error(text), text


def test_vision_keeps_partial_text_when_truncated_twice(monkeypatch):
    """Ikki marta GPU sarflab, oxirida bo'sh qaytish - eng yomon natija."""
    from src import extract, llm
    calls = []

    def fake_chat(model, msg, images=None, **kw):
        calls.append(kw)
        raise llm.TruncatedResponseError("length", raw="kadrdagi yarim matn")

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert extract.read_frames(["a.jpg"]) == "kadrdagi yarim matn"
    assert len(calls) == 2
    assert calls[1]["num_ctx"] > 0 and calls[1]["num_predict"] > 0


def test_vision_reraises_when_nothing_was_read(monkeypatch):
    from src import extract, llm

    def fake_chat(model, msg, images=None, **kw):
        raise llm.TruncatedResponseError("length", raw="   ")

    monkeypatch.setattr(llm, "chat", fake_chat)
    try:
        extract.read_frames(["a.jpg"])
    except llm.TruncatedResponseError:
        return
    raise AssertionError("bo'sh natijada xato otilishi kerak edi")


def test_recent_filters_by_chat(tmp_path, monkeypatch):
    """/status a'zoga faqat o'zining ishlarini ko'rsatsin."""
    from src import config, queue_db
    monkeypatch.setattr(config, "QUEUE_DB", tmp_path / "jobs.sqlite3")
    queue_db.init()
    queue_db.enqueue("a", "sc-a", "text", "x", 111, 1)
    queue_db.enqueue("b", "sc-b", "text", "y", 222, 2)

    mine = {r["shortcode"] for r in queue_db.recent(10, chat_id=111)}
    assert mine == {"sc-a"}
    assert {r["shortcode"] for r in queue_db.recent(10)} == {"sc-a", "sc-b"}
