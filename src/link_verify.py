"""GitHub havolalarini tekshirish moduli (link_verify).

Ushbu modul post matnidagi GitHub havolalarini aniqlaydi, GitHub API orqali
ularning mavjudligini (reachability) tekshiradi va havola postdagi vosita yoki
sarlavhaga mazmunan mos kelishini (identity) tasdiqlaydi.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

ALGO_VERSION = 1

GH_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

PUNCTUATION_TO_STRIP = ".,;:!?'\")>]}/\\"

STOP_WORDS = {
    "ai", "the", "a", "an", "for", "and", "or", "with", "workflow", "tool", "tools",
    "app", "apps", "api", "apis", "framework", "agent", "agents", "skill", "skills",
    "open", "source", "based", "using", "via", "to", "of", "in", "on", "by", "your",
    "an", "from", "is", "at", "as", "into", "all", "new", "how", "what", "this", "that",
    "it", "not", "free", "best", "top", "get", "code", "github", "com", "http", "https"
}

IGNORED_OWNERS = {
    "features", "pricing", "join", "login", "signup", "settings",
    "explore", "topics", "trending", "collections", "events", "about",
    "contact", "site", "readme", "security", "customer-stories"
}


def find_github_links(text: str) -> list[tuple[str, str]]:
    """Matndan GitHub (owner, repo) havolalarini ajratadi.

    - .git kesiladi
    - oxiridagi tinish belgilari tozalanadi
    - takrorlar olib tashlanadi (tartib saqlangan holda)
    """
    if not text:
        return []

    seen = set()
    results = []

    for match in GH_URL_PATTERN.finditer(text):
        owner = match.group(1).strip(PUNCTUATION_TO_STRIP)
        repo = match.group(2).rstrip(PUNCTUATION_TO_STRIP)

        if repo.lower().endswith(".git"):
            repo = repo[:-4].rstrip(PUNCTUATION_TO_STRIP)

        if not owner or not repo:
            continue

        if owner.lower() in IGNORED_OWNERS:
            continue

        key = (owner.lower(), repo.lower())
        if key not in seen:
            seen.add(key)
            results.append((owner, repo))

    return results


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """SSRF himoyasi va ortiqcha redirectlarni rad etuvchi handler."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_github_api(url: str, timeout: int) -> dict:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
        raise ValueError(f"Faqat https://api.github.com ruxsat etilgan: {url}")

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Instagram-to-Graphify-Bot",
            "Accept": "application/vnd.github+json",
        },
    )
    opener = urllib.request.build_opener(_NoRedirectHandler)
    with opener.open(req, timeout=timeout) as resp:
        content = resp.read().decode("utf-8")
        return json.loads(content)


def check_github(owner: str, repo: str, timeout: int | None = None) -> dict:
    """GET api.github.com/repos/{owner}/{repo}

    Qaytadi:
    found, full_name, description, topics, homepage, archived, fork, pushed_at, stars.
    """
    if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or not re.match(r"^[A-Za-z0-9_.-]+$", repo):
        return {"found": False, "error": "invalid_owner_or_repo"}

    if timeout is None:
        timeout = getattr(config, "LINK_VERIFY_TIMEOUT", 8)

    url = f"https://api.github.com/repos/{owner}/{repo}"

    try:
        data = _fetch_github_api(url, timeout=timeout)
        return {
            "found": True,
            "full_name": data.get("full_name") or f"{owner}/{repo}",
            "description": data.get("description") or "",
            "topics": data.get("topics") or [],
            "homepage": data.get("homepage") or "",
            "archived": bool(data.get("archived", False)),
            "fork": bool(data.get("fork", False)),
            "pushed_at": data.get("pushed_at"),
            "stars": data.get("stargazers_count", 0),
        }
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            log.warning("GitHub API rate limit (%d): %s/%s", e.code, owner, repo)
            return {"found": False, "code": e.code, "rate_limited": True}
        if e.code == 404:
            return {"found": False, "code": 404}
        log.warning("GitHub API HTTPError %d (%s/%s)", e.code, owner, repo)
        return {"found": False, "code": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        log.warning("GitHub tekshirishda istisno %s (%s/%s): %s", type(e).__name__, owner, repo, e)
        return {"found": False, "error": type(e).__name__}


def extract_meaningful_tokens(text: str) -> set[str]:
    """Matndan stop-so'zlardan xoli va 2 belgidan uzun tokenlarni ajratadi."""
    if not text:
        return set()
    words = re.split(r"[^a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in STOP_WORDS}


def identity_matches(item_name: str, title: str, repo_info: dict) -> bool:
    """Repo item yoki sarlavhaga mos kelishini tekshiradi.

    - Repo nomi item/sarlavha nomiga AYNAN tushsa True;
    - Aks holda nom+tavsif+topics bo'yicha kamida 2 ta mazmunli umumiy so'z.
    """
    if not isinstance(repo_info, dict) or not repo_info.get("found"):
        return False

    full_name = repo_info.get("full_name") or ""
    repo_name = full_name.split("/")[-1].lower() if "/" in full_name else full_name.lower()
    if not repo_name:
        return False

    clean_repo = re.sub(r"[^a-z0-9]", "", repo_name)
    clean_item = re.sub(r"[^a-z0-9]", "", (item_name or "").lower())
    clean_title = re.sub(r"[^a-z0-9]", "", (title or "").lower())

    item_words = [re.sub(r"[^a-z0-9]", "", w) for w in (item_name or "").lower().split()]
    title_words = [re.sub(r"[^a-z0-9]", "", w) for w in (title or "").lower().split()]

    if clean_repo and len(clean_repo) >= 2:
        if clean_item and clean_repo == clean_item:
            return True
        if clean_title and clean_repo == clean_title:
            return True
        if clean_repo in item_words and clean_repo not in STOP_WORDS:
            return True
        if clean_repo in title_words and clean_repo not in STOP_WORDS:
            return True

    nom_tok = extract_meaningful_tokens(item_name) | extract_meaningful_tokens(title)
    desc = repo_info.get("description") or ""
    topics = repo_info.get("topics") or []
    topics_str = " ".join(topics) if isinstance(topics, list) else str(topics)
    repo_meta_text = f"{full_name} {desc} {topics_str}"
    meta_tok = extract_meaningful_tokens(repo_meta_text)

    common = nom_tok & meta_tok
    return len(common) >= 2


def is_cache_valid(entry: dict, now: float | None = None) -> bool:
    """Kesh yozuvining algo_version va TTL bo'yicha amal qilish muddatini tekshiradi."""
    if not isinstance(entry, dict):
        return False
    if entry.get("algo_version") != ALGO_VERSION:
        return False
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return False
    if now is None:
        now = time.time()
    age = now - checked_at
    if age < 0:
        return False

    result = entry.get("result")
    if not isinstance(result, dict):
        return False

    if result.get("found") is True:
        ttl = getattr(config, "LINKS_CACHE_TTL_FOUND_SEC", 30 * 86400)
    elif result.get("code") == 404:
        ttl = getattr(config, "LINKS_CACHE_TTL_NOT_FOUND_SEC", 7 * 86400)
    else:
        # Xatoliklar (5xx, timeout, tarmoq xatolari)
        ttl = getattr(config, "LINKS_CACHE_TTL_ERROR_SEC", 3600)

    return age < ttl


def get_cache_path() -> Path:
    return getattr(config, "LINKS_CACHE_FILE", config.DATA_DIR / "links_cache.json")


def load_cache(cache_path: Path | None = None) -> dict:
    path = cache_path or get_cache_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            log.warning("links_cache yuklashda xatolik: %s", e)
    return {}


def save_cache(cache: dict, cache_path: Path | None = None) -> None:
    path = cache_path or get_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("links_cache saqlashda xatolik: %s", e)


def verify_links_in_items(
    items: list[dict],
    source_text: str,
    title: str = "",
    cache_path: Path | None = None,
) -> dict:
    """Matndagi GitHub havolalarini tekshiradi va mos kelsa item["source_url"] ga yozadi.

    Cheklovlar:
    - Har yozuvga maksimal LINK_VERIFY_MAX_REPOS (standart 5 ta) repo tekshiriladi.
    - Umumiy vaqt budjeti LINK_VERIFY_TOTAL_TIMEOUT (standart 20s).
    - 403/429 da tekshiruv to'xtaydi, xato keshlanmaydi, bayroq qo'yilmaydi.
    - Tarmoq xatosi konveyerni yiqitmaydi.
    """
    stats = {"checked": 0, "matched": 0, "rate_limited": False, "repos_found": 0}
    if not getattr(config, "LINK_VERIFY", True):
        return stats

    check_items = [i for i in items if isinstance(i, dict) and i.get("kind") != "prompt" and i.get("name_en")]
    if not check_items:
        return stats

    links = find_github_links(source_text)
    if not links:
        return stats

    max_repos = getattr(config, "LINK_VERIFY_MAX_REPOS", 5)
    links = links[:max_repos]
    time_budget = getattr(config, "LINK_VERIFY_TOTAL_TIMEOUT", 20)
    start_time = time.time()

    cache = load_cache(cache_path)
    cache_updated = False
    checked_repos = []

    for owner, repo in links:
        if time.time() - start_time >= time_budget:
            log.warning("Link verify vaqt budjeti (%ds) yetdi, to'xtatildi", time_budget)
            break

        stats["checked"] += 1
        key = f"{owner.lower()}/{repo.lower()}"
        cached_entry = cache.get(key)

        if cached_entry and is_cache_valid(cached_entry):
            repo_info = cached_entry.get("result", {})
        else:
            try:
                repo_info = check_github(owner, repo)
            except Exception as e:
                log.warning("check_github xatosi: %s", e)
                repo_info = {"found": False, "error": str(e)}

            # Rate limit tekshiruvi:
            if repo_info.get("rate_limited") or repo_info.get("code") in (403, 429):
                log.warning("GitHub rate limit uchradi (%s/%s) — to'xtatildi", owner, repo)
                stats["rate_limited"] = True
                # 403/429 keshlanmaydi!
                break

            cache[key] = {
                "result": repo_info,
                "checked_at": time.time(),
                "algo_version": ALGO_VERSION,
            }
            cache_updated = True

        if repo_info.get("found"):
            stats["repos_found"] += 1
            checked_repos.append((owner, repo, repo_info))

    if cache_updated:
        save_cache(cache, cache_path)

    # Itemlarga mos havolani bog'lash:
    for item in check_items:
        if item.get("source_url"):
            continue
        item_name = item.get("name_en", "")
        for owner, repo, repo_info in checked_repos:
            if identity_matches(item_name, title, repo_info):
                full_name = repo_info.get("full_name") or f"{owner}/{repo}"
                item["source_url"] = f"https://github.com/{full_name}"
                item["link_verified"] = True
                stats["matched"] += 1
                break

    # Rate limited bo'lsa, hali tasdiqlanmagan itemlarni belgilaymiz:
    if stats["rate_limited"]:
        for item in check_items:
            if not item.get("source_url"):
                item["rate_limited"] = True

    return stats
