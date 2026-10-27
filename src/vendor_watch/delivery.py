"""Telegram delivery and dry-run output dispatch for vendor updates."""

import json
import logging
import os
import sys
import urllib.request

log = logging.getLogger(__name__)


def _safe_print(text: str) -> None:
    """Print text safely on Windows consoles where encoding might be cp1251/cp1252."""
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sanitized = text.encode(encoding, errors="replace").decode(encoding)
        sys.stdout.write(sanitized + "\n")
        sys.stdout.flush()


def get_target_chat_ids() -> list[str]:
    """Retrieve target chat IDs from WATCH_CHAT_IDS or fallback to ADMIN_USER_IDS."""
    raw = os.getenv("WATCH_CHAT_IDS", "").strip()
    if raw:
        return [c.strip() for c in raw.split(",") if c.strip()]

    try:
        from .. import config
        admin_ids = getattr(config, "ADMIN_USER_IDS", set())
        if admin_ids:
            log.info("WATCH_CHAT_IDS belgilanmagan; ADMIN_USER_IDS ga yo'naltirilmoqda: %s", admin_ids)
            return [str(aid) for aid in admin_ids]
    except Exception as e:
        log.warning("ADMIN_USER_IDS ni o'qishda xatolik: %s", e)

    return []


def send_to_telegram(token: str, chat_id: str, text: str, timeout: int = 15) -> None:
    """Send HTML-formatted text via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram API xatosi HTTP {resp.status}")


def deliver(text: str, dry_run: bool = False) -> tuple[bool, str]:
    """Dispatch message: print to stdout if dry_run, otherwise send to Telegram chats."""
    if dry_run:
        _safe_print("\n" + "=" * 60)
        _safe_print(text)
        _safe_print("=" * 60 + "\n")
        return True, ""

    try:
        from .. import config
        token = getattr(config, "BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "")
    except Exception:
        token = os.getenv("BOT_TOKEN", "")

    if not token:
        err = "Telegram BOT_TOKEN topilmadi (.env yoki config)"
        log.error(err)
        return False, err

    chat_ids = get_target_chat_ids()
    if not chat_ids:
        err = "Yuborish uchun chat topilmadi (WATCH_CHAT_IDS va ADMIN_USER_IDS bo'sh)"
        log.error(err)
        return False, err

    errors = []
    for cid in chat_ids:
        try:
            send_to_telegram(token, cid, text)
        except Exception as e:
            err_msg = f"Chat {cid} ga yuborishda xato: {e}"
            log.error(err_msg)
            errors.append(err_msg)

    if errors:
        return False, "; ".join(errors)

    return True, ""
