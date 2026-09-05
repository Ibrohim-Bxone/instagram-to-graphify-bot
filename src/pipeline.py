"""Stage orchestration for one job. Blocking by design — the bot runs it in a worker."""

import json
import logging
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

from . import archive, config, extract, frames, ingest, kb, llm, queue_db, transcribe

log = logging.getLogger(__name__)


class VideoTooLongError(RuntimeError):
    """Raised when video duration exceeds config.MAX_VIDEO_SEC."""
    pass


class _OlchovTracker:
    """Har bosqichning vaqtini o'lchash va yakunda JSONL formatida saqlash."""

    def __init__(self, shortcode: str, source_type: str):
        self.enabled = getattr(config, "MEASURE", True)
        self.shortcode = shortcode
        self.source_type = source_type
        self.sana = datetime.now().isoformat(timespec="seconds")
        self.bosqichlar = {
            "yuklab_olish": 0.0,
            "audio_ajratish": 0.0,
            "transkripsiya": 0.0,
            "kadr_tahlili": 0.0,
            "ajratish": 0.0,
            "dublikat_va_indekslash": 0.0,
            "arxiv_va_telegram": 0.0,
        }
        self.current_stage = None
        self.stage_start = 0.0
        self.xato = None
        self.video_uzunligi = 0.0
        self.transkript_belgi = 0
        self.llm_backend = "vertex" if getattr(config, "EXTRACT_BACKEND", "") == "vertex" else getattr(config, "MODEL_EXTRACT", "gemma4:12b")
        self.written = False

    def start_stage(self, name: str):
        if not self.enabled:
            return
        self.current_stage = name
        self.stage_start = time.perf_counter()

    def end_stage(self, name: str = None):
        if not self.enabled or not self.stage_start:
            return
        st = name or self.current_stage
        if st in self.bosqichlar:
            elapsed = round(time.perf_counter() - self.stage_start, 3)
            self.bosqichlar[st] = round(self.bosqichlar[st] + elapsed, 3)
        self.stage_start = 0.0
        self.current_stage = None

    def record_error(self, stage: str = None):
        if not self.enabled:
            return
        st = stage or self.current_stage or "nomalum"
        self.end_stage(st)
        if not self.xato:
            self.xato = st

    def update_from_media(self, media: dict):
        if not self.enabled or not isinstance(media, dict):
            return
        if media.get("duration"):
            self.video_uzunligi = media["duration"]

    def update_from_record(self, record: dict):
        if not self.enabled or not isinstance(record, dict):
            return
        if not self.video_uzunligi and record.get("duration"):
            self.video_uzunligi = record["duration"]
        transcript = record.get("transcript") or ""
        self.transkript_belgi = len(transcript)
        flags = record.get("flags") or []
        if "llm: gemma4-fallback" in flags:
            self.llm_backend = "gemma4-fallback"
        elif getattr(config, "EXTRACT_BACKEND", "") == "vertex":
            self.llm_backend = "vertex"
        else:
            self.llm_backend = getattr(config, "MODEL_EXTRACT", "gemma4:12b")

    def to_dict(self) -> dict:
        jami = round(sum(self.bosqichlar.values()), 3)
        st = self.source_type
        if st in ("url", "file"):
            manba_turi = "video"
        elif st in ("text", "article"):
            manba_turi = "matn"
        elif st == "photo":
            manba_turi = "rasm"
        else:
            manba_turi = "video"

        entry = {
            "shortcode": self.shortcode,
            "sana": self.sana,
            **self.bosqichlar,
            "jami": jami,
            "manba_turi": manba_turi,
            "video_uzunligi": self.video_uzunligi,
            "transkript_belgi": self.transkript_belgi,
            "llm_backend": self.llm_backend,
        }
        if self.xato:
            entry["xato"] = self.xato
        return entry

    def write_to_file(self):
        if not self.enabled or self.written:
            return
        self.written = True
        entry = self.to_dict()
        olchov_file = getattr(config, "OLCHOV_PATH", config.DATA_DIR / "olchov.jsonl")
        try:
            olchov_file.parent.mkdir(parents=True, exist_ok=True)
            with open(olchov_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("olchov.jsonl yozishda xato: %s", e)


def _should_look_at_frames(transcript: str) -> bool:
    if config.VISION_MODE == "always":
        return True
    if config.VISION_MODE == "never":
        return False
    return len(transcript.strip()) < config.MIN_TRANSCRIPT_CHARS


def _base_record(sc: str, job: dict, media: dict, author_id: str = "", author_name: str = "") -> dict:
    url = media.get("webpage_url") or (job["source"] if job["source_type"] == "url" else "")
    links = [u for u in ingest.extract_links(media.get("caption", "")) if u != url]
    return {
        "shortcode": sc, "url": url, "links": links,
        "uploader": media.get("uploader", ""),
        "duration": media.get("duration", 0),
        "caption": media.get("caption", ""),
        "transcript": "", "language": "", "language_probability": 0.0,
        "onscreen": "", "date": date.today().isoformat(),
        "title_en": "", "content_type": "other", "summary_uz": [], "tags_en": [],
        "items": [], "apply_suggestions_uz": [], "usable": False,
        "flags": list(media.get("flags") or []),
        "author_id": str(author_id or job.get("author_id", "") or ""),
        "author_name": str(author_name or job.get("author_name", "") or ""),
    }


def _finish(job: dict, record: dict, media: dict, cleanup, dry_run: bool = False, keep_media: bool = False, author_id: str = "", author_name: str = "", tracker: _OlchovTracker = None) -> dict:
    author_id = str(author_id or record.get("author_id", "") or job.get("author_id", "") or "")
    author_name = str(author_name or record.get("author_name", "") or job.get("author_name", "") or "")
    queue_db.set_stage(job["id"], "extracting")
    if tracker:
        tracker.start_stage("ajratish")
    try:
        data = extract.extract(record["transcript"], record["caption"], record["onscreen"], media)
        record.update({k: v for k, v in data.items() if k != "source_text"})
    except Exception:
        if tracker:
            tracker.record_error("ajratish")
        raise
    finally:
        if tracker:
            tracker.end_stage("ajratish")

    queue_db.set_stage(job["id"], "saving")
    if tracker:
        tracker.start_stage("arxiv_va_telegram")
    try:
        md_path = archive.write(record)
        record["md_path"] = str(md_path)
    except Exception:
        if tracker:
            tracker.record_error("arxiv_va_telegram")
        raise
    finally:
        if tracker:
            tracker.end_stage("arxiv_va_telegram")

    if tracker:
        tracker.start_stage("dublikat_va_indekslash")
    try:
        if dry_run:
            record["kb_rows"] = 0
        else:
            if record.get("usable"):
                # Duplicate detection is a nice-to-have; the job behind it is not.
                # A Chroma read can fail when another process (the Graphify MCP
                # server) has written to the same collection — kb's reopen recovers
                # upsert but not query, so this call can still raise after whisper,
                # vision and extraction have already run. Losing minutes of GPU work
                # over an optional flag is the wrong trade: skip the check instead.
                try:
                    matches = kb.search(
                        f"{record.get('title_en','')} {' '.join(record.get('summary_uz', []))}",
                        top_k=1, project=config.PROJECT_LABEL, kind="summary",
                        exclude_shortcode=record["shortcode"],
                    )
                except Exception as e:
                    log.warning("duplicate check skipped for %s: %s", record["shortcode"], e)
                    record["flags"].append("dedup_skipped")
                    matches = []
                if matches and matches[0]["similarity"] >= config.DUPLICATE_SIMILARITY_THRESHOLD:
                    m = matches[0]
                    record["flags"].append(f"possible_duplicate:{m['shortcode']}")
                    record["duplicate_of"] = m

            if record.get("usable"):
                record["kb_rows"] = kb.upsert_record(record, author_id=author_id, author_name=author_name)
            else:
                record["kb_rows"] = 0
    except Exception:
        if tracker:
            tracker.record_error("dublikat_va_indekslash")
        raise
    finally:
        if tracker:
            tracker.end_stage("dublikat_va_indekslash")

    cleanup(keep_media)
    return record


def _process_video(job: dict, progress, dry_run: bool = False, keep_media: bool = False, author_id: str = "", author_name: str = "", tracker: _OlchovTracker = None) -> dict:
    sc = job["shortcode"]
    queue_db.set_stage(job["id"], "downloading")
    progress("downloading")
    if tracker:
        tracker.start_stage("yuklab_olish")
    try:
        if job["source_type"] == "url":
            media = ingest.download(job["source"], sc)
        else:
            media = ingest.adopt_file(Path(job["source"]), sc)
    except Exception:
        if tracker:
            tracker.record_error("yuklab_olish")
        raise
    finally:
        if tracker:
            tracker.end_stage("yuklab_olish")

    def cleanup(preserve_media: bool = False):
        if not (preserve_media and job["source_type"] == "url"):
            shutil.rmtree(config.MEDIA_DIR / sc, ignore_errors=True)
        if job["source_type"] == "file":
            Path(job["source"]).unlink(missing_ok=True)
            (config.MEDIA_DIR / f"{sc}.caption.txt").unlink(missing_ok=True)

    try:
        return _process_video_stages(job, progress, dry_run, keep_media, sc, media, cleanup,
                                     author_id=author_id, author_name=author_name, tracker=tracker)
    except Exception:
        # cleanup() only ran on the success path (inside _finish), so any failure
        # between download and _finish left data/media/<sc>/ behind forever.
        cleanup()
        raise


def _process_video_stages(job: dict, progress, dry_run: bool, keep_media: bool, sc: str,
                          media: dict, cleanup, author_id: str = "", author_name: str = "",
                          tracker: _OlchovTracker = None) -> dict:
    duration = media.get("duration") or 0
    if tracker:
        tracker.update_from_media(media)
    if config.MAX_VIDEO_SEC and duration > config.MAX_VIDEO_SEC:
        cleanup()
        queue_db.fail(job["id"], f"VideoTooLongError: duration {duration}s > {config.MAX_VIDEO_SEC}s", retry=False)
        if tracker:
            tracker.record_error("video_too_long")
        raise VideoTooLongError(f"video duration {duration}s exceeds MAX_VIDEO_SEC ({config.MAX_VIDEO_SEC}s)")

    video_path = media.get("video_path")
    record = _base_record(sc, job, media, author_id=author_id, author_name=author_name)
    if video_path:
        queue_db.set_stage(job["id"], "transcribing")
        progress("transcribing")
        try:
            if tracker:
                tracker.start_stage("audio_ajratish")
            try:
                audio = transcribe.extract_audio(video_path)
            finally:
                if tracker:
                    tracker.end_stage("audio_ajratish")

            if tracker:
                tracker.start_stage("transkripsiya")
            try:
                tr = transcribe.transcribe(audio)
                transcribe.unload()
                record.update(transcript=tr["text"], language=tr["language"],
                              language_probability=tr["language_probability"])
                if not tr["text"]:
                    record["flags"].append("no_speech")
                elif tr["language_probability"] < 0.6:
                    record["flags"].append(f"low_language_confidence:{tr['language']}")
            finally:
                if tracker:
                    tracker.end_stage("transkripsiya")
        except transcribe.NoAudioTrack:
            record["flags"].append("no_audio_track")
        except Exception:
            if tracker:
                tracker.record_error()
            raise

    archive.write(record)

    vision_paths = list(media.get("photo_paths", []))
    need_vision = (video_path and _should_look_at_frames(record["transcript"])) or bool(vision_paths)
    if need_vision:
        if tracker:
            tracker.start_stage("kadr_tahlili")
        try:
            if video_path and _should_look_at_frames(record["transcript"]):
                try:
                    vision_paths.extend(frames.extract_frames(video_path))
                except Exception as e:
                    log.warning("frame extraction failed for %s: %s", sc, e)
                    record["flags"].append("vision_failed")

            if vision_paths:
                queue_db.set_stage(job["id"], "vision")
                progress("vision")
                try:
                    record["onscreen"] = extract.read_frames(vision_paths)
                except Exception as e:
                    if queue_db.is_timeout_error(e):
                        raise
                    log.warning("vision stage failed for %s: %s", sc, e)
                    record["flags"].append("vision_failed")
        except Exception:
            if tracker:
                tracker.record_error("kadr_tahlili")
            raise
        finally:
            if tracker:
                tracker.end_stage("kadr_tahlili")

    # Re-persisted here, not just after transcription: if extraction below fails,
    # a 🔄 retry must not have to re-run whisper/vision to get back to where it
    # already was — vision-frame reading is itself an Ollama call expensive
    # enough that losing its output to an unrelated extract() crash would be
    # its own bug.
    archive.write(record)

    progress("extracting")
    result = _finish(job, record, media, cleanup, dry_run, keep_media, author_id=author_id, author_name=author_name, tracker=tracker)
    if keep_media and job["source_type"] == "url":
        result["media_paths"] = [str(p) for p in media.get("media_paths", [])]
    return result


def _process_photo_or_text(job: dict, progress, dry_run: bool = False, author_id: str = "", author_name: str = "", tracker: _OlchovTracker = None) -> dict:
    """No audio track: a forwarded post (text or photos), or a fetched web page."""
    sc = job["shortcode"]
    queue_db.set_stage(job["id"], "downloading")
    progress("downloading")
    if tracker:
        tracker.start_stage("yuklab_olish")
    try:
        if job["source_type"] == "article":
            media = ingest.fetch_article(job["source"])
        elif job["source_type"] == "text":
            media = ingest.adopt_text(job["source"], sc)
        else:
            photo_dir = Path(job["source"])
            paths = sorted(photo_dir.glob("*.jpg"))
            cap_file = photo_dir / "caption.txt"
            caption = cap_file.read_text(encoding="utf-8") if cap_file.exists() else ""
            media = ingest.adopt_photos(paths, sc, caption)
    except Exception:
        if tracker:
            tracker.record_error("yuklab_olish")
        raise
    finally:
        if tracker:
            tracker.end_stage("yuklab_olish")

    if tracker:
        tracker.update_from_media(media)

    record = _base_record(sc, job, media, author_id=author_id, author_name=author_name)
    archive.write(record)

    if media.get("photo_paths"):
        queue_db.set_stage(job["id"], "vision")
        progress("vision")
        if tracker:
            tracker.start_stage("kadr_tahlili")
        try:
            record["onscreen"] = extract.read_frames(media["photo_paths"])
        except Exception as e:
            if queue_db.is_timeout_error(e):
                if tracker:
                    tracker.record_error("kadr_tahlili")
                raise
            log.warning("vision stage failed for %s: %s", sc, e)
            record["flags"].append("vision_failed")
        finally:
            if tracker:
                tracker.end_stage("kadr_tahlili")
        archive.write(record)  # see the matching comment in _process_video

    def cleanup(_preserve_media: bool = False):
        if job["source_type"] == "photo":
            shutil.rmtree(job["source"], ignore_errors=True)
    # `article` writes nothing to disk, so there is nothing to clean up.

    progress("extracting")
    return _finish(job, record, media, cleanup, dry_run, author_id=author_id, author_name=author_name, tracker=tracker)


def process(job: dict, progress=lambda stage: None, dry_run: bool = False, keep_media: bool = False, author_id: str = "", author_name: str = "") -> dict:
    author_id = author_id or job.get("author_id", "")
    author_name = author_name or job.get("author_name", "")
    tracker = _OlchovTracker(job.get("shortcode", ""), job.get("source_type", "url")) if getattr(config, "MEASURE", True) else None
    try:
        if job["source_type"] in ("url", "file"):
            record = _process_video(job, progress, dry_run, keep_media, author_id=author_id, author_name=author_name, tracker=tracker)
        else:
            record = _process_photo_or_text(job, progress, dry_run, author_id=author_id, author_name=author_name, tracker=tracker)

        if tracker:
            tracker.update_from_record(record)
            if job.get("_defer_measure"):
                record["_tracker"] = tracker
            else:
                tracker.write_to_file()
        return record
    except subprocess.TimeoutExpired as e:
        if tracker:
            tracker.record_error()
            tracker.write_to_file()
        # A corrupt file, not a busy machine: retrying buys another 600s hang.
        queue_db.fail(job["id"], f"{type(e).__name__}: {e}", retry=False)
        raise
    except Exception as e:
        if tracker:
            tracker.record_error()
            tracker.write_to_file()
        if queue_db.is_timeout_error(e):
            row = queue_db.get(job["id"])
            attempts = row["attempts"] if row else job.get("attempts", 1)
            if attempts <= config.TIMEOUT_MAX_RETRIES:
                backoff = config.TIMEOUT_BACKOFF_SEC * (2 ** (attempts - 1))
                if backoff > 0:
                    time.sleep(backoff)
                queue_db.fail(job["id"], f"{type(e).__name__}: {e}", retry=True)
            else:
                queue_db.fail(job["id"], f"{type(e).__name__}: {e}", retry=False)
        raise


def reprocess(shortcode: str, author_id: str = "", author_name: str = "") -> dict:
    """Re-run extraction from the archived transcript — no download, no whisper."""
    record = archive.read(archive.path_for(shortcode))
    if record is None:
        raise FileNotFoundError(f"no archive for {shortcode}")
    author_id = author_id or record.get("author_id", "") or record.get("first_author", "")
    author_name = author_name or record.get("author_name", "") or record.get("author_names", "")
    meta = {"uploader": record.get("uploader", ""), "duration": record.get("duration", 0)}
    data = extract.extract(record.get("transcript", ""), record.get("caption", ""),
                           record.get("onscreen", ""), meta)
    record.update({k: v for k, v in data.items() if k != "source_text"})
    md_path = archive.write(record)
    record["md_path"] = str(md_path)
    try:
        col = kb._collection()
        res = kb._retry(col.get, where={"shortcode": shortcode}, include=["metadatas"])
        if res and res.get("metadatas"):
            for m in res["metadatas"]:
                if m:
                    if not record.get("first_author") and m.get("first_author"):
                        record["first_author"] = m["first_author"]
                    if not record.get("contributors") and m.get("contributors"):
                        record["contributors"] = m["contributors"]
                    if not record.get("author_names") and m.get("author_names"):
                        record["author_names"] = m["author_names"]
                    if record.get("first_author"):
                        break
    except Exception:
        pass

    kb.delete_shortcode(shortcode)
    if record.get("usable"):
        record["kb_rows"] = kb.upsert_record(record, author_id=author_id, author_name=author_name)
    else:
        record["kb_rows"] = 0
    return record


def unload_models() -> None:
    transcribe.unload()
    llm.unload(config.MODEL_EXTRACT)
    if config.MODEL_VISION != config.MODEL_EXTRACT:
        llm.unload(config.MODEL_VISION)
