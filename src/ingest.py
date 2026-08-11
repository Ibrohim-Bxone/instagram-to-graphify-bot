"""Getting the video: Instagram link via yt-dlp, or a file sent straight to the bot."""

import random
import re
import time
from pathlib import Path

from . import config

_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:[^/]+/)?(?:reels?|p|tv|stories/[^/]+)/([A-Za-z0-9_-]+)", re.IGNORECASE
)
_YOUTUBE_RE = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})", re.IGNORECASE)
URL_RE = re.compile(r"https?://(?:\S*(?:instagram\.com|youtube\.com|youtu\.be)/\S+)", re.IGNORECASE)
ANY_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def extract_links(text: str) -> list:
    """Links in a forwarded post are usually the "batafsil" pointer — keep them
    verbatim in the record so the entry stays actionable later."""
    seen, out = set(), []
    for m in ANY_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def shortcode_from_url(url: str) -> str | None:
    m = _SHORTCODE_RE.search(url) or _YOUTUBE_RE.search(url)
    return m.group(1) if m else None

def is_youtube_url(url: str) -> bool:
    return bool(re.search(r"(?:youtube\.com|youtu\.be)/", url, re.IGNORECASE))


def clean_url(url: str) -> str:
    """Drop tracking query params so the same reel always yields the same job id."""
    if is_youtube_url(url):
        return url.split("&")[0].rstrip("/")
    return url.split("?")[0].rstrip("/")


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
        with yt_dlp.YoutubeDL({**common, "skip_download": True}) as ydl:
            raw = ydl.extract_info(url, download=False, process=False)
    except Exception as e:
        raise DownloadError(str(e)) from e

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
