"""Soqchi: botda osilib qolgan ishlarni aniqlab, navbatni ochib yuboruvchi mustaqil kuzatuvchi.

MUHIM — BU SKRIPT NIMA QILA OLMAYDI:
Skript SQLite bazasidagi yozuvni tuzatadi (status='failed'), lekin OSILGAN PYTHON
JARAYONINI TO'XTATA OLMAYDI. Ishchi oqim baribir bloklangan holda qoladi.
Ish 'failed' qilingach, navbat davom etishi uchun botni qayta ishga tushirish
kerak bo'lishi mumkin.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Windows konsolida cp1251 kodlash xatolarini oldini olish
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Nomlangan konstantalar
DEFAULT_PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_REL_PATH = Path("data") / "jobs.sqlite3"
DEFAULT_CHEGARA_MIN = 30.0
DB_TIMEOUT_SEC = 5.0
BUSY_TIMEOUT_MS = 5000


def inspect_and_clean(db_path: Path, chegara_min: float, quruq: bool = False) -> None:
    """Bazadagi ishlarni tekshiradi va belgilangan muddatdan oshgan running ishlarni failed qiladi."""
    now = time.time()
    chegara_sec = chegara_min * 60.0

    if not db_path.exists():
        print(f"⚠️ Baza topilmadi: {db_path}", file=sys.stderr)
        return

    con = None
    try:
        con = sqlite3.connect(str(db_path), timeout=DB_TIMEOUT_SEC)
        con.row_factory = sqlite3.Row
        try:
            con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        except sqlite3.OperationalError:
            pass

        # 1. Holat hisoboti: done/queued/running/failed sonlari
        cur = con.cursor()
        counts = {"done": 0, "queued": 0, "running": 0, "failed": 0}
        for row in cur.execute("SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"):
            counts[row["status"]] = row["c"]

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[{now_str}] Holat: done={counts.get('done', 0)}, "
            f"queued={counts.get('queued', 0)}, "
            f"running={counts.get('running', 0)}, "
            f"failed={counts.get('failed', 0)}"
        )

        # 2. Osilgan ishlarni topish (status='running' va now - updated_at > chegara)
        cur.execute(
            "SELECT id, shortcode, stage, updated_at FROM jobs "
            "WHERE status = 'running' AND (? - updated_at) > ?",
            (now, chegara_sec),
        )
        stuck_jobs = cur.fetchall()

        if not stuck_jobs:
            return

        modified_count = 0
        for job in stuck_jobs:
            delta_sec = now - job["updated_at"]
            delta_min = int(delta_sec // 60)
            stage = job["stage"] or "noma'lum"
            reason = f"soqchi: {stage} bosqichida {delta_min} daqiqa qotib qoldi"

            if quruq:
                print(
                    f"  [QURUQ] Osilgan ish aniqlandi: id={job['id']}, "
                    f"shortcode={job['shortcode']}, stage={stage}, "
                    f"qotgan_vaqt={delta_min} daqiqa. "
                    f"(Harakat: status='failed', error='{reason}')"
                )
            else:
                update_cur = con.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (reason, now, job["id"]),
                )
                if update_cur.rowcount > 0:
                    modified_count += 1
                    print(
                        f"  [TUZATILDI] {job['id']} ({job['shortcode']}): "
                        f"status='failed' qilindi ({reason})"
                    )

        if not quruq:
            con.commit()
            if modified_count > 0:
                print(
                    "  ⚠️ DIQQAT: Ish 'failed' qilindi, lekin navbat davom etishi uchun "
                    "botni qayta ishga tushirish kerak bo'lishi mumkin."
                )

    except sqlite3.OperationalError as e:
        print(f"⚠️ Baza band ({e}), keyingi siklga o'tilmoqda...", file=sys.stderr)
    finally:
        if con is not None:
            con.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Botda osilib qolgan ishlarni aniqlab, navbatni ochib yuboruvchi mustaqil kuzatuvchi skript."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_PROJECT_DIR,
        help=f"Loyiha papkasi (default: {DEFAULT_PROJECT_DIR})",
    )
    parser.add_argument(
        "--chegara",
        type=float,
        default=DEFAULT_CHEGARA_MIN,
        help=f"Osilgan deb hisoblash chegarasi (daqiqa) (default: {DEFAULT_CHEGARA_MIN})",
    )
    parser.add_argument(
        "--kutish",
        type=float,
        default=None,
        help="Cheksiz siklda ishlash oralig'i (soniya). Berilmasa bir marta tekshiradi.",
    )
    parser.add_argument(
        "--quruq",
        action="store_true",
        help="Quruq rejim (dry-run): faqat ko'rsatish, bazani o'zgartirmaslik.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = args.dir / DEFAULT_DB_REL_PATH

    if args.kutish is not None:
        print(
            f"Soqchi ishga tushdi: baza={db_path}, chegara={args.chegara} daq, "
            f"oraliq={args.kutish} soniya. To'xtatish: Ctrl+C"
        )
        try:
            while True:
                inspect_and_clean(db_path, args.chegara, quruq=args.quruq)
                time.sleep(args.kutish)
        except KeyboardInterrupt:
            print("\nSoqchi to'xtatildi.")
    else:
        inspect_and_clean(db_path, args.chegara, quruq=args.quruq)


if __name__ == "__main__":
    main()
