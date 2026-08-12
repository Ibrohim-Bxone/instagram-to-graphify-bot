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


def main() -> None:
    _log("watchdog started")
    while True:
        _log("starting bot")
        proc = subprocess.Popen([str(PYTHON), "-m", "src.bot"], cwd=str(ROOT))
        code = proc.wait()
        _log(f"bot exited (code {code}); restarting in {RESTART_DELAY_SEC}s")
        time.sleep(RESTART_DELAY_SEC)


if __name__ == "__main__":
    sys.exit(main())
