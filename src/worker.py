"""Background worker loop and delivery logic."""

import asyncio
import html
import logging
import re
import shutil
import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, InputMediaVideo)

from . import archive, config, pipeline, queue_db, users

log = logging.getLogger("worker")

STAGE_LABELS = {
    "queued": "⏳ Navbatda",
    "downloading": "📥 Yuklanmoqda",
    "transcribing": "🎧 Transkripsiya",
    "vision": "🖼 Kadrlar o'qilmoqda",
    "extracting": "🧠 Mag'z ajratilmoqda",
    "saving": "💾 Saqlanmoqda",
}


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

    if rec.get("kb_rows") is not None:
        parts.append(f"\n💾 Graphify: {rec['kb_rows']} ta yozuv "
                     f"(<code>project={html.escape(config.PROJECT_LABEL)}</code>)")
    return "\n".join(parts)


# Telegram caps a message at 4096 characters; leave room for the header.
BROADCAST_MAX_CHARS = 3900
# Telegram tolerates roughly 30 messages a second; a small gap keeps a large
# community well under that instead of discovering the limit as errors.
BROADCAST_GAP_SEC = 0.05


def _cut_on_line_boundary(text: str, limit: int, hint: str = "") -> str:
    """Cut text on a line boundary to fit within limit, preserving HTML tag balance.

    If text exceeds limit, lines are dropped from the end so that half-written
    HTML tags never reach Telegram. If provided, hint is appended.
    """
    if len(text) <= limit:
        return text

    if hint:
        if hint.startswith("<") or hint.startswith("…") or hint.startswith("..."):
            suffix = f"\n\n{hint}"
        else:
            suffix = f"\n\n<i>…to'lig'i: <code>/search {hint}</code></i>"
    else:
        suffix = ""

    max_len = limit - len(suffix)
    if max_len < 0:
        max_len = limit
        suffix = ""

    kept = []
    used = 0
    for line in text.split("\n"):
        needed = len(line) + (1 if kept else 0)
        if used + needed > max_len:
            break
        kept.append(line)
        used += needed

    if kept:
        return "\n".join(kept) + suffix

    # Fallback: if even the first line exceeds max_len, truncate safely
    # without cutting inside an HTML tag and close any unclosed tags.
    cut = text[:max_len]
    last_lt = cut.rfind("<")
    last_gt = cut.rfind(">")
    if last_lt > last_gt:
        cut = cut[:last_lt]

    open_tags = []
    for m in re.finditer(r"<(/?[a-zA-Z]+)(?:\s+[^>]*)?>", cut):
        tag = m.group(1)
        if tag.startswith("/"):
            tag_name = tag[1:].lower()
            if open_tags and open_tags[-1] == tag_name:
                open_tags.pop()
        else:
            open_tags.append(tag.lower())

    closing = "".join(f"</{t}>" for t in reversed(open_tags))
    while len(cut) + len(closing) + len(suffix) > limit and cut:
        cut = cut[:-1]
        last_lt = cut.rfind("<")
        last_gt = cut.rfind(">")
        if last_lt > last_gt:
            cut = cut[:last_lt]
        open_tags = []
        for m in re.finditer(r"<(/?[a-zA-Z]+)(?:\s+[^>]*)?>", cut):
            tag = m.group(1)
            if tag.startswith("/"):
                tag_name = tag[1:].lower()
                if open_tags and open_tags[-1] == tag_name:
                    open_tags.pop()
            else:
                open_tags.append(tag.lower())
        closing = "".join(f"</{t}>" for t in reversed(open_tags))

    return cut + closing + suffix


def _render_caption(rec: dict) -> str:
    """Short media caption: title, content type, and source link.

    Guaranteed to fit Telegram's 1024-character caption limit via
    _cut_on_line_boundary, leaving the full result card for the follow-up
    message.
    """
    if not rec.get("usable"):
        return f"⚠️ <b>{html.escape(rec.get('shortcode', ''))}</b>"

    parts = [f"✅ <b>{html.escape(rec.get('title_en', '') or rec.get('shortcode', ''))}</b>",
             f"<i>{html.escape(rec.get('content_type', 'other'))}</i>"]
    if rec.get("url"):
        parts.append(f"🔗 <a href=\"{html.escape(rec['url'], quote=True)}\">Instagram manbasi</a> · <code>{html.escape(rec.get('shortcode', ''))}</code>")
    return _cut_on_line_boundary("\n".join(parts), 1024)


def _render_broadcast(rec: dict, author: str) -> str:
    """The full result card, prefixed with who contributed it.

    Deliberately the same card the sender gets: a member should be able to
    judge the content from the notification itself, without opening or
    searching for anything. Over-long cards are cut on a line boundary so a
    half-written HTML tag can never reach Telegram.
    """
    who = html.escape(author) if author else "Jamoa a'zosi"
    text = f"🆕 <b>{who}</b> yangi bilim qo'shdi:\n\n" + _render_result(rec)
    hint = html.escape((rec.get("title_en") or rec.get("shortcode") or "")[:40])
    return _cut_on_line_boundary(text, BROADCAST_MAX_CHARS, hint=hint)


async def _broadcast(bot: Bot, rec: dict, exclude_chat_id: int, author: str) -> None:
    """Tell every other member that something new landed.

    Pull (/search) is not awareness: it needs the member to already suspect
    there is something worth looking for. The notice therefore carries the
    whole card, not a teaser. It carries NO inline keyboard: those buttons
    delete or reprocess the shared entry, and only the member who submitted
    it should be holding them. Failures here are logged and skipped: a
    blocked bot or a deleted chat must never affect the saved entry, which
    is already committed by the time this runs.
    """
    if not config.BROADCAST_ENABLED or not rec.get("usable"):
        return
    text = _render_broadcast(rec, author)
    sent = failed = 0
    for uid in users.recipients():
        if uid == exclude_chat_id:
            continue
        try:
            try:
                await bot.send_message(uid, text, disable_web_page_preview=True)
            except TelegramRetryAfter as flood:
                # Telegram says how long to wait. Without honouring it, every
                # remaining member in the loop fails too and silently gets
                # nothing - the larger the community, the worse it gets.
                log.info("broadcast: flood limit, %ss kutilmoqda", flood.retry_after)
                await asyncio.sleep(flood.retry_after)
                await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
            await asyncio.sleep(BROADCAST_GAP_SEC)
        except Exception as e:
            failed += 1
            log.warning("broadcast to %s failed: %s", uid, e)
    log.info("broadcast: %s -> %d yuborildi, %d yetmadi", rec.get("shortcode", ""),
             sent, failed)


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
            job["_defer_measure"] = True
            record = await loop.run_in_executor(None, lambda: pipeline.process(job, progress, keep_media=True))
            # Marked done the moment the work is done. Everything below is
            # delivery — a caption Telegram refuses, or a video over the upload
            # limit, must not roll a finished job back into the queue and re-run
            # a 15-minute download+whisper+Ollama pass over already-saved output.
            queue_db.finish(job["id"], {"kb_rows": record.get("kb_rows", 0),
                                        "md_path": record.get("md_path", "")})
        except Exception as e:
            log.exception("job %s failed", job["id"])
            status = queue_db.fail(job["id"], f"{type(e).__name__}: {e}")
            note = "qayta urinaman" if status == "queued" else "to'xtatildi"
            # Once retries are exhausted there is otherwise no way to try again
            # from Telegram — but only offer 🔄 if there is an archived transcript
            # for it to reprocess from (a download-stage failure has none yet).
            markup = None
            if status == "failed" and archive.path_for(job["shortcode"]).exists():
                markup = _keyboard(job["shortcode"])
            try:
                await bot.edit_message_text(
                    chat_id=job["chat_id"], message_id=job["message_id"],
                    text=f"❌ <code>{html.escape(job['shortcode'])}</code> — {note}\n"
                         f"<i>{html.escape(str(e)[:300])}</i>",
                    reply_markup=markup,
                )
            except Exception:
                log.warning("could not report failure for %s", job["id"])
            continue

        t_tg_start = time.perf_counter()
        try:
            media_sent = False
            try:
                media_paths = [Path(p) for p in record.pop("media_paths", []) if Path(p).exists()]
                if media_paths:
                    try:
                        caption = _render_caption(record)
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
                                    item_caption = caption if offset == 0 else None
                                    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                                        await bot.send_photo(job["chat_id"], FSInputFile(path), caption=item_caption)
                                    else:
                                        await bot.send_video(job["chat_id"], FSInputFile(path),
                                                             caption=item_caption, supports_streaming=True)
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
                        media_sent = True
                    finally:
                        shutil.rmtree(media_paths[0].parent, ignore_errors=True)
            except Exception:
                log.exception("job %s: media delivery failed (entry is saved)", job["id"])

            if media_sent:
                try:
                    await bot.delete_message(chat_id=job["chat_id"], message_id=job["message_id"])
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=job["chat_id"], message_id=job["message_id"],
                            text=f"✅ <code>{html.escape(job['shortcode'])}</code> tayyor.",
                        )
                    except Exception:
                        log.warning("job %s: could not clear progress message", job["id"])

                try:
                    await bot.send_message(
                        chat_id=job["chat_id"],
                        text=_render_result(record),
                        reply_markup=_keyboard(job["shortcode"]),
                    )
                except Exception:
                    log.exception("job %s: could not send result message", job["id"])
                    try:
                        await bot.send_message(
                            job["chat_id"],
                            f"✅ <code>{html.escape(job['shortcode'])}</code> saqlandi, "
                            f"lekin natijani ko'rsatib bo'lmadi. "
                            f"Arxiv: <code>{html.escape(record.get('md_path', ''))}</code>")
                    except Exception:
                        log.warning("job %s: could not notify at all", job["id"])
            else:
                try:
                    await bot.edit_message_text(
                        chat_id=job["chat_id"], message_id=job["message_id"],
                        text=_render_result(record), reply_markup=_keyboard(job["shortcode"]),
                    )
                except Exception:
                    log.exception("job %s: could not edit result message", job["id"])
                    try:
                        await bot.send_message(
                            job["chat_id"],
                            f"✅ <code>{html.escape(job['shortcode'])}</code> saqlandi, "
                            f"lekin natijani ko'rsatib bo'lmadi. "
                            f"Arxiv: <code>{html.escape(record.get('md_path', ''))}</code>")
                    except Exception:
                        log.warning("job %s: could not notify at all", job["id"])
        finally:
            tracker = record.pop("_tracker", None)
            if tracker:
                tg_elapsed = time.perf_counter() - t_tg_start
                tracker.bosqichlar["arxiv_va_telegram"] = round(tracker.bosqichlar["arxiv_va_telegram"] + tg_elapsed, 3)
                tracker.write_to_file()

        # Fire and forget: awaiting this held the single worker for as long as
        # the fan-out took, so nobody's next video started until everyone had
        # been notified about the previous one.
        task = asyncio.create_task(
            _broadcast(bot, record, job["chat_id"],
                       job.get("author_name") or record.get("author_names", "")))
        task.add_done_callback(
            lambda t, jid=job["id"]: t.exception() is not None
            and log.error("job %s: broadcast failed (entry is saved): %s", jid, t.exception()))
