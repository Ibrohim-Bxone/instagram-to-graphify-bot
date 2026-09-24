"""Soqchi: botda osilib qolgan ishlarni aniqlab, navbatni ochib yuboruvchi mustaqil kuzatuvchi.
"""

import argparse
import os
import signal
import sqlite3
import subprocess
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


def get_process_cmdline(pid: int) -> str | None:
    """Tizimdan berilgan PID ning buyruq qatorini oladi."""
    try:
        if sys.platform == "win32":
            cmd = [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
            return None
        else:
            p = Path(f"/proc/{pid}/cmdline")
            if p.exists():
                return p.read_text().replace("\x00", " ")
    except Exception:
        pass
    return None


def terminate_process(pid: int) -> bool:
    """Jarayonni to'xtatadi."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        try:
            if sys.platform == "win32":
                res = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                return res.returncode == 0
        except Exception:
            pass
        return False


def check_worker_heartbeat(project_dir: Path, chegara_min: float, quruq: bool = False) -> None:
    """Worker heartbeat faylini tekshiradi va eski bo'lsa bot jarayonini to'xtatadi."""
    heartbeat_file = project_dir / "data" / "worker.heartbeat"
    lock_file = project_dir / "data" / "bot.lock"
    chegara_sec = chegara_min * 60.0
    now = time.time()

    if not heartbeat_file.exists():
        return

    try:
        hb_str = heartbeat_file.read_text(encoding="utf-8").strip()
        hb_time = float(hb_str)
    except Exception:
        hb_time = heartbeat_file.stat().st_mtime

    delta_sec = now - hb_time
    if delta_sec <= chegara_sec:
        return

    delta_min = int(delta_sec // 60)
    print(f"  ⚠️ Worker heartbeat {delta_min} daqiqa eski (chegara: {chegara_min} daq).")

    if not lock_file.exists():
        print("  ℹ️ bot.lock topilmadi, to'xtatiladigan PID yo'q.")
        return

    try:
        pid = int(lock_file.read_text(encoding="utf-8").strip())
    except Exception as e:
        print(f"  ⚠️ bot.lock faylidan PID o'qib bo'lmadi: {e}", file=sys.stderr)
        return

    cmdline = get_process_cmdline(pid)
    if not cmdline:
        print(f"  ℹ️ PID {pid} tirik emas yoki buyruq qatorini olib bo'lmadi.")
        return

    # Tekshiruvsiz PID o'ldirish YO'Q!
    if "src.bot" not in cmdline:
        print(
            f"  ⚠️ XAVFSIZLIK: PID {pid} buyruq qatorida 'src.bot' topilmadi "
            f"('{cmdline[:80]}'). Jarayon to'xtatilmadi!"
        )
        return

    if quruq:
        print(f"  [QURUQ] Osilgan bot aniqlandi (PID {pid}, cmdline: '{cmdline[:60]}...'). To'xtatiladi.")
    else:
        print(f"  [TUZATILDI] Osilgan bot jarayoni to'xtatilmoqda: PID {pid} ('{cmdline[:60]}...')")
        success = terminate_process(pid)
        if success:
            print(f"  ✅ PID {pid} to'xtatildi. Watchdog qayta ko'taradi.")
        else:
            print(f"  ❌ PID {pid} ni to'xtatib bo'lmadi.", file=sys.stderr)


def inspect_and_clean(db_path: Path, chegara_min: float, quruq: bool = False, project_dir: Path | None = None) -> None:
    """Bazadagi ishlarni tekshiradi va belgilangan muddatdan oshgan running ishlarni failed qiladi."""
    if project_dir is None:
        project_dir = db_path.parent.parent if db_path.parent.name == "data" else DEFAULT_PROJECT_DIR

    # 1. Heartbeat va jarayon tekshiruvi (B8)
    check_worker_heartbeat(project_dir, chegara_min, quruq=quruq)

    # 2. Bazadagi osilgan ishlarni tozalash
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

        # Holat hisoboti: done/queued/running/failed sonlari
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

        # Osilgan ishlarni topish (status='running' va now - updated_at > chegara)
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
                inspect_and_clean(db_path, args.chegara, quruq=args.quruq, project_dir=args.dir)
                time.sleep(args.kutish)
        except KeyboardInterrupt:
            print("\nSoqchi to'xtatildi.")
    else:
        inspect_and_clean(db_path, args.chegara, quruq=args.quruq, project_dir=args.dir)


if __name__ == "__main__":
    main()
