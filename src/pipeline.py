"""Stage orchestration for one job. Blocking by design — the bot runs it in a worker."""

import logging
import shutil
from datetime import date
from pathlib import Path

from . import archive, config, extract, frames, ingest, kb, llm, queue_db, transcribe

log = logging.getLogger(__name__)


def _should_look_at_frames(transcript: str) -> bool:
    if config.VISION_MODE == "always":
        return True
    if config.VISION_MODE == "never":
        return False
    return len(transcript.strip()) < config.MIN_TRANSCRIPT_CHARS


def _base_record(sc: str, job: dict, media: dict) -> dict:
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
        "items": [], "apply_suggestions_uz": [], "usable": False, "flags": [],
    }


def _finish(job: dict, record: dict, media: dict, cleanup, dry_run: bool = False, keep_media: bool = False) -> dict:
    queue_db.set_stage(job["id"], "extracting")
    data = extract.extract(record["transcript"], record["caption"], record["onscreen"], media)
    record.update({k: v for k, v in data.items() if k != "source_text"})

    queue_db.set_stage(job["id"], "saving")
    md_path = archive.write(record)
    record["md_path"] = str(md_path)
    if dry_run:
        record["kb_rows"] = 0
    else:
        if record.get("usable"):
            matches = kb.search(
                f"{record.get('title_en','')} {' '.join(record.get('summary_uz', []))}",
                top_k=1, project=config.PROJECT_LABEL, kind="summary",
                exclude_shortcode=record["shortcode"],
            )
            if matches and matches[0]["similarity"] >= config.DUPLICATE_SIMILARITY_THRESHOLD:
                m = matches[0]
                record["flags"].append(f"possible_duplicate:{m['shortcode']}")
                record["duplicate_of"] = m

        record["kb_rows"] = kb.upsert_record(record) if record.get("usable") else 0

    cleanup(keep_media)
    return record


def _process_video(job: dict, progress, dry_run: bool = False, keep_media: bool = False) -> dict:
    sc = job["shortcode"]
    queue_db.set_stage(job["id"], "downloading")
    progress("downloading")
    if job["source_type"] == "url":
        media = ingest.download(job["source"], sc)
    else:
        media = ingest.adopt_file(Path(job["source"]), sc)

    video_path = media.get("video_path")
    record = _base_record(sc, job, media)
    if video_path:
        queue_db.set_stage(job["id"], "transcribing")
        progress("transcribing")
        try:
            audio = transcribe.extract_audio(video_path)
            tr = transcribe.transcribe(audio)
            transcribe.unload()
            record.update(transcript=tr["text"], language=tr["language"],
                          language_probability=tr["language_probability"])
            if not tr["text"]:
                record["flags"].append("no_speech")
            elif tr["language_probability"] < 0.6:
                record["flags"].append(f"low_language_confidence:{tr['language']}")
        except transcribe.NoAudioTrack:
            record["flags"].append("no_audio_track")

    archive.write(record)

    vision_paths = list(media.get("photo_paths", []))
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
            log.warning("vision stage failed for %s: %s", sc, e)
            record["flags"].append("vision_failed")

    def cleanup(preserve_media: bool = False):
        if not (preserve_media and job["source_type"] == "url"):
            shutil.rmtree(config.MEDIA_DIR / sc, ignore_errors=True)
        if job["source_type"] == "file":
            Path(job["source"]).unlink(missing_ok=True)
            (config.MEDIA_DIR / f"{sc}.caption.txt").unlink(missing_ok=True)

    progress("extracting")
    result = _finish(job, record, media, cleanup, dry_run, keep_media)
    if keep_media and job["source_type"] == "url":
        result["media_paths"] = [str(p) for p in media.get("media_paths", [])]
    return result

def _process_photo_or_text(job: dict, progress, dry_run: bool = False) -> dict:
    """A forwarded Telegram post: text only, or photo(s) + caption. No audio track."""
    sc = job["shortcode"]
    queue_db.set_stage(job["id"], "downloading")
    progress("downloading")
    if job["source_type"] == "text":
        media = ingest.adopt_text(job["source"], sc)
    else:
        photo_dir = Path(job["source"])
        paths = sorted(photo_dir.glob("*.jpg"))
        cap_file = photo_dir / "caption.txt"
        caption = cap_file.read_text(encoding="utf-8") if cap_file.exists() else ""
        media = ingest.adopt_photos(paths, sc, caption)

    record = _base_record(sc, job, media)
    archive.write(record)

    if media.get("photo_paths"):
        queue_db.set_stage(job["id"], "vision")
        progress("vision")
        try:
            record["onscreen"] = extract.read_frames(media["photo_paths"])
        except Exception as e:
            log.warning("vision stage failed for %s: %s", sc, e)
            record["flags"].append("vision_failed")

    def cleanup(_preserve_media: bool = False):
        if job["source_type"] == "photo":
            shutil.rmtree(job["source"], ignore_errors=True)

    progress("extracting")
    return _finish(job, record, media, cleanup, dry_run)


def process(job: dict, progress=lambda stage: None, dry_run: bool = False, keep_media: bool = False) -> dict:
    if job["source_type"] in ("url", "file"):
        return _process_video(job, progress, dry_run, keep_media)
    return _process_photo_or_text(job, progress, dry_run)


def reprocess(shortcode: str) -> dict:
    """Re-run extraction from the archived transcript — no download, no whisper."""
    record = archive.read(archive.path_for(shortcode))
    if record is None:
        raise FileNotFoundError(f"no archive for {shortcode}")
    meta = {"uploader": record.get("uploader", ""), "duration": record.get("duration", 0)}
    data = extract.extract(record.get("transcript", ""), record.get("caption", ""),
                           record.get("onscreen", ""), meta)
    record.update({k: v for k, v in data.items() if k != "source_text"})
    md_path = archive.write(record)
    record["md_path"] = str(md_path)
    kb.delete_shortcode(shortcode)
    record["kb_rows"] = kb.upsert_record(record) if record.get("usable") else 0
    return record


def unload_models() -> None:
    transcribe.unload()
    llm.unload(config.MODEL_EXTRACT)
    if config.MODEL_VISION != config.MODEL_EXTRACT:
        llm.unload(config.MODEL_VISION)
