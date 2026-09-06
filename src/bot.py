"""Telegram front end. Accepts an Instagram link or a forwarded video file."""

import asyncio
import hashlib
import html
import logging
import re
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
                           CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, InputMediaVideo, Message)

from . import archive, config, ingest, installer, kb, pipeline, queue_db, users
from .worker import (BROADCAST_MAX_CHARS, STAGE_LABELS, _broadcast, _keyboard,
                     _render_broadcast, _render_result, worker)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(config.LOG_DIR / "bot.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("bot")

ALBUM_DEBOUNCE_SEC = 1.5

dp = Dispatcher()
_albums: dict = {}  # media_group_id -> list[Message], drained by a debounce task


def _allowed(user_id: int) -> bool:
    return users.is_allowed(user_id)


def _is_admin(user_id: int) -> bool:
    return users.is_admin(user_id)


def _may_modify(user_id: int, shortcode: str) -> bool:
    """Who may delete or re-run an entry: an admin, or whoever contributed it.

    The base is shared, so any member could reach another member's entry and
    the 🗑 button erases both the ChromaDB rows and the markdown archive -
    unrecoverable. Membership is permission to add, not to erase.
    """
    if _is_admin(user_id):
        return True
    rec = archive.read(archive.path_for(shortcode))
    return bool(rec) and str(rec.get("author_id", "")) == str(user_id)


async def _deliver_existing(status, shortcode: str) -> None:
    """A second member sending the same thing must still see the result.

    The queue rejects the duplicate job (the work is already done), but the
    sender used to get only a bare "already known" line and no way to reach
    the entry — which is exactly the awareness gap this bot exists to close.
    """
    rec = archive.read(archive.path_for(shortcode))
    if rec is None:
        return await status.edit_text(
            f"ℹ️ <code>{html.escape(shortcode)}</code> allaqachon bazada "
            f"(arxiv fayli topilmadi).")
    rec.setdefault("shortcode", shortcode)
    rec["kb_rows"] = None  # arxivda yozuvlar soni saqlanmaydi — son to'qilmaydi
    try:
        await status.edit_text(
            "♻️ Bu allaqachon bazada — mana o'sha yozuv:\n\n" + _render_result(rec),
            reply_markup=_keyboard(shortcode))
    except Exception:
        log.exception("could not re-deliver %s", shortcode)
        await status.edit_text(
            f"ℹ️ <code>{html.escape(shortcode)}</code> allaqachon bazada.")


@dp.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return await message.answer(f"Ruxsat yo'q. Sizning ID: <code>{message.from_user.id}</code>")
    await message.answer(
        "Quyidagilarni tashlang — men o'qib, mag'zini ajratib, Graphify bazasiga "
        f"<code>project={html.escape(config.PROJECT_LABEL)}</code> ostida saqlayman:\n\n"
        "🔗 Video linki — Instagram, YouTube, TikTok, X, Reddit, Facebook, "
        "Vimeo, LinkedIn va boshqalar\n"
        "🎬 Video fayl (to'g'ridan-to'g'ri)\n"
        "🖼 Rasm(lar) + matn — Telegram post/kanal forward\n"
        "📝 Faqat matn (link bilan) — forward qilingan post\n\n"
        "/video &lt;link&gt; — ro'yxatda yo'q saytdan majburan yuklash\n"
        "/status — navbat holati\n"
        f"\nSizning ID: <code>{message.from_user.id}</code>"
        + ("\n\n<b>Admin:</b>\n/users — foydalanuvchilarni boshqarish\n"
           "/install &lt;shortcode&gt; — yozuvdagi vositani o'rnatish"
           if _is_admin(message.from_user.id) else ""),
        reply_markup=(InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="usrpanel")]])
            if _is_admin(message.from_user.id) else None),
    )


@dp.callback_query(F.data == "usrpanel")
async def on_users_panel(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        return await cb.answer("Faqat admin", show_alert=True)
    text, markup = _users_panel()
    await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return
    # Admins see the whole queue (they run the machine); a member sees only
    # their own submissions.
    rows = queue_db.recent(8, None if _is_admin(message.from_user.id) else message.chat.id)
    lines = [f"Navbatda: <b>{queue_db.pending_count()}</b>", ""]
    for r in rows:
        label = STAGE_LABELS.get(r["stage"], r["stage"])
        suffix = f" — {html.escape((r['error'] or '')[:80])}" if r["status"] == "failed" else ""
        lines.append(f"<code>{html.escape(r['shortcode'])}</code> {r['status']} / {label}{suffix}")
    await message.answer("\n".join(lines))


@dp.message(Command("search"))
async def cmd_search(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return
        
    args = message.text.split(" ", 1)
    if len(args) < 2 or not args[1].strip():
        # Bot runs in HTML parse mode — a literal "<so'rov>" would be read as an
        # (invalid) HTML tag and Telegram would reject the whole message.
        return await message.answer(
            "Foydalanish: <code>/search so'rov</code> yoki <code>/search all: so'rov</code>")
        
    query = args[1].strip()
    project = config.PROJECT_LABEL
    if query.lower().startswith("all:"):
        project = "all"
        query = query[4:].strip()
        
    if not query:
        return await message.answer("Qidiruv so'rovini kiriting.")
        
    status = await message.answer("⏳ Qidirilmoqda...")
    
    try:
        results = await asyncio.to_thread(kb.search, query, top_k=5, project=project)
    except Exception as e:
        return await status.edit_text(f"❌ Qidiruvda xato: {html.escape(str(e)[:300])}")
        
    if not results:
        return await status.edit_text("🔍 Hech narsa topilmadi. Boshqa so'z bilan urinib ko'ring.")
        
    lines = [f"🔍 Qidiruv natijalari (<b>{html.escape(query)}</b>):", ""]
    for r in results:
        sim = float(r.get("raw_similarity", r.get("similarity", 0)))
        lines.append(f"• <b>{html.escape(r.get('title', ''))}</b> (yaqinlik {sim:.2f})")
        lines.append(f"  <i>{html.escape(r.get('kind', ''))}</i> | <code>{html.escape(r.get('shortcode', ''))}</code>")
        if r.get("source"):
            lines.append(f"  🔗 <a href=\"{html.escape(r['source'], quote=True)}\">Manba</a>")
        snippet = r.get('snippet', '').replace('\n', ' ')
        lines.append(f"  📝 <i>{html.escape(snippet)}...</i>\n")
        
    await status.edit_text("\n".join(lines))


# Admin ids that tapped ➕ and owe the bot a user id. Every command below must
# be registered above on_text_post: that handler matches any text and swallows
# unknown "/..." messages, so a command declared after it never runs.
_awaiting_user_id: set = set()
# token -> taklifni yaratgan adminning id'si
_install_owner: dict = {}


@dp.message(Command("users"))
async def cmd_users(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    status = await message.answer("⏳ Kimga xabar yetishi tekshirilmoqda...")
    reachable = await _reachable_ids(message.bot, users.recipients())
    text, markup = _users_panel(reachable)
    await status.edit_text(text, reply_markup=markup)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return
    _awaiting_user_id.discard(message.from_user.id)
    await message.answer("Bekor qilindi.")


@dp.message(Command("install"))
async def cmd_install_entry(message: Message) -> None:
    await cmd_install(message)


def _author_info(user) -> tuple[str, str]:
    if not user:
        return "", ""
    name = f"@{user.username}" if user.username else (user.full_name or "")
    return str(user.id), name


async def _enqueue_url(message: Message, url: str) -> None:
    url = ingest.clean_url(url)
    shortcode = ingest.shortcode_from_url(url)
    if not shortcode:
        return await message.answer("Bu linkdan video kodini ajrata olmadim.")
    status = await message.answer(f"⏳ Navbatga qo'shildi: <code>{shortcode}</code>")
    prefix = "yt" if ingest.is_youtube_url(url) else "ig"
    author_id, author_name = _author_info(message.from_user)
    added = queue_db.enqueue(f"{prefix}:{shortcode}", shortcode, "url", url,
                             message.chat.id, status.message_id,
                             author_id=author_id, author_name=author_name)
    if not added:
        await _deliver_existing(status, shortcode)


@dp.message(Command("video"))
async def on_video_command(message: Message) -> None:
    """Force the video pipeline for a host not in ingest.MEDIA_HOSTS.

    yt-dlp supports far more sites than the router recognises, so rather than
    grow the host list for every one-off site, the user can name the link.
    """
    if not _allowed(message.from_user.id):
        return
    m = ingest.ANY_URL_RE.search(message.text or "")
    if not m:
        return await message.answer("Foydalanish: <code>/video &lt;link&gt;</code>")
    await _enqueue_url(message, m.group(0))


def _has_media_link(text: str) -> bool:
    """Anywhere in the message, case-insensitively.

    Not `F.text.regexp`: magic-filter anchors that at position 0 and recompiles
    the pattern without IGNORECASE, so a link pasted with a leading newline — or
    written `HTTPS://` — fell through to the article path and was fetched as a
    web page instead of downloaded as a video.
    """
    return bool(ingest.URL_RE.search(text or ""))


@dp.message(F.text.func(_has_media_link))
async def on_link(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return
    await _enqueue_url(message, ingest.URL_RE.search(message.text).group(0))


def _human_size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}MB"


async def _fetch_file(bot: Bot, file_id: str, dest: Path) -> None:
    """Pull a Telegram file to `dest`, whichever Bot API backend is in use.

    A self-hosted server has already written the file to its own working
    directory by the time get_file returns, so `file_path` is an absolute path
    and there is nothing to download — copying it beats re-fetching megabytes
    over HTTP from a server running on the same machine.
    """
    file = await bot.get_file(file_id)
    if config.TELEGRAM_API_LOCAL:
        src = Path(file.file_path)
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            return
    await bot.download_file(file.file_path, destination=dest)


@dp.message(F.video | F.document | F.video_note)
async def on_video(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return
    media = message.video or message.video_note or message.document
    shortcode = f"tg-{media.file_unique_id}"

    # file_size is optional in the Bot API and is often absent on exactly the
    # forwarded channel videos this guard exists for, so an unknown size is not
    # treated as small — the download below reports the limit if it trips.
    size = media.file_size
    if not config.TELEGRAM_API_LOCAL and size is not None and size > config.CLOUD_FILE_LIMIT:
        # Checked up front: the cloud Bot API rejects the get_file call itself,
        # so trying anyway only produces an opaque error after a pointless wait.
        return await message.answer(
            f"❌ Fayl juda katta: {_human_size(size)} "
            f"(Telegram Bot API chegarasi {_human_size(config.CLOUD_FILE_LIMIT)}).\n\n"
            f"Yechimlar:\n"
            f"• Video linkini tashlang — chegara qo'llanmaydi\n"
            f"• Yoki .env da <code>TELEGRAM_API_BASE</code> ni o'z Bot API serveringizga "
            f"yo'naltiring (2GB gacha) — README, \"Katta fayllar\" bo'limi"
        )

    status = await message.answer(f"⏳ Fayl qabul qilindi: <code>{shortcode}</code>")

    dest = config.MEDIA_DIR / f"{shortcode}.mp4"
    try:
        await _fetch_file(message.bot, media.file_id, dest)
    except Exception as e:
        log.exception("could not fetch file for %s", shortcode)
        hint = ("Fayl 20MB dan katta bo'lishi mumkin — video linkini tashlang, "
                "yoki .env da TELEGRAM_API_BASE ni sozlang."
                if not config.TELEGRAM_API_LOCAL else "Video linkini tashlab ko'ring.")
        return await status.edit_text(
            f"❌ Faylni yuklab bo'lmadi: {html.escape(str(e)[:200])}\n{hint}")

    if message.caption:
        # Picked up by ingest.adopt_file — the caption often holds the actual prompt.
        (config.MEDIA_DIR / f"{shortcode}.caption.txt").write_text(
            message.caption, encoding="utf-8")

    author_id, author_name = _author_info(message.from_user)
    added = queue_db.enqueue(f"file:{media.file_unique_id}", shortcode, "file", str(dest),
                             message.chat.id, status.message_id,
                             author_id=author_id, author_name=author_name)
    if not added:
        await _deliver_existing(status, shortcode)


@dp.message(F.photo & ~F.media_group_id)
async def on_photo(message: Message) -> None:
    """A single forwarded photo post (no album)."""
    if not _allowed(message.from_user.id):
        return
    await _handle_photos(message, [message], message.photo[-1].file_unique_id)


@dp.message(F.photo & F.media_group_id)
async def on_album_photo(message: Message) -> None:
    """One message of a multi-photo album post. Buffered until the burst settles,
    since Telegram delivers each photo as a separate update with no 'last one' marker."""
    if not _allowed(message.from_user.id):
        return
    gid = message.media_group_id
    _albums.setdefault(gid, []).append(message)
    if len(_albums[gid]) > 1:
        return  # a debounce task is already scheduled for this group

    async def flush():
        await asyncio.sleep(ALBUM_DEBOUNCE_SEC)
        msgs = _albums.pop(gid, [])
        if msgs:
            await _handle_photos(msgs[-1], msgs, gid)

    asyncio.create_task(flush())


async def _handle_photos(anchor: Message, messages: list, group_key: str) -> None:
    shortcode = f"tg-{group_key}"
    status = await anchor.answer(f"⏳ Post qabul qilindi ({len(messages)} rasm): <code>{shortcode}</code>")

    photo_dir = config.MEDIA_DIR / shortcode / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    caption = next((m.caption for m in messages if m.caption), "")

    try:
        for i, m in enumerate(messages):
            await _fetch_file(m.bot, m.photo[-1].file_id, photo_dir / f"{i:02d}.jpg")
    except Exception as e:
        return await status.edit_text(f"❌ Rasmni yuklab bo'lmadi: {html.escape(str(e)[:200])}")

    if caption:
        (photo_dir / "caption.txt").write_text(caption, encoding="utf-8")

    author_id, author_name = _author_info(anchor.from_user)
    added = queue_db.enqueue(f"post:{group_key}", shortcode, "photo", str(photo_dir),
                             anchor.chat.id, status.message_id,
                             author_id=author_id, author_name=author_name)
    if not added:
        await _deliver_existing(status, shortcode)


@dp.message(F.text & ~F.text.func(_has_media_link) & ~F.via_bot)
async def on_text_post(message: Message) -> None:
    """A forwarded text post, or a bare link to a page worth reading."""
    if not _allowed(message.from_user.id):
        return

    # The number an admin sends right after tapping ➕. Handled here rather than
    # in its own handler because this one already catches every plain text
    # message; the pending flag is cleared either way, so a typo cannot leave
    # the admin stuck answering a question the bot is no longer asking.
    if message.from_user.id in _awaiting_user_id:
        _awaiting_user_id.discard(message.from_user.id)
        if not _is_admin(message.from_user.id):
            return
        raw = message.text.strip()
        if not re.fullmatch(r"-?\d{1,20}", raw):
            return await message.answer(
                "Bu raqamga o'xshamaydi. /users dan qayta urinib ko'ring.")
        added = users.add(int(raw))
        head = "✅ Qo'shildi" if added else "ℹ️ Allaqachon ruxsati bor"
        await message.answer(f"{head}: <code>{raw}</code>")
        text, markup = _users_panel()
        return await message.answer(text, reply_markup=markup)

    if message.text.startswith("/"):
        return

    if ingest.is_bare_link(message.text):
        url = ingest.clean_url(message.text.strip())
        shortcode = ingest.shortcode_from_url(url)
        if not shortcode:
            return await message.answer("Bu linkdan sahifa kodini ajrata olmadim.")
        status = await message.answer(f"⏳ Sahifa o'qilmoqda: <code>{shortcode}</code>")
        # Same job-id prefix as the video path: one URL must not be able to run
        # twice under `web:` and `ig:` and overwrite its own archive entry.
        author_id, author_name = _author_info(message.from_user)
        added = queue_db.enqueue(f"ig:{shortcode}", shortcode, "article", url,
                                 message.chat.id, status.message_id,
                                 author_id=author_id, author_name=author_name)
        if not added:
            await _deliver_existing(status, shortcode)
        return

    # Was tg-{chat_id}-{message_id}: per-sender, so the same text forwarded by
    # two members produced two ids, two full Ollama passes and two entries.
    # Hashing the content makes the id the same for everyone.
    _digest = hashlib.sha1(" ".join(message.text.split()).encode("utf-8")).hexdigest()[:12]
    shortcode = f"tg-txt-{_digest}"
    status = await message.answer(f"⏳ Post qabul qilindi: <code>{shortcode}</code>")
    author_id, author_name = _author_info(message.from_user)
    added = queue_db.enqueue(f"post:{shortcode}", shortcode, "text", message.text,
                             message.chat.id, status.message_id,
                             author_id=author_id, author_name=author_name)
    if not added:
        await _deliver_existing(status, shortcode)


@dp.callback_query(F.data.startswith("del:"))
async def on_delete(cb: CallbackQuery) -> None:
    if not _allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    shortcode = cb.data.split(":", 1)[1]
    if not await asyncio.to_thread(_may_modify, cb.from_user.id, shortcode):
        return await cb.answer("Bu yozuv sizniki emas — o'chirish faqat muallif "
                               "yoki admin uchun", show_alert=True)
    # The archive is deleted even when the ChromaDB delete fails: leaving the
    # two halves disagreeing is worse than either outcome, and reindex would
    # otherwise resurrect rows the user asked to remove.
    db_error = None
    try:
        await asyncio.to_thread(kb.delete_shortcode, shortcode)
    except Exception as e:
        db_error = e
        log.exception("delete %s: kb.delete_shortcode failed", shortcode)
    archive.path_for(shortcode).unlink(missing_ok=True)
    if db_error is None:
        await cb.message.edit_text(
            f"🗑 <code>{html.escape(shortcode)}</code> bazadan o'chirildi.\n"
            f"MD arxiv fayli ham o'chirildi."
        )
        return await cb.answer("O'chirildi")
    await cb.message.edit_text(
        f"⚠️ <code>{html.escape(shortcode)}</code>: arxiv fayli o'chirildi, "
        f"lekin bazadan o'chirishda xato — qayta urinib ko'ring.\n"
        f"<i>{html.escape(str(db_error)[:200])}</i>")
    await cb.answer("Qisman o'chirildi", show_alert=True)


@dp.callback_query(F.data.startswith("re:"))
async def on_reprocess(cb: CallbackQuery) -> None:
    if not _allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    shortcode = cb.data.split(":", 1)[1]
    if not await asyncio.to_thread(_may_modify, cb.from_user.id, shortcode):
        return await cb.answer("Bu yozuv sizniki emas — qayta ishlash faqat muallif "
                               "yoki admin uchun", show_alert=True)
    await cb.answer("Qayta ishlanmoqda...")
    author_id, author_name = _author_info(cb.from_user)
    try:
        rec = await asyncio.to_thread(pipeline.reprocess, shortcode, author_id=author_id, author_name=author_name)
    except Exception as e:
        log.exception("reprocess failed")
        return await cb.message.edit_text(f"❌ Qayta ishlashda xato: {html.escape(str(e)[:300])}")
    await cb.message.edit_text(_render_result(rec), reply_markup=_keyboard(shortcode))


async def _reachable_ids(bot: Bot, ids) -> set:
    """Which members the bot may actually message.

    A Telegram bot cannot open a conversation: until the person presses
    /start once, every send to them fails with "chat not found" — so an id
    sitting in ALLOWED_USER_IDS is permission, not reach. get_chat probes
    this without sending the person anything.
    """
    out = set()
    for uid in ids:
        try:
            await bot.get_chat(uid)
            out.add(uid)
        except Exception:
            pass
    return out


def _users_panel(reachable: set | None = None) -> tuple:
    admin_ids = sorted(users.admins())
    env_ids = sorted(config.ALLOWED_USER_IDS - users.admins())
    member_ids = sorted(users.members())

    def mark(i: int) -> str:
        if reachable is None:
            return ""
        return " ✅" if i in reachable else " ⚠️ /start bosmagan"

    lines = ["<b>👥 Foydalanuvchilar</b>", "",
             "<b>Adminlar</b> (faqat .env orqali o'zgaradi, o'rnata oladi):"]
    lines += [f"• <code>{i}</code>{mark(i)}" for i in admin_ids] or ["• —"]
    if env_ids:
        lines += ["", "<b>.env dagi foydalanuvchilar</b>:"]
        lines += [f"• <code>{i}</code>{mark(i)}" for i in env_ids]
    lines += ["", "<b>Qo'shilganlar</b> (o'rnata olmaydi):"]
    lines += [f"• <code>{i}</code>{mark(i)}" for i in member_ids] or ["• hali yo'q"]
    if reachable is not None:
        missing = len([i for i in admin_ids + env_ids + member_ids if i not in reachable])
        if missing:
            lines += ["", f"<i>⚠️ {missing} kishi botga hali /start bosmagan — ularga "
                          f"yangilik xabari BORMAYDI. Telegram boti suhbatni o'zi "
                          f"boshlay olmaydi, odam bir marta yozishi shart.</i>"]

    rows = [[InlineKeyboardButton(text="➕ Foydalanuvchi qo'shish", callback_data="usradd")]]
    rows += [[InlineKeyboardButton(text=f"❌ {i} ni o'chirish", callback_data=f"usrdel:{i}")]
             for i in member_ids]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "usradd")
async def on_user_add(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        return await cb.answer("Faqat admin", show_alert=True)
    _awaiting_user_id.add(cb.from_user.id)
    await cb.message.answer(
        "Qo'shiladigan Telegram user ID sini yuboring (faqat raqam).\n"
        "Foydalanuvchi o'z ID sini botga <code>/start</code> yozib bilib oladi.\n\n"
        "Bekor qilish: /cancel")
    await cb.answer()


@dp.callback_query(F.data.startswith("usrdel:"))
async def on_user_del(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        return await cb.answer("Faqat admin", show_alert=True)
    try:
        target = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer("Noto'g'ri ID", show_alert=True)
    removed = users.remove(target)
    text, markup = _users_panel()
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer("O'chirildi" if removed else "Topilmadi")


# token -> Candidate, filled by /install and drained by the confirm button.
# Deliberately in-memory: a restart forgets every pending offer, so a stale
# button can never install something the user has moved on from.
_install_offers: dict = {}


async def cmd_install(message: Message) -> None:
    """Offer to install what an archived entry talks about. Nothing runs yet."""
    if not _is_admin(message.from_user.id):
        return await message.answer("⛔ O'rnatish faqat admin uchun.")
    if not config.INSTALL_ENABLED:
        return await message.answer(
            "⛔ O'rnatish o'chirilgan.\n"
            "Yoqish uchun <code>.env</code> da <code>INSTALL_ENABLED=true</code> qiling.\n\n"
            "⚠️ Diqqat: bu buyruq internetdan kelgan matn asosida kompyuteringizda "
            "haqiqiy buyruq bajaradi. Faqat o'zingiz ishonadigan manbalar uchun yoqing."
        )

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer(
            "Foydalanish: <code>/install &lt;shortcode&gt;</code>\n"
            "Shortcode natija xabarining yuqorisida turadi."
        )
    shortcode = parts[1].strip()

    if not archive.path_for(shortcode).exists():
        return await message.answer(f"❌ <code>{html.escape(shortcode)}</code> arxivda topilmadi.")

    cands = await asyncio.to_thread(installer.candidates_for, shortcode)
    if not cands:
        return await message.answer(
            "Bu yozuvda o'rnatsa bo'ladigan narsa topilmadi.\n"
            "Qo'llab-quvvatlanadi: GitHub repo, npm, pip, ollama."
        )

    rows = []
    for cand in cands:
        _install_offers[cand.token] = cand
        # Two admins share one process: without an owner the second could
        # confirm or cancel a command the first is still looking at.
        _install_owner[cand.token] = message.from_user.id
        blocker = installer.preflight(cand)
        rows.append([InlineKeyboardButton(
            text=(f"⚠️ {cand.label}" if blocker else cand.label),
            callback_data=f"ins:{cand.token}")])

    await message.answer(
        f"<b>{html.escape(shortcode)}</b> — nimani o'rnatamiz?\n\n"
        f"Tugmani bosgach aniq buyruq ko'rsatiladi, bajarilmaydi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("ins:"))
async def on_install_offer(cb: CallbackQuery) -> None:
    """First tap: show the exact argv and ask for confirmation."""
    if not _is_admin(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    cand = _install_offers.get(cb.data.split(":", 1)[1])
    if cand is None:
        return await cb.answer("Bu taklif eskirgan, /install ni qayta yuboring", show_alert=True)

    blocker = installer.preflight(cand)
    text = [f"<b>{html.escape(cand.label)}</b>", "",
            "Bajariladigan buyruq:", f"<pre>{html.escape(cand.command)}</pre>"]
    if cand.cwd:
        text.append(f"Papka: <code>{html.escape(str(cand.cwd))}</code>")
    if blocker:
        text += ["", f"⚠️ {blocker}"]
        await cb.message.edit_text("\n".join(text))
        return await cb.answer()

    text += ["", "Bajarilsinmi?"]
    await cb.message.edit_text("\n".join(text), reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha, bajar", callback_data=f"insgo:{cand.token}"),
            InlineKeyboardButton(text="✖️ Bekor", callback_data=f"inscancel:{cand.token}"),
        ]]))
    await cb.answer()


@dp.callback_query(F.data.startswith("inscancel:"))
async def on_install_cancel(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    token = cb.data.split(":", 1)[1]
    if _install_owner.get(token) not in (None, cb.from_user.id):
        return await cb.answer("Bu taklifni boshqa admin ochgan", show_alert=True)
    _install_owner.pop(token, None)
    _install_offers.pop(token, None)
    await cb.message.edit_text("✖️ Bekor qilindi.")
    await cb.answer()


@dp.callback_query(F.data.startswith("insgo:"))
async def on_install_run(cb: CallbackQuery) -> None:
    """Second tap: actually run it. The token is consumed so it cannot repeat.

    Admin-only, like every step of the install flow: /install itself is
    gated on _is_admin, so gating the button that actually runs the command
    on mere membership was the weaker check of the two. It matters the
    moment the bot is added to a group, where any member can tap it.
    """
    if not _is_admin(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    token = cb.data.split(":", 1)[1]
    if _install_owner.get(token) not in (None, cb.from_user.id):
        return await cb.answer("Bu taklifni boshqa admin ochgan", show_alert=True)
    _install_owner.pop(token, None)
    cand = _install_offers.pop(token, None)
    if cand is None:
        return await cb.answer("Bu taklif eskirgan, /install ni qayta yuboring", show_alert=True)

    await cb.answer("Bajarilmoqda...")
    await cb.message.edit_text(
        f"⏳ Bajarilmoqda:\n<pre>{html.escape(cand.command)}</pre>")
    ok, output = await asyncio.to_thread(installer.run, cand)

    head = "✅ Bajarildi" if ok else "❌ Xato"
    where = f"\nPapka: <code>{html.escape(cand.note)}</code>" if cand.note and ok else ""
    await cb.message.edit_text(
        f"{head}: <pre>{html.escape(cand.command)}</pre>{where}\n\n"
        f"<pre>{html.escape(output[:1200])}</pre>")


_BASE_COMMANDS = [
    ("start", "Yordam va sizning ID"),
    ("status", "Navbat holati"),
    ("search", "Bazadan qidirish"),
    ("video", "Linkdan majburan video yuklash"),
]
_ADMIN_COMMANDS = [
    ("users", "Foydalanuvchilarni boshqarish"),
    ("install", "Yozuvdagi vositani o'rnatish"),
]


async def _publish_commands(bot: Bot) -> None:
    """Fill Telegram's command menu — without this the bot has no menu at all.

    Admin entries go out per-chat: BotCommandScopeChat is the only scope that
    shows a command to some users and not others, so /users never appears in a
    regular member's menu.
    """
    base = [BotCommand(command=c, description=d) for c, d in _BASE_COMMANDS]
    await bot.set_my_commands(base, scope=BotCommandScopeDefault())

    admin = base + [BotCommand(command=c, description=d) for c, d in _ADMIN_COMMANDS]
    for admin_id in users.admins():
        try:
            await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            # An admin who has never opened a chat with the bot has no chat to
            # scope to; the default menu still works for them.
            log.info("could not set admin menu for %s: %s", admin_id, e)

async def _notify_restart_failed(bot: Bot, jobs: list[dict]) -> None:
    for job in jobs:
        chat_id = job.get("chat_id")
        message_id = job.get("message_id")
        sc = job.get("shortcode") or ""
        if not chat_id or not message_id:
            continue
        markup = None
        if archive.path_for(sc).exists():
            markup = _keyboard(sc)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"❌ <code>{html.escape(sc)}</code> — bot qayta ishga tushdi, urinishlar tugadi",
                reply_markup=markup,
            )
        except Exception:
            log.warning("could not report restart failure for %s (chat %s)", sc, chat_id)


# Bu ikkalasi HAMMA ishlovchidan keyin ro'yxatdan o'tadi — aiogram filtrlarni
# ro'yxatdan o'tish tartibida sinaydi, shuning yuqoridagilar to'silmaydi.

@dp.edited_message(F.text.func(_has_media_link))
async def on_edited_link(message: Message) -> None:
    """Tahrirlangan xabardagi havola. `@dp.message` tahrirlarni KO'RMAYDI —
    ular alohida `edited_message` oqimiga boradi. 2026-09-05 da bitta havola
    shu sababdan jimgina yo'qolgan bo'lishi mumkin (update 613275562
    "is not handled", 0 ms)."""
    if not _allowed(message.from_user.id):
        return
    await _enqueue_url(message, ingest.URL_RE.search(message.text).group(0))


@dp.message()
async def on_unhandled(message: Message) -> None:
    """Hech qaysi filtr tanimagan xabar. Busiz bot MUTLAQO jim qoladi va
    foydalanuvchi havolasi qabul qilinmaganini bilmaydi."""
    log.warning(
        "tanilmagan xabar: chat=%s type=%s text=%r caption=%r via_bot=%s",
        message.chat.id, message.content_type,
        (message.text or "")[:120], (message.caption or "")[:120],
        bool(message.via_bot),
    )
    if not _allowed(message.from_user.id):
        return
    text = message.text or message.caption or ""
    m = ingest.URL_RE.search(text) or ingest.ANY_URL_RE.search(text)
    if m:
        return await _enqueue_url(message, m.group(0))
    await message.answer(
        "❓ Bu xabarni tushunmadim. Havola yuboring yoki /help ni bosing."
    )


async def main() -> None:
    import sys
    import os
    import msvcrt
    
    lock_file = config.DATA_DIR / "bot.lock"
    try:
        lock_fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o666)
        msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        msg = f"Bot allaqachon ishlayapti (qulf: {lock_file}). Ikkinchi nusxa ishga tushmadi."
        print(msg, file=sys.stderr)
        log.error(msg)
        sys.exit(3)

    try:
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode("utf-8"))

        if not config.BOT_TOKEN:
            raise SystemExit("BOT_TOKEN .env faylida ko'rsatilmagan")
        if not config.ALLOWED_USER_IDS:
            raise SystemExit("ALLOWED_USER_IDS bo'sh — bot hech kimga javob bermaydi")

        queue_db.init()
        failed_jobs = queue_db.requeue_running()
        if failed_jobs:
            log.info("found %d failed job(s) interrupted by restart", len(failed_jobs))

        session = None
        if config.TELEGRAM_API_LOCAL:
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(config.TELEGRAM_API_BASE, is_local=True))
            log.info("Bot API: self-hosted at %s (files up to 2GB)", config.TELEGRAM_API_BASE)

        bot = Bot(config.BOT_TOKEN, session=session,
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await _publish_commands(bot)
        if failed_jobs:
            await _notify_restart_failed(bot, failed_jobs)
        asyncio.create_task(worker(bot))
        await dp.start_polling(bot)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
