"""Offline test-set runner — the loop you tune prompts in.

Iterating through Telegram one video at a time is unusably slow for prompt work.
This runs the same pipeline over a fixed set of local files or URLs, prints a
per-stage timing table, and can skip the Graphify write entirely.

Usage:
    python -m scripts.batch tests/fixtures            # every video in a folder
    python -m scripts.batch urls.txt --dry-run        # one URL per line
    python -m scripts.batch tests/fixtures --extract-only   # reuse archived transcripts
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import archive, config, extract, ingest, kb, pipeline, queue_db  # noqa: E402

# A batch run must never share the live bot's job queue — mixing test fixture
# runs into it clutters /status and, worse, can collide on shortcode ids.
config.QUEUE_DB = config.DATA_DIR / "batch_jobs.sqlite3"

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _targets(source: Path) -> list:
    if source.is_dir():
        return [("file", p) for p in sorted(source.iterdir()) if p.suffix.lower() in VIDEO_EXTS]
    if source.suffix.lower() == ".txt":
        return [("url", line.strip()) for line in source.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
    return [("file", source)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path, help="video folder, single video, or a .txt of URLs")
    ap.add_argument("--dry-run", action="store_true", help="do not write to ChromaDB")
    ap.add_argument("--extract-only", action="store_true",
                    help="skip download/transcription, re-extract from the archive")
    args = ap.parse_args()

    queue_db.init()
    rows = []

    for kind, target in _targets(args.source):
        if kind == "url":
            shortcode = ingest.shortcode_from_url(str(target)) or "unknown"
            source = ingest.clean_url(str(target))
        else:
            shortcode = f"local-{Path(target).stem}"
            source = str(target)

        t0 = time.time()
        try:
            if args.extract_only:
                record = archive.read(archive.path_for(shortcode))
                if record is None:
                    print(f"skip {shortcode}: no archive yet")
                    continue
                data = extract.extract(record.get("transcript", ""), record.get("caption", ""),
                                       record.get("onscreen", ""),
                                       {"uploader": record.get("uploader", "")})
                record.update({k: v for k, v in data.items() if k != "source_text"})
                archive.write(record)
            else:
                if kind == "file":
                    # pipeline.process deletes a "file" job's source once done — that
                    # is correct for a bot-downloaded temp file, but here `source` is
                    # the reusable test fixture, so hand it a disposable copy instead.
                    scratch = config.MEDIA_DIR / "batch-scratch" / Path(source).name
                    scratch.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, scratch)
                    source = str(scratch)
                job = {"id": f"batch:{shortcode}", "shortcode": shortcode,
                       "source_type": kind, "source": source}
                queue_db.enqueue(job["id"], shortcode, kind, source, 0, 0)
                record = pipeline.process(job, dry_run=args.dry_run)

            if args.extract_only and not args.dry_run and record.get("usable"):
                kb.delete_shortcode(shortcode)
                kb.upsert_record(record)

            rows.append((shortcode, round(time.time() - t0, 1), record.get("content_type"),
                         len(record.get("items", [])),
                         sum(1 for i in record.get("items", []) if i.get("verified")),
                         record.get("usable"), ",".join(record.get("flags", []))))
        except Exception as e:
            rows.append((shortcode, round(time.time() - t0, 1), "ERROR", 0, 0, False,
                         f"{type(e).__name__}: {e}"[:80]))

    print(f"\n{'shortcode':<24} {'sec':>7} {'type':<10} {'items':>5} {'ok':>3} {'use':>4}  flags")
    print("-" * 92)
    for r in rows:
        print(f"{r[0]:<24} {r[1]:>7} {str(r[2]):<10} {r[3]:>5} {r[4]:>3} {str(r[5]):>4}  {r[6]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
