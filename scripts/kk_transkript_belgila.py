"""Qozoqcha ('kk') deb noto'g'ri aniqlangan transkriptlarni belgilash va indeksdan o'chirish.

A7 topilmasi bo'yicha: Whisper o'zbekchani 'kk' (qozoqcha) deb aniqlab, 33 ta arxiv yozuvida
qozoq kirillidagi buzuq transkript hosil qilgan. Ushbu skript:
1. Arxivdagi (archive.py usulida) language == 'kk' bo'lgan yozuvlarga flags ga 'transkript_buzuq:kk' qo'shadi.
2. Indeksdan 'ig:{sc}:transcript:0' bo'lagini Graphify/yozuv.delete bilan o'chiradi.
Standart --quruq va --yoz bayroqlari bilan boshqariladi.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# UTF-8 terminal chiqishi
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Loyiha ildizini sys.path ga qo'shish
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import archive, config, kb

log = logging.getLogger("kk_transkript_belgila")


def process_kk_records(dry_run: bool = True) -> dict:
    """Arxivdagi 'kk' tilidagi yozuvlarni topadi, bayroq qo'shadi va indeksdan o'chiradi."""
    archive_dir = config.ARCHIVE_DIR
    if not archive_dir.exists():
        print(f"Arxiv papkasi topilmadi: {archive_dir}")
        return {"total_checked": 0, "kk_found": 0, "updated": 0, "deleted_kb": 0}

    md_files = sorted(archive_dir.glob("*.md"))
    total_checked = 0
    kk_found = 0
    updated = 0
    deleted_kb = 0

    col = None
    if not dry_run:
        try:
            col = kb._collection()
        except Exception as e:
            log.warning("Kolleksiya ochishda xatolik: %s", e)

    # Graphify yozuv modulini import qilish
    yozuv_mod = None
    graphify_candidates = []
    if config.GRAPHIFY_DIR and Path(config.GRAPHIFY_DIR).is_dir():
        graphify_candidates.append(Path(config.GRAPHIFY_DIR))
    parent_graphify = ROOT.parent / "Graphify"
    if parent_graphify.is_dir() and parent_graphify not in graphify_candidates:
        graphify_candidates.append(parent_graphify)

    for gdir in graphify_candidates:
        if str(gdir) not in sys.path:
            sys.path.insert(0, str(gdir))
        try:
            import yozuv
            yozuv_mod = yozuv
            break
        except ImportError:
            pass

    for p in md_files:
        total_checked += 1
        record = archive.read(p)
        if not record or not isinstance(record, dict):
            continue

        lang = str(record.get("language") or "").strip().lower()
        if lang != "kk":
            continue

        sc = record.get("shortcode") or p.stem
        kk_found += 1
        flags = record.setdefault("flags", [])
        already_flagged = "transkript_buzuq:kk" in flags

        if not already_flagged:
            flags.append("transkript_buzuq:kk")

        mode_str = "[QURUQ]" if dry_run else "[YOZILDI]"
        print(f"{mode_str} {sc}: language='kk', transkript_buzuq:kk qo'shildi")

        if not dry_run:
            archive.write(record)
            updated += 1

            # Indeksdan ig:{sc}:transcript:0 ni o'chirish
            transcript_id = f"ig:{sc}:transcript:0"
            if col is not None:
                try:
                    if yozuv_mod is not None:
                        yozuv_mod.delete(col, ids=[transcript_id])
                    else:
                        col.delete(ids=[transcript_id])
                    deleted_kb += 1
                    print(f"  -> Indeksdan o'chirildi: {transcript_id}")
                except Exception as e:
                    print(f"  -> Indeksdan o'chirishda xatolik ({transcript_id}): {e}", file=sys.stderr)

    return {
        "total_checked": total_checked,
        "kk_found": kk_found,
        "updated": updated if not dry_run else kk_found,
        "deleted_kb": deleted_kb if not dry_run else kk_found,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Qozoqcha ('kk') deb noto'g'ri aniqlangan transkriptlarni belgilash va indeksdan o'chirish"
    )
    parser.add_argument("--quruq", action="store_true", help="Quruq rejim: o'zgartirishlar kiritilmaydi (standart)")
    parser.add_argument("--yoz", action="store_true", help="Haqiqiy yozish rejimi: arxivga yozadi va indeksdan o'chiradi")

    args = parser.parse_args()
    dry_run = not args.yoz

    mode_name = "QURUQ REJIM (o'zgartirishsiz)" if dry_run else "HAQIQIY YOZISH REJIMI"
    print(f"=== kk_transkript_belgila ({mode_name}) ===")

    stats = process_kk_records(dry_run=dry_run)

    print("\n--- Xulosa ---")
    print(f"Jami ko'rilgan fayllar: {stats['total_checked']}")
    print(f"Topilgan 'kk' yozuvlar: {stats['kk_found']}")
    if dry_run:
        print("Eslatma: Quruq rejimda hech narsa o'zgartirilmadi. Haqiqiy yozish uchun --yoz bayrog'ini ishlating.")
    else:
        print(f"Yangilangan arxiv fayllar: {stats['updated']}")
        print(f"Indeksdan o'chirilgan transkriptlar: {stats['deleted_kb']}")


if __name__ == "__main__":
    main()
