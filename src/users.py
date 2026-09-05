"""Who may talk to the bot, and who may install.

Two tiers, deliberately unequal:
  admin  — the ids in ADMIN_USER_IDS (or, if that is unset, ALLOWED_USER_IDS).
           Only an admin can add or remove users, and only an admin can install.
  member — an id an admin added at runtime. Full read/ingest access, no install.

Admins live in .env and cannot be granted from inside Telegram: a chat message
must never be able to widen its own sender's privileges. Members live in a JSON
file so the admin can add them from a button without editing config or
restarting.
"""

import json
import logging
import threading

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_members: set | None = None


def _path():
    return config.DATA_DIR / "members.json"


def _load() -> set:
    global _members
    if _members is not None:
        return _members
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        _members = {int(x) for x in raw.get("members", [])}
    except FileNotFoundError:
        _members = set()
    except Exception as e:  # corrupted file must not lock the admin out
        log.warning("members.json unreadable (%s) — starting from empty", e)
        _members = set()
    return _members


def _save() -> None:
    _path().write_text(json.dumps({"members": sorted(_load())}, indent=1), encoding="utf-8")


def admins() -> set:
    return set(config.ADMIN_USER_IDS)


def members() -> set:
    with _lock:
        return set(_load())


def recipients() -> set:
    """Everyone who may receive a community broadcast: both tiers, one set."""
    with _lock:
        return set(config.ADMIN_USER_IDS) | set(config.ALLOWED_USER_IDS) | set(_load())


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_USER_IDS


def is_allowed(user_id: int) -> bool:
    if is_admin(user_id) or user_id in config.ALLOWED_USER_IDS:
        return True
    with _lock:
        return user_id in _load()


def add(user_id: int) -> bool:
    """True if newly added, False if this id already had access."""
    with _lock:
        current = _load()
        if user_id in current or user_id in config.ALLOWED_USER_IDS or is_admin(user_id):
            return False
        current.add(user_id)
        _save()
    log.info("users: added %d", user_id)
    return True


def remove(user_id: int) -> bool:
    """True if removed. An admin or a .env-listed id cannot be removed here —
    revoking those means editing .env, which is the point of keeping them there."""
    with _lock:
        current = _load()
        if user_id not in current:
            return False
        current.discard(user_id)
        _save()
    log.info("users: removed %d", user_id)
    return True
