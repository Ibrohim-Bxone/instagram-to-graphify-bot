"""Telegram front end. Accepts an Instagram link or a forwarded video file."""

import asyncio
import html
import logging
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, InputMediaVideo, Message)

from . import archive, config, ingest, kb, pipeline, queue_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(config.LOG_DIR / "bot.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("bot")

STAGE_LABELS = {
    "queued": "⏳ Navbatda",
    "downloading": "📥 Yuklanmoqda",
    "transcribing": "🎧 Transkripsiya",
    "vision": "🖼 Kadrlar o'qilmoqda",
    "extracting": "🧠 Mag'z ajratilmoqda",
    "saving": "💾 Saqlanmoqda",
}
ALBUM_DEBOUNCE_SEC = 1.5

dp = Dispatcher()
_albums: dict = {}  # media_group_id -> list[Message], drained by a debounce task


def _allowed(user_id: int) -> bool:
    return user_id in config.ALLOWED_USER_IDS


def _keyboard(shortcode: str) -> InlineKeyboardMarkup:
    # Only the shortcode travels in callback_data — it stays valid across restarts.
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"del:{shortcode}"),
        InlineKeyboardButton(text="🔄 Qayta ishlash", callback_data=f"re:{shortcode}"),
    ]])


def _render_result(rec: dict) -> str:
    if not rec.get("usable"):
        return (f"⚠️ <b>{html.escape(rec.get('shortcode', ''))}</b> — foydali mazmun topilmadi, "
                f"bazaga saqlanmadi.\nTranskript arxivda qoldi:\n<code>"
                f"{html.escape(rec.get('md_path', ''))}</code>")

    parts = [f"✅ <b>{html.escape(rec.get('title_en', '') or rec['shortcode'])}</b>",
             f"<i>{html.escape(rec.get('content_type', 'other'))}</i>"]
    if rec.get("url"):
        parts.append(f"🔗 <a href=\"{html.escape(rec['url'], quote=True)}\">Instagram manbasi</a> · <code>{html.escape(rec['shortcode'])}</code>")
    if rec.get("summary_uz"):
        parts.append("")
        parts += [f"• {html.escape(s)}" for s in rec["summary_uz"][:6]]
    if rec.get("items"):
        parts.append("")
        for item in rec["items"][:6]:
            mark = "✅" if item.get("verified") else "⚠️"
            parts.append(f"{mark} <code>{html.escape(item.get('kind', '?'))}</code> "
                         f"{html.escape(item.get('name_en', '?'))}")
    if rec.get("apply_suggestions_uz"):
        parts.append("\n<b>Qo'llash tavsiyasi</b> <i>(qo'llanilmadi)</i>:")
        parts += [f"• {html.escape(s)}" for s in rec["apply_suggestions_uz"][:3]]
    if rec.get("flags"):
        parts.append(f"\n<i>Bayroqlar: {html.escape(', '.join(rec['flags']))}</i>")
    
    if rec.get("duplicate_of"):
        dup = rec["duplicate_of"]
        parts.append(f"\n🔁 Eslatma: shunga o'xshash narsa allaqachon bazada bor — \"{html.escape(dup.get('title', ''))}\" (<code>{html.escape(dup.get('shortcode', ''))}</code>), o'xshashlik {int(dup.get('similarity', 0)*100)}%.")

    parts.append(f"\n💾 Graphify: {rec.get('kb_rows', 0)} ta yozuv "
                 f"(<code>project={html.escape(config.PROJECT_LABEL)}</code>)")
    return "\n".join(parts)


@dp.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return await message.answer(f"Ruxsat yo'q. Sizning ID: <code>{message.from_user.id}</code>")
    await message.answer(
        "Quyidagilarni tashlang — men o'qib, mag'zini ajratib, Graphify bazasiga "
        f"<code>project={html.escape(config.PROJECT_LABEL)}</code> ostida saqlayman:\n\n"
        "🔗 Instagram reel linki\n"
        "🎬 Video fayl (to'g'ridan-to'g'ri)\n"
        "🖼 Rasm(lar) + matn — Telegram post/kanal forward\n"
        "📝 Faqat matn (link bilan) — forward qilingan post\n\n"
        "/status — navbat holati"
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return
    rows = queue_db.recent(8)
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
        sim = int(r.get("similarity", 0) * 100)
        lines.append(f"• <b>{html.escape(r.get('title', ''))}</b> ({sim}%)")
        lines.append(f"  <i>{html.escape(r.get('kind', ''))}</i> | <code>{html.escape(r.get('shortcode', ''))}</code>")
        if r.get("source"):
            lines.append(f"  🔗 <a href=\"{html.escape(r['source'], quote=True)}\">Manba</a>")
        snippet = r.get('snippet', '').replace('\n', ' ')
        lines.append(f"  📝 <i>{html.escape(snippet)}...</i>\n")
        
    await status.edit_text("\n".join(lines))


@dp.message(F.text.regexp(ingest.URL_RE.pattern))
async def on_link(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return
    url = ingest.clean_url(ingest.URL_RE.search(message.text).group(0))
    shortcode = ingest.shortcode_from_url(url)
    if not shortcode:
        return await message.answer("Bu linkdan reel kodini ajrata olmadim.")
    status = await message.answer(f"⏳ Navbatga qo'shildi: <code>{shortcode}</code>")
    prefix = "yt" if ingest.is_youtube_url(url) else "ig"
    added = queue_db.enqueue(f"{prefix}:{shortcode}", shortcode, "url", url,
                             message.chat.id, status.message_id)
    if not added:
        await status.edit_text(f"ℹ️ <code>{shortcode}</code> allaqachon bazada. "
                               f"Qayta ishlash uchun /status dan foydalaning.")


@dp.message(F.video | F.document | F.video_note)
async def on_video(message: Message) -> None:
    if not _allowed(message.from_user.id):
        return
    media = message.video or message.video_note or message.document
    shortcode = f"tg-{media.file_unique_id}"
    status = await message.answer(f"⏳ Fayl qabul qilindi: <code>{shortcode}</code>")

    dest = config.MEDIA_DIR / f"{shortcode}.mp4"
    try:
        file = await message.bot.get_file(media.file_id)
        await message.bot.download_file(file.file_path, destination=dest)
    except Exception as e:
        # Bot API caps downloads at 20MB unless a local Bot API server is used.
        return await status.edit_text(
            f"❌ Faylni yuklab bo'lmadi: {html.escape(str(e)[:200])}\n"
            f"20MB dan katta bo'lsa, linkini tashlang."
        )

    if message.caption:
        # Picked up by ingest.adopt_file — the caption often holds the actual prompt.
        (config.MEDIA_DIR / f"{shortcode}.caption.txt").write_text(
            message.caption, encoding="utf-8")

    added = queue_db.enqueue(f"file:{media.file_unique_id}", shortcode, "file", str(dest),
                             message.chat.id, status.message_id)
    if not added:
        await status.edit_text(f"ℹ️ Bu fayl allaqachon qayta ishlangan: <code>{shortcode}</code>")


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
            file = await m.bot.get_file(m.photo[-1].file_id)
            await m.bot.download_file(file.file_path, destination=photo_dir / f"{i:02d}.jpg")
    except Exception as e:
        return await status.edit_text(f"❌ Rasmni yuklab bo'lmadi: {html.escape(str(e)[:200])}")

    if caption:
        (photo_dir / "caption.txt").write_text(caption, encoding="utf-8")

    added = queue_db.enqueue(f"post:{group_key}", shortcode, "photo", str(photo_dir),
                             anchor.chat.id, status.message_id)
    if not added:
        await status.edit_text(f"ℹ️ Bu post allaqachon qayta ishlangan: <code>{shortcode}</code>")


@dp.message(F.text & ~F.text.regexp(ingest.URL_RE.pattern) & ~F.via_bot)
async def on_text_post(message: Message) -> None:
    """A forwarded text-only post — usually a news/tip blurb with a read-more link."""
    if not _allowed(message.from_user.id):
        return
    if message.text.startswith("/"):
        return
    shortcode = f"tg-{message.chat.id}-{message.message_id}"
    status = await message.answer(f"⏳ Post qabul qilindi: <code>{shortcode}</code>")
    added = queue_db.enqueue(f"post:{shortcode}", shortcode, "text", message.text,
                             message.chat.id, status.message_id)
    if not added:
        await status.edit_text(f"ℹ️ Bu post allaqachon qayta ishlangan: <code>{shortcode}</code>")


@dp.callback_query(F.data.startswith("del:"))
async def on_delete(cb: CallbackQuery) -> None:
    if not _allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    shortcode = cb.data.split(":", 1)[1]
    await asyncio.to_thread(kb.delete_shortcode, shortcode)
    archive.path_for(shortcode).unlink(missing_ok=True)
    await cb.message.edit_text(
        f"🗑 <code>{html.escape(shortcode)}</code> bazadan o'chirildi.\n"
        f"MD arxiv fayli ham o'chirildi."
    )
    await cb.answer("O'chirildi")


@dp.callback_query(F.data.startswith("re:"))
async def on_reprocess(cb: CallbackQuery) -> None:
    if not _allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    shortcode = cb.data.split(":", 1)[1]
    await cb.answer("Qayta ishlanmoqda...")
    try:
        rec = await asyncio.to_thread(pipeline.reprocess, shortcode)
    except Exception as e:
        log.exception("reprocess failed")
        return await cb.message.edit_text(f"❌ Qayta ishlashda xato: {html.escape(str(e)[:300])}")
    await cb.message.edit_text(_render_result(rec), reply_markup=_keyboard(shortcode))


async def worker(bot: Bot) -> None:
    """Single sequential worker: whisper and Ollama cannot share 8GB of VRAM."""
    loop = asyncio.get_running_loop()
    while True:
        job = queue_db.claim_next()
        if job is None:
            await asyncio.sleep(3)
            continue

        def progress(stage: str, job=job) -> None:
            asyncio.run_coroutine_threadsafe(
                bot.edit_message_text(
                    chat_id=job["chat_id"], message_id=job["message_id"],
                    text=f"{STAGE_LABELS.get(stage, stage)} — <code>{job['shortcode']}</code>",
                ),
                loop,
            )

        try:
            record = await loop.run_in_executor(None, lambda: pipeline.process(job, progress, keep_media=True))
            media_paths = [Path(p) for p in record.pop("media_paths", []) if Path(p).exists()]
            if media_paths:
                try:
                    caption = _render_result(record)[:1024]
                    if len(media_paths) == 1:
                        path = media_paths[0]
                        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                            await bot.send_photo(job["chat_id"], FSInputFile(path), caption=caption)
                        else:
                            await bot.send_video(job["chat_id"], FSInputFile(path),
                                                 caption=caption, supports_streaming=True)
                    else:
                        for offset in range(0, len(media_paths), 10):
                            chunk = media_paths[offset:offset + 10]
                            if len(chunk) == 1:
                                path = chunk[0]
                                if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                                    await bot.send_photo(job["chat_id"], FSInputFile(path))
                                else:
                                    await bot.send_video(job["chat_id"], FSInputFile(path),
                                                         supports_streaming=True)
                                continue
                            group = []
                            for index, path in enumerate(chunk):
                                item_caption = caption if offset == 0 and index == 0 else None
                                media = FSInputFile(path)
                                if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                                    group.append(InputMediaPhoto(media=media, caption=item_caption))
                                else:
                                    group.append(InputMediaVideo(media=media, caption=item_caption,
                                                                 supports_streaming=True))
                            await bot.send_media_group(job["chat_id"], media=group)
                finally:
                    shutil.rmtree(media_paths[0].parent, ignore_errors=True)
            queue_db.finish(job["id"], {"kb_rows": record.get("kb_rows", 0), "md_path": record.get("md_path", "")})
            await bot.edit_message_text(
                chat_id=job["chat_id"], message_id=job["message_id"],
                text=_render_result(record), reply_markup=_keyboard(job["shortcode"]),
            )
        except Exception as e:
            log.exception("job %s failed", job["id"])
            status = queue_db.fail(job["id"], f"{type(e).__name__}: {e}")
            note = "qayta urinaman" if status == "queued" else "to'xtatildi"
            try:
                await bot.edit_message_text(
                    chat_id=job["chat_id"], message_id=job["message_id"],
                    text=f"❌ <code>{html.escape(job['shortcode'])}</code> — {note}\n"
                         f"<i>{html.escape(str(e)[:300])}</i>",
                )
            except Exception:
                pass


async def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env faylida ko'rsatilmagan")
    if not config.ALLOWED_USER_IDS:
        raise SystemExit("ALLOWED_USER_IDS bo'sh — bot hech kimga javob bermaydi")

    queue_db.init()
    requeued = queue_db.requeue_running()
    if requeued:
        log.info("requeued %d job(s) interrupted by a restart", requeued)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(worker(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
