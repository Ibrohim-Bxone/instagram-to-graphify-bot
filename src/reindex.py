"""Rebuild the ChromaDB entries from the markdown archive.

Usage:
    python -m src.reindex [--purge]

Run this after changing the embedding model, after restoring a backup, or any
time the vector store and the archive have drifted apart.
"""

import argparse
import sys

from . import archive, kb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--purge", action="store_true",
                    help="delete each shortcode's existing rows before writing")
    args = ap.parse_args()

    files = rows = skipped = 0
    for path, record in archive.all_records():
        files += 1
        if not record.get("usable"):
            skipped += 1
            continue
        if args.purge:
            kb.delete_shortcode(record["shortcode"])
        rows += kb.upsert_record(record)
        print(f"indexed {record['shortcode']} ({path.name})")

    print(f"\n{files} arxiv fayl, {rows} yozuv indekslandi, {skipped} tashlab ketildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
