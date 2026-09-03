"""Runtime configuration, read from .env at the project root."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _ids(raw: str) -> set:
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_USER_IDS = _ids(os.getenv("ALLOWED_USER_IDS", ""))
# Admins can add/remove users and are the only ones who may run /install.
# Unset means "whoever was already trusted in .env", which keeps a single-user
# install working unchanged; an explicit list is what separates the two tiers.
ADMIN_USER_IDS = _ids(os.getenv("ADMIN_USER_IDS", "")) or set(ALLOWED_USER_IDS)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL_EXTRACT = os.getenv("MODEL_EXTRACT", "gemma4:12b")
MODEL_VISION = os.getenv("MODEL_VISION", "gemma4:12b")
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
# 8192 fits gemma4:12b fully in an 8GB card alongside its KV cache; a larger
# context (e.g. 32768) pushes part of the model onto the CPU and makes every
# call several times slower. Raise only if a transcript is routinely truncated.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
# 4096 output tokens is enough for full extraction JSON schemas; without an
# explicit budget Ollama cuts generation early with done_reason="length".
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "4096"))

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "") or None

VISION_MODE = os.getenv("VISION_MODE", "auto")
MAX_FRAMES = int(os.getenv("MAX_FRAMES", "4"))
MIN_TRANSCRIPT_CHARS = int(os.getenv("MIN_TRANSCRIPT_CHARS", "200"))
# Empirically measured against the real archive (2026-08-11): a near-exact
# resubmission of the same content scores ~0.80 with this embedder, unrelated
# topics score ~0.10-0.15. 0.90 never fires in practice — 0.75 leaves margin
# below true duplicates while staying well clear of unrelated content.
DUPLICATE_SIMILARITY_THRESHOLD = float(os.getenv("DUPLICATE_SIMILARITY_THRESHOLD", "0.75"))

DATA_DIR = ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
QUEUE_DB = DATA_DIR / "jobs.sqlite3"
LOG_DIR = DATA_DIR / "logs"

# Portable by default: the archive lives inside this repo's own data/ folder
# unless overridden. No machine-specific path is baked in.
ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", "") or (DATA_DIR / "archive"))

# GRAPHIFY_DIR is optional. Empty (default) means "standalone mode": kb.py keeps
# its own bundled ChromaDB at DATA_DIR/kb_db, no external project required. Set
# this only if you already run Graphify (github.com/search "Graphify MCP" — a
# separate local knowledge-base tool) and want entries to land there too.
_graphify_env = os.getenv("GRAPHIFY_DIR", "").strip()
GRAPHIFY_DIR = Path(_graphify_env) if _graphify_env else None
STANDALONE_KB_DIR = DATA_DIR / "kb_db"

PROJECT_LABEL = os.getenv("PROJECT_LABEL", "Promtlarim")

YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

# Telegram's cloud Bot API refuses to serve any file larger than 20MB, whatever
# the bot does. Pointing this at a self-hosted Bot API server
# (github.com/tdlib/telegram-bot-api) raises the ceiling to 2GB; in that mode
# get_file returns an absolute path on disk instead of a download URL, so the
# file is moved into place rather than fetched over HTTP.
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "").strip().rstrip("/")
TELEGRAM_API_LOCAL = bool(TELEGRAM_API_BASE)
CLOUD_FILE_LIMIT = 20 * 1024 * 1024


# /install runs real commands on this machine, driven by text that came off the
# internet — so it is opt-in, allow-listed to four installers, and always asks
# for confirmation showing the exact argv first. See src/installer.py.
# Both targets stay inside data/ by default: nothing lands anywhere that another
# tool reads automatically unless the user names that path themselves.
INSTALL_ENABLED = os.getenv("INSTALL_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
TOOLS_DIR = Path(os.getenv("TOOLS_DIR", "") or (DATA_DIR / "tools"))
SKILLS_DIR = Path(os.getenv("SKILLS_DIR", "") or (DATA_DIR / "skills"))
INSTALL_TIMEOUT_SEC = int(os.getenv("INSTALL_TIMEOUT_SEC", "600"))

for _d in (DATA_DIR, MEDIA_DIR, LOG_DIR, ARCHIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
