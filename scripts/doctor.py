"""Pre-flight check: every external dependency the pipeline needs, in one run.

    python -m scripts.doctor
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "
_results = []


def check(label: str, fn) -> None:
    try:
        status, detail = fn()
    except Exception as e:
        status, detail = BAD, f"{type(e).__name__}: {e}"
    _results.append((status, label, str(detail)[:120]))


def _ffmpeg():
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = subprocess.run([exe, "-version"], capture_output=True, text=True)
    return (OK, out.stdout.splitlines()[0]) if out.returncode == 0 else (BAD, out.stderr[:100])


def _whisper_device():
    import ctranslate2
    n = ctranslate2.get_cuda_device_count()
    if n > 0:
        return OK, f"CUDA device count: {n} (float16)"
    return WARN, "no CUDA visible — whisper will run on CPU int8 (slower but fine)"


def _ollama():
    import requests
    r = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=10)
    r.raise_for_status()
    names = [m["name"] for m in r.json().get("models", [])]
    missing = [m for m in {config.MODEL_EXTRACT, config.MODEL_VISION} if m not in names]
    if missing:
        return BAD, f"missing model(s): {', '.join(missing)} — run: ollama pull {missing[0]}"
    return OK, f"{config.MODEL_EXTRACT} / {config.MODEL_VISION} available"


def _vision_capable():
    import requests
    r = requests.post(f"{config.OLLAMA_HOST}/api/show",
                      json={"model": config.MODEL_VISION}, timeout=30)
    r.raise_for_status()
    caps = r.json().get("capabilities", [])
    if "vision" in caps:
        return OK, f"capabilities: {', '.join(caps)}"
    return WARN, f"{config.MODEL_VISION} has no vision capability — set VISION_MODE=never"


def _chroma():
    from src import kb
    col = kb._collection()
    mode = "Graphify-linked" if kb._using_graphify() else "standalone"
    db_path = (config.GRAPHIFY_DIR / "kg_db") if kb._using_graphify() else config.STANDALONE_KB_DIR
    return OK, f"{mode}: {db_path} — collection '{kb.COLLECTION}' holds {col.count()} chunks"


def _archive():
    p = config.ARCHIVE_DIR / ".write-test"
    p.write_text("ok", encoding="utf-8")
    p.unlink()
    return OK, str(config.ARCHIVE_DIR)


def _telegram():
    if not config.BOT_TOKEN:
        return BAD, "BOT_TOKEN is empty (.env)"
    if not config.ALLOWED_USER_IDS:
        return BAD, "ALLOWED_USER_IDS is empty — the bot would answer nobody"
    return OK, f"{len(config.ALLOWED_USER_IDS)} allowed user(s)"


def main() -> int:
    check("ffmpeg (imageio-ffmpeg)", _ffmpeg)
    check("whisper device", _whisper_device)
    check("ollama reachable + models", _ollama)
    check("vision capability", _vision_capable)
    check("knowledge-base backend", _chroma)
    check("archive dir writable", _archive)
    check("telegram config", _telegram)

    print()
    for status, label, detail in _results:
        print(f"[{status}] {label:<28} {detail}")
    failed = sum(1 for s, _, _ in _results if s == BAD)
    print(f"\n{len(_results) - failed}/{len(_results)} ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
