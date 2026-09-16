"""Tests for src/link_verify.py and its integration with src/extract.py.

Tarmoq so'rovlari MOCK qilingan — haqiqiy tarmoqqa murojaat yo'q.
"""

import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from src import config, extract, link_verify


# ---------------------------------------------------------------------------
# 1. Havola ajratish testlari (find_github_links)
# ---------------------------------------------------------------------------

def test_find_github_links_basic():
    text = "Kodni ko'ring: https://github.com/psf/black va https://github.com/pallets/flask"
    links = link_verify.find_github_links(text)
    assert links == [("psf", "black"), ("pallets", "flask")]


def test_find_github_links_strips_git_extension():
    text = "Repo: https://github.com/psf/black.git"
    links = link_verify.find_github_links(text)
    assert links == [("psf", "black")]


def test_find_github_links_strips_punctuation():
    text = "Tavsiya: (https://github.com/psf/black), yoki https://github.com/pallets/flask. Yana: https://github.com/tiangolo/fastapi!"
    links = link_verify.find_github_links(text)
    assert links == [("psf", "black"), ("pallets", "flask"), ("tiangolo", "fastapi")]


def test_find_github_links_deduplication():
    text = """
    Havolalar:
    https://github.com/psf/black
    https://github.com/psf/black.git
    https://github.com/PSF/black/
    (https://github.com/psf/black).
    """
    links = link_verify.find_github_links(text)
    assert len(links) == 1
    assert links[0] == ("psf", "black")


def test_find_github_links_without_protocol():
    text = "Repozitoriy github.com/octocat/Hello-World sahifasida joylashgan."
    links = link_verify.find_github_links(text)
    assert links == [("octocat", "Hello-World")]


def test_find_github_links_empty_and_ignored_pages():
    assert link_verify.find_github_links("") == []
    assert link_verify.find_github_links("Hech qanday havola yo'q") == []
    assert link_verify.find_github_links("https://github.com/features/actions") == []
    assert link_verify.find_github_links("https://github.com/pricing") == []


# ---------------------------------------------------------------------------
# 2. check_github testlari (urllib mock)
# ---------------------------------------------------------------------------

def test_check_github_success():
    mock_payload = {
        "full_name": "psf/black",
        "description": "The uncompromising Python code formatter",
        "topics": ["python", "formatter", "code"],
        "homepage": "https://black.readthedocs.io",
        "archived": False,
        "fork": False,
        "pushed_at": "2026-09-01T12:00:00Z",
        "stargazers_count": 38000,
    }
    with patch.object(link_verify, "_fetch_github_api", return_value=mock_payload):
        res = link_verify.check_github("psf", "black")
        assert res["found"] is True
        assert res["full_name"] == "psf/black"
        assert res["description"] == "The uncompromising Python code formatter"
        assert res["topics"] == ["python", "formatter", "code"]
        assert res["stars"] == 38000
        assert res["archived"] is False


def test_check_github_404():
    err = urllib.error.HTTPError("https://api.github.com/repos/o/r", 404, "Not Found", {}, None)
    with patch.object(link_verify, "_fetch_github_api", side_effect=err):
        res = link_verify.check_github("o", "r")
        assert res["found"] is False
        assert res["code"] == 404


def test_check_github_403_rate_limit():
    err = urllib.error.HTTPError("https://api.github.com/repos/o/r", 403, "rate limit exceeded", {}, None)
    with patch.object(link_verify, "_fetch_github_api", side_effect=err):
        res = link_verify.check_github("o", "r")
        assert res["found"] is False
        assert res["code"] == 403
        assert res.get("rate_limited") is True


def test_check_github_network_error():
    with patch.object(link_verify, "_fetch_github_api", side_effect=urllib.error.URLError("Network unreachable")):
        res = link_verify.check_github("o", "r")
        assert res["found"] is False
        assert res.get("error") == "URLError"


def test_check_github_ssrf_protection():
    # Faqat to'g'ri nomlar qabul qilinadi
    res = link_verify.check_github("owner/injection", "repo")
    assert res["found"] is False
    assert res["error"] == "invalid_owner_or_repo"


# ---------------------------------------------------------------------------
# 3. identity_matches testlari
# ---------------------------------------------------------------------------

def test_identity_matches_exact_name():
    repo_info = {"found": True, "full_name": "psf/black", "description": "", "topics": []}
    assert link_verify.identity_matches("black", "", repo_info) is True
    assert link_verify.identity_matches("Black", "Python vositalari", repo_info) is True


def test_identity_matches_exact_name_casing_and_hyphen():
    repo_info = {"found": True, "full_name": "Significant-Gravitas/AutoGPT", "description": "", "topics": []}
    assert link_verify.identity_matches("Auto-GPT", "", repo_info) is True
    assert link_verify.identity_matches("autogpt", "", repo_info) is True


def test_identity_matches_item_contains_repo_word():
    repo_info = {"found": True, "full_name": "ollama/ollama", "description": "Get up and running with LMs", "topics": []}
    assert link_verify.identity_matches("Ollama tool", "", repo_info) is True


def test_identity_matches_title_exact():
    repo_info = {"found": True, "full_name": "langchain-ai/langchain", "description": "Building context-aware LLM apps", "topics": []}
    assert link_verify.identity_matches("AI tool", "LangChain bilan ishlash", repo_info) is True


def test_identity_matches_two_meaningful_words_overlap():
    repo_info = {
        "found": True,
        "full_name": "speech-team/engine",
        "description": "Fast speech to text transcriber engine",
        "topics": ["transcription"],
    }
    # item_name "speech transcriber" -> "speech" va "transcriber" ikkalasi repo_info da bor (2 ta so'z)
    assert link_verify.identity_matches("speech transcriber", "Audio tool", repo_info) is True


def test_identity_matches_not_enough_words_overlap():
    repo_info = {
        "found": True,
        "full_name": "foo/bar",
        "description": "Python web server",
        "topics": [],
    }
    # Faqat 1 ta so'z mos ("server"), bu yetarli emas (< 2)
    assert link_verify.identity_matches("docker server", "Dasturlash", repo_info) is False


def test_identity_matches_stop_words_do_not_count():
    repo_info = {
        "found": True,
        "full_name": "company/unknown-pkg",
        "description": "Open source AI tool and workflow app",
        "topics": ["ai", "workflow"],
    }
    # Faqat stop-so'zlar ("ai", "tool", "app", "workflow") mos, mazmunli so'z yo'q
    assert link_verify.identity_matches("AI tool app", "Workflow", repo_info) is False


def test_identity_matches_unfound_repo():
    repo_info = {"found": False, "code": 404}
    assert link_verify.identity_matches("black", "Black formatter", repo_info) is False


# ---------------------------------------------------------------------------
# 4. Kesh TTL testlari (links_cache.json)
# ---------------------------------------------------------------------------

def test_cache_ttl_found_repo(tmp_path):
    now = time.time()
    valid_entry = {
        "algo_version": link_verify.ALGO_VERSION,
        "checked_at": now - 10 * 86400,  # 10 kun oldin (TTL 30 kun)
        "result": {"found": True, "full_name": "psf/black"},
    }
    expired_entry = {
        "algo_version": link_verify.ALGO_VERSION,
        "checked_at": now - 31 * 86400,  # 31 kun oldin
        "result": {"found": True, "full_name": "psf/black"},
    }
    assert link_verify.is_cache_valid(valid_entry, now=now) is True
    assert link_verify.is_cache_valid(expired_entry, now=now) is False


def test_cache_ttl_not_found_404(tmp_path):
    now = time.time()
    valid_entry = {
        "algo_version": link_verify.ALGO_VERSION,
        "checked_at": now - 5 * 86400,  # 5 kun oldin (TTL 7 kun)
        "result": {"found": False, "code": 404},
    }
    expired_entry = {
        "algo_version": link_verify.ALGO_VERSION,
        "checked_at": now - 8 * 86400,  # 8 kun oldin
        "result": {"found": False, "code": 404},
    }
    assert link_verify.is_cache_valid(valid_entry, now=now) is True
    assert link_verify.is_cache_valid(expired_entry, now=now) is False


def test_cache_ttl_error_5xx(tmp_path):
    now = time.time()
    valid_entry = {
        "algo_version": link_verify.ALGO_VERSION,
        "checked_at": now - 1800,  # 30 daqiqa oldin (TTL 1 soat)
        "result": {"found": False, "code": 500, "error": "HTTP 500"},
    }
    expired_entry = {
        "algo_version": link_verify.ALGO_VERSION,
        "checked_at": now - 3700,  # 61 daqiqa oldin
        "result": {"found": False, "code": 500, "error": "HTTP 500"},
    }
    assert link_verify.is_cache_valid(valid_entry, now=now) is True
    assert link_verify.is_cache_valid(expired_entry, now=now) is False


def test_cache_atomic_write(tmp_path):
    cache_file = tmp_path / "links_cache.json"
    cache = {
        "psf/black": {
            "algo_version": 1,
            "checked_at": time.time(),
            "result": {"found": True, "full_name": "psf/black"},
        }
    }
    link_verify.save_cache(cache, cache_file)
    assert cache_file.exists()
    loaded = link_verify.load_cache(cache_file)
    assert "psf/black" in loaded
    assert loaded["psf/black"]["result"]["found"] is True


# ---------------------------------------------------------------------------
# 5. extract.py bilan integratsiya testlari
# ---------------------------------------------------------------------------

def test_extract_flow_link_verified_no_flag(tmp_path, monkeypatch):
    """Matndagi havola tasdiqlansa: source_url olinadi, unverified bayrog'i qo'yilmaydi."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LINK_VERIFY", True)
    monkeypatch.setattr(config, "VERIFY_NAMES", True)

    repo_data = {
        "full_name": "psf/black",
        "description": "The uncompromising Python code formatter",
        "topics": ["python", "formatter"],
        "homepage": "",
        "archived": False,
        "fork": False,
        "pushed_at": "2026-09-01T00:00:00Z",
        "stargazers_count": 38000,
    }

    mock_llm_json = {
        "title_en": "Python code formatting",
        "content_type": "tool",
        "summary_uz": ["Kodni formatlash vositasi"],
        "tags_en": ["python", "formatter"],
        "items": [
            {"kind": "tool", "name_en": "Black", "content": "black formatter", "note_uz": "formatlash"}
        ],
        "apply_suggestions_uz": ["Loyiha kodini tozalash"],
        "usable": True,
    }

    with patch.object(link_verify, "_fetch_github_api", return_value=repo_data), \
         patch("src.llm.chat_json", return_value=mock_llm_json), \
         patch.object(config, "EXTRACT_BACKEND", "llm"):

        caption = "Python dasturchilari uchun: https://github.com/psf/black vositasidan foydalaning!"
        result = extract.extract(transcript="", caption=caption, onscreen="", meta={})

        item = result["items"][0]
        assert item["source_url"] == "https://github.com/psf/black"
        assert item["verified"] is True
        assert item.get("link_verified") is True
        # Havola tasdiqlangach unverified bayrog'i qo'yilmaydi!
        assert not any("unverified:Black" in f for f in result.get("flags", []))


def test_extract_flow_rate_limit_403_no_flag_and_not_cached(tmp_path, monkeypatch):
    """403 rate limit yuz berganda: tekshiruv to'xtaydi, keshlanmaydi, bayroq ham qo'yilmaydi."""
    cache_file = tmp_path / "links_cache.json"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LINKS_CACHE_FILE", cache_file)
    monkeypatch.setattr(config, "LINK_VERIFY", True)
    monkeypatch.setattr(config, "VERIFY_NAMES", True)

    err = urllib.error.HTTPError("https://api.github.com/repos/psf/black", 403, "rate limit", {}, None)

    mock_llm_json = {
        "title_en": "Code formatter",
        "content_type": "tool",
        "summary_uz": ["Formatlash"],
        "tags_en": ["python"],
        "items": [
            {"kind": "tool", "name_en": "Black", "content": "formatter", "note_uz": "izoh"}
        ],
        "apply_suggestions_uz": ["Qo'llash"],
        "usable": True,
    }

    with patch.object(link_verify, "_fetch_github_api", side_effect=err), \
         patch("src.llm.chat_json", return_value=mock_llm_json), \
         patch.object(config, "EXTRACT_BACKEND", "llm"):

        caption = "Ko'ring: https://github.com/psf/black"
        result = extract.extract(transcript="", caption=caption, onscreen="", meta={})

        # 1. 403 keshlanmaydi:
        cache = link_verify.load_cache(cache_file)
        assert "psf/black" not in cache

        # 2. Bayroq qo'yilmaydi — limit yozuvni yomon qilib qo'ymaydi:
        flags = result.get("flags", [])
        assert not any("unverified:Black" in f for f in flags)
        assert not any("unchecked:Black" in f for f in flags)


def test_extract_flow_network_error_resilience(tmp_path, monkeypatch):
    """Tarmoq xatosi konveyerni yiqitmaydi (try/except, log, davom etadi)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LINK_VERIFY", True)
    monkeypatch.setattr(config, "VERIFY_NAMES", True)

    mock_llm_json = {
        "title_en": "Some tool",
        "content_type": "tool",
        "summary_uz": ["Tavsif"],
        "tags_en": ["tool"],
        "items": [
            {"kind": "tool", "name_en": "SomeTool", "content": "some tool", "note_uz": "izoh"}
        ],
        "apply_suggestions_uz": ["Foydalanish"],
        "usable": True,
    }

    with patch.object(link_verify, "_fetch_github_api", side_effect=urllib.error.URLError("Connection refused")), \
         patch("src.llm.chat_json", return_value=mock_llm_json), \
         patch.object(config, "EXTRACT_BACKEND", "llm"):

        caption = "Link: https://github.com/owner/repo"
        # Exception otilmasligi, konveyer yiqilmasligi shart:
        result = extract.extract(transcript="", caption=caption, onscreen="", meta={})
        assert isinstance(result, dict)
        assert len(result["items"]) == 1


def test_max_repos_limit(tmp_path, monkeypatch):
    """Har yozuvga maksimal LINK_VERIFY_MAX_REPOS ta repo tekshiriladi."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LINK_VERIFY", True)
    monkeypatch.setattr(config, "LINK_VERIFY_MAX_REPOS", 3)

    items = [{"kind": "tool", "name_en": "tool"}]
    source_text = " ".join([f"https://github.com/org/repo{i}" for i in range(10)])

    call_count = 0
    def mock_fetch(url, timeout):
        nonlocal call_count
        call_count += 1
        return {"full_name": f"org/repo{call_count}", "description": "", "topics": []}

    with patch.object(link_verify, "_fetch_github_api", side_effect=mock_fetch):
        stats = link_verify.verify_links_in_items(items, source_text, title="", cache_path=tmp_path / "cache.json")
        assert stats["checked"] == 3
        assert call_count == 3


def test_rate_limited_key_never_reaches_the_archive(tmp_path, monkeypatch):
    """`rate_limited` — VAQTINCHALIK holat. Arxivga butun `record` JSON bo'lib
    yoziladi (archive.py:86), shuning uchun bu kalit u yerga tushsa limit
    o'tib ketgandan keyin ham yozuvda muzlab qolardi."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LINK_VERIFY", True)
    monkeypatch.setattr(config, "VERIFY_NAMES", True)

    mock_llm_json = {
        "title_en": "Tool roundup",
        "content_type": "tool",
        "summary_uz": ["Vositalar to'plami"],
        "tags_en": ["ai"],
        "items": [
            {"kind": "tool", "name_en": "Nomalum Vosita", "content": "x", "note_uz": "y"}
        ],
        "apply_suggestions_uz": ["Sinab ko'rish"],
        "usable": True,
    }

    err = urllib.error.HTTPError("https://api.github.com", 403, "rate limit", {}, None)
    with patch.object(link_verify, "_fetch_github_api", side_effect=err),          patch("src.llm.chat_json", return_value=mock_llm_json),          patch.object(config, "EXTRACT_BACKEND", "llm"):

        caption = "Repo: https://github.com/some/repo"
        result = extract.extract(transcript="", caption=caption, onscreen="", meta={})

    for item in result["items"]:
        assert "rate_limited" not in item, "vaqtinchalik holat arxivga yozilmasin"
