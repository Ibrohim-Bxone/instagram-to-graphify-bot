"""Generate a silent text-slide video — the exact shape the vision path exists for.

    python -m tests.make_synthetic

Used to smoke-test the pipeline end to end without touching Instagram.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from src.transcribe import ffmpeg_exe  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SLIDE_TEXT = [
    "3 PROMPTS THAT SAVE ME HOURS",
    "",
    "1) Act as a senior code reviewer.",
    "   List only the 3 riskiest lines",
    "   in this diff and why.",
    "",
    "2) Summarize this doc as 5 bullets",
    "   a new engineer could act on.",
]


def main() -> int:
    FIXTURES.mkdir(exist_ok=True)
    png = FIXTURES / "slide.png"
    img = Image.new("RGB", (720, 1280), (18, 18, 22))
    draw = ImageDraw.Draw(img)
    y = 300
    for line in SLIDE_TEXT:
        draw.text((60, y), line, fill=(240, 240, 240))
        y += 46
    img.save(png)

    out = FIXTURES / "synthetic_slides.mp4"
    cmd = [ffmpeg_exe(), "-y", "-loop", "1", "-i", str(png),
           "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000",
           "-t", "6", "-pix_fmt", "yuv420p", "-r", "12", "-shortest", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-800:])
        return 1
    png.unlink()
    print(f"created {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
