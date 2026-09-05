"""Key frame selection.

Many "useful" reels are silent text slides, where the audio track carries
nothing — the frames are the whole content. Frames are picked by scene change
rather than a fixed count, then de-duplicated perceptually so a static talking
head does not spend four near-identical images.
"""

import subprocess
from pathlib import Path

from PIL import Image

from . import config
from .transcribe import ffmpeg_exe


def _dhash(path: Path, size: int = 8) -> int:
    img = Image.open(path).convert("L").resize((size + 1, size))
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | int(img.getpixel((x, y)) < img.getpixel((x + 1, y)))
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _run(cmd: list) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=config.FFMPEG_TIMEOUT_SEC).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def extract_frames(video_path: Path, max_frames: int | None = None) -> list:
    max_frames = max_frames or config.MAX_FRAMES
    out_dir = video_path.parent / "frames"
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()

    _run([ffmpeg_exe(), "-y", "-i", str(video_path),
          "-vf", "select='gt(scene,0.3)',scale=720:-1", "-vsync", "vfr",
          "-frames:v", str(max_frames * 3), "-q:v", "4",
          str(out_dir / "scene_%03d.jpg")])

    frames = sorted(out_dir.glob("scene_*.jpg"))
    if len(frames) < 2:
        # No detectable cuts (single continuous shot) — fall back to even sampling.
        _run([ffmpeg_exe(), "-y", "-i", str(video_path),
              "-vf", "fps=1/3,scale=720:-1", "-frames:v", str(max_frames), "-q:v", "4",
              str(out_dir / "even_%03d.jpg")])
        frames = sorted(out_dir.glob("*.jpg"))

    kept, hashes = [], []
    for f in frames:
        try:
            h = _dhash(f)
        except Exception:
            continue
        if any(_hamming(h, prev) < 8 for prev in hashes):
            continue
        hashes.append(h)
        kept.append(f)
        if len(kept) >= max_frames:
            break
    return kept
