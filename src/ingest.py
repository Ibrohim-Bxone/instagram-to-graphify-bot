"""Getting the video: Instagram link via yt-dlp, or a file sent straight to the bot."""

import hashlib
import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config

_SHORTCODE_RE = re.compile(
    r"https?://(?:[A-Za-z0-9-]+\.)*instagram\.com/(?:[^/]+/)?"
    r"(?:reels?|p|tv|stories/[^/]+)/([A-Za-z0-9_-]+)", re.IGNORECASE
)
# Anchored at the scheme for the same reason as URL_RE below: without it,
# notyoutube.com/watch?v=X and evil.ru/?u=instagram.com/p/X would hand back a
# real video's id, and that id is the archive filename and KB row prefix.
_YOUTUBE_RE = re.compile(
    r"https?://(?:[A-Za-z0-9-]+\.)*(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{6,})", re.IGNORECASE)

# yt-dlp already handles ~1800 sites; the bot only has to recognise a link as
# "video" to route it there. The host list stays explicit rather than "any URL"
# because a forwarded post that merely contains a link must keep going down the
# text path — /video forces the video pipeline for anything not listed here.
MEDIA_HOSTS = [
    "instagram.com", "youtube.com", "youtu.be", "tiktok.com", "vt.tiktok.com",
    "twitter.com", "x.com", "reddit.com", "facebook.com", "fb.watch",
    "vimeo.com", "twitch.tv", "dailymotion.com", "threads.net", "threads.com",
    "pinterest.com", "pin.it", "linkedin.com", "bilibili.com", "rutube.ru",
    "vk.com", "ok.ru", "soundcloud.com", "kick.com", "streamable.com",
]
_HOST_ALT = "|".join(h.replace(".", r"\.") for h in MEDIA_HOSTS)
# The host must sit directly after the scheme, optionally behind subdomains —
# matching it as a loose substring would pull in dropbox.com (contains box.com,
# which contains x.com), mailbox.com and notyoutube.com.
URL_RE = re.compile(rf"https?://(?:[A-Za-z0-9-]+\.)*(?:{_HOST_ALT})(?:[:/]\S*)?", re.IGNORECASE)
ANY_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def extract_links(text: str) -> list:
    """Links in a forwarded post are usually the "batafsil" pointer — keep them
    verbatim in the record so the entry stays actionable later."""
    seen, out = set(), []
    for m in ANY_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?)]}>\"'«»")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def shortcode_from_url(url: str) -> str | None:
    """A stable, filename-safe id for the post behind `url`.

    Instagram and YouTube keep their native ids so existing archive entries stay
    addressable. Every other host falls back to slug+hash: the slug keeps the
    filename readable, the hash keeps two posts with the same trailing segment
    (`/video/`, `/p/`) from colliding.
    """
    # Matched against the canonical form, not the raw one: it puts `v=` back
    # first on `watch?app=desktop&v=XYZ`, and it percent-encodes a whole URL
    # smuggled through a redirect wrapper's query so it can no longer pose as
    # the id of the video it names.
    canonical = clean_url(url)
    m = _SHORTCODE_RE.search(canonical) or _YOUTUBE_RE.search(canonical)
    if m:
        return m.group(1)

    parsed = urlsplit(canonical)
    if not parsed.netloc:
        return None
    host = parsed.netloc.removeprefix("www.").split(".")[0]
    slug = re.sub(r"[^A-Za-z0-9_-]", "", parsed.path.rsplit("/", 1)[-1])[:24]
    # Hashing the canonical form (query included) is what keeps ?id=1 and ?id=2
    # apart while still collapsing the same page shared with different tracking.
    digest = hashlib.sha1(canonical.encode()).hexdigest()[:6]
    return "-".join(p for p in (host, slug, digest) if p)

def is_youtube_url(url: str) -> bool:
    return bool(re.search(r"(?:youtube\.com|youtu\.be)/", url, re.IGNORECASE))


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "igsh", "igshid", "si", "fbclid", "gclid", "ref", "ref_src", "ref_url",
    "share_id", "share_app_id", "spm", "_r", "_t", "feature", "app", "pp",
    "source", "epa", "sfnsn", "mibextid", "rdt", "web_copy_link",
}


def clean_url(url: str) -> str:
    """Canonical form: tracking params and fragment dropped, meaning kept.

    Two links must produce the same string exactly when they point at the same
    content, because this string is what the shortcode (archive filename, KB row
    prefix) and the queue id are derived from. That rules out dropping the query
    wholesale: `?id=1` and `?id=2` are different articles, while a bare `&`-split
    loses the `v=` on `youtube.com/watch?app=desktop&v=XYZ`.
    """
    parsed = urlsplit(url.strip())
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    if is_youtube_url(url):
        # `v` alone identifies the video; playlist/timestamp params only split ids.
        kept = [(k, v) for k, v in kept if k == "v"]
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path,
                       urlencode(kept), ""))


class DownloadError(RuntimeError):
    pass


def download(url: str, shortcode: str) -> dict:
    """Download video(s) and photo carousel items from a supported URL."""
    from io import BytesIO

    import requests
    import yt_dlp
    from PIL import Image

    from .transcribe import ffmpeg_exe

    out_dir = config.MEDIA_DIR / shortcode
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*"):
        if old.is_file():
            old.unlink(missing_ok=True)

    common = {"quiet": True, "no_warnings": True}
    if config.YTDLP_COOKIES_FROM_BROWSER:
        common["cookiesfrombrowser"] = (config.YTDLP_COOKIES_FROM_BROWSER,)

    time.sleep(random.uniform(1.0, 3.0))
    try:
        with yt_dlp.YoutubeDL({**common, "skip_download": True, "ignore_no_formats_error": True}) as ydl:
            raw = ydl.extract_info(url, download=False, process=False)
    except Exception as e:
        raise DownloadError(str(e)) from e
    if not raw:
        raise DownloadError("Instagram post metadata could not be retrieved")

    entries = list(raw.get("entries") or [raw])
    photo_paths = []
    for index, entry in enumerate(entries, 1):
        if not entry or entry.get("formats") or not entry.get("thumbnails"):
            continue
        try:
            photo_url = entry["thumbnails"][-1]["url"]
            headers = entry.get("http_headers") or raw.get("http_headers") or {}
            response = requests.get(photo_url, headers=headers, timeout=60)
            response.raise_for_status()
            path = out_dir / f"photo-{index:03d}.jpg"
            Image.open(BytesIO(response.content)).convert("RGB").save(path, "JPEG", quality=95)
            photo_paths.append(path)
        except Exception:
            continue

    if any(entry and entry.get("formats") for entry in entries):
        opts = {
            **common,
            "outtmpl": str(out_dir / "video-%(playlist_index)s.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "ffmpeg_location": ffmpeg_exe(),
            "noprogress": True,
            "retries": 3,
            "ignoreerrors": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as e:
            if not photo_paths:
                raise DownloadError(str(e)) from e

    videos = sorted(p for p in out_dir.glob("video-*")
                    if p.suffix != ".part" and p.is_file())
    media_paths = videos + photo_paths
    if not media_paths:
        raise DownloadError("Instagram post contains no downloadable media (expired, private, or access-limited)")

    return {
        "video_path": videos[0] if videos else None,
        "video_paths": videos,
        "photo_paths": photo_paths,
        "media_paths": media_paths,
        "caption": (raw.get("description") or "").strip(),
        "uploader": raw.get("uploader") or raw.get("channel") or "",
        "title": (raw.get("title") or "").strip(),
        "duration": raw.get("duration") or 0,
        "webpage_url": raw.get("webpage_url") or url,
    }

def is_bare_link(text: str) -> bool:
    """Is this message just a link the user wants analysed?

    A forwarded post usually carries its own body text around the link and must
    keep going down the text path — only a message that is essentially nothing
    but a URL means "go read this page for me".
    """
    stripped = (text or "").strip()
    m = ANY_URL_RE.fullmatch(stripped)
    return bool(m)


class ArticleError(RuntimeError):
    pass


def fetch_article(url: str) -> dict:
    """Read a web page (habr, a blog, docs) down to its article text.

    Kept separate from download(): there is no media, so the pipeline skips
    transcription and vision entirely and extracts straight from the text.
    """
    import trafilatura

    headers = {"User-Agent": "Mozilla/5.0 (compatible; IdeaBot/1.0)"}
    try:
        import requests
        response = requests.get(url, headers=headers, timeout=45)
        response.raise_for_status()
        html = response.text
    except Exception as e:
        raise ArticleError(f"sahifani ochib bo'lmadi: {e}") from e

    text = trafilatura.extract(html, include_comments=False, include_tables=True,
                               favor_precision=True) or ""
    if len(text.strip()) < 200:
        raise ArticleError("sahifada o'qiladigan matn topilmadi (paywall, JS, yoki bo'sh)")

    meta = trafilatura.extract_metadata(html)
    title = getattr(meta, "title", "") or ""
    author = getattr(meta, "author", "") or getattr(meta, "sitename", "") or ""

    # Titles are worth keeping in the body: extract() drops the <h1>, and the
    # title is often the single most informative line on the page.
    caption = f"{title}\n\n{text}".strip() if title else text.strip()
    return {
        "video_path": None, "photo_paths": [],
        "caption": caption,
        "uploader": author, "title": title, "duration": 0,
        "webpage_url": url,
    }


def adopt_text(text: str, shortcode: str) -> dict:
    """A forwarded Telegram post with no media — text (and usually a link) only."""
    return {
        "video_path": None, "photo_paths": [],
        "caption": (text or "").strip(),
        "uploader": "", "title": "", "duration": 0, "webpage_url": "",
    }


def adopt_photos(paths: list, shortcode: str, caption: str = "") -> dict:
    """A forwarded Telegram post with one or more photos (an album shares one post)."""
    return {
        "video_path": None, "photo_paths": list(paths),
        "caption": (caption or "").strip(),
        "uploader": "", "title": "", "duration": 0, "webpage_url": "",
    }


def adopt_file(path: Path, shortcode: str, caption: str = "") -> dict:
    """A video the user forwarded to Telegram directly — no Instagram call at all."""
    out_dir = config.MEDIA_DIR / shortcode
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("video-*"):
        old.unlink(missing_ok=True)
    caption_file = config.MEDIA_DIR / f"{shortcode}.caption.txt"
    if not caption and caption_file.exists():
        caption = caption_file.read_text(encoding="utf-8")
    target = out_dir / f"video{path.suffix or '.mp4'}"
    if path.resolve() != target.resolve():
        target.write_bytes(path.read_bytes())
    return {
        "video_path": target,
        "caption": caption.strip(),
        "uploader": "",
        "title": "",
        "duration": 0,
        "webpage_url": "",
    }
