"""Supervisor: launches the bot and restarts it if it ever exits (crash,
unhandled exception, or the machine having been asleep). Meant to run once
from the Windows Startup folder — see README's "Auto-start on boot" section
for how it's registered.

    pythonw.exe -m scripts.watchdog
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "pythonw.exe"
LOG = ROOT / "data" / "logs" / "watchdog.log"
RESTART_DELAY_SEC = 10


def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    import msvcrt
    import os
    
    lock_file = ROOT / "data" / "watchdog.lock"
    try:
        lock_fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o666)
        msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        _log(f"watchdog allaqachon ishlayapti (qulf: {lock_file}), chiqilmoqda.")
        return 0
        
    try:
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode("utf-8"))
        
        _log("watchdog started")
        fast_crashes = 0
        
        while True:
            _log("starting bot")
            start_time = time.time()
            proc = subprocess.Popen([str(PYTHON), "-m", "src.bot"], cwd=str(ROOT))
            code = proc.wait()
            
            if code == 3:
                _log("bot exit code 3 bilan chiqdi, watchdog to'xtatildi.")
                return 3
                
            uptime = time.time() - start_time
            if uptime < 60:
                fast_crashes += 1
                if fast_crashes >= 5:
                    _log("bot ketma-ket 5 marta 60 soniyadan tez yiqildi, watchdog to'xtatildi.")
                    return 1
            else:
                fast_crashes = 0
                
            _log(f"bot exited (code {code}); restarting in {RESTART_DELAY_SEC}s")
            time.sleep(RESTART_DELAY_SEC)
    finally:
        os.close(lock_fd)
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
