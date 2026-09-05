"""Audio extraction and transcription (faster-whisper, local)."""

import gc
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

from . import config

log = logging.getLogger(__name__)

_model = None
_model_key = None


def _register_cuda_dlls() -> None:
    """CTranslate2 needs cuBLAS/cuDNN next to it. The pip nvidia-* wheels ship the
    DLLs but do not put them anywhere Windows looks. os.add_dll_directory alone is
    NOT enough here: it covers modules loaded through Python's import machinery, but
    ctranslate2's compiled core delay-loads cublas64_12.dll on the first GPU matmul,
    resolved through the plain Windows DLL search order — which only sees PATH."""
    if os.name != "nt":
        return
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    dirs = [str(d) for d in base.glob("*/bin")]
    for dll_dir in dirs:
        try:
            os.add_dll_directory(dll_dir)
        except OSError:
            pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")


_register_cuda_dlls()


class NoAudioTrack(RuntimeError):
    """The container has no audio stream at all (common for silent/Twitter-sourced
    clips) — this is a normal case, not a pipeline failure."""


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def extract_audio(video_path: Path) -> Path | None:
    audio = video_path.with_suffix(".wav")
    cmd = [ffmpeg_exe(), "-y", "-i", str(video_path),
           "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(audio)]
    # No timeout here used to hang the single worker forever on a corrupt file.
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=config.FFMPEG_TIMEOUT_SEC)
    if proc.returncode != 0 or not audio.exists():
        # Video-only muxers do not all use the same ffmpeg error wording.
        has_audio_stream = re.search(r"Stream #\d+:\d+.*\bAudio:", proc.stderr, re.IGNORECASE | re.DOTALL)
        if (not has_audio_stream or "does not contain any stream" in proc.stderr
                or "Stream map '0:a" in proc.stderr):
            raise NoAudioTrack("video has no audio stream")
        raise RuntimeError(f"ffmpeg audio extraction failed: {proc.stderr[-500:]}")
    return audio


def _device() -> tuple:
    if config.WHISPER_DEVICE != "auto":
        dev = config.WHISPER_DEVICE
    else:
        try:
            import ctranslate2
            dev = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            dev = "cpu"
    compute = config.WHISPER_COMPUTE_TYPE or ("float16" if dev == "cuda" else "int8")
    return dev, compute


def load_model():
    global _model, _model_key
    from faster_whisper import WhisperModel

    dev, compute = _device()
    key = (config.WHISPER_MODEL, dev, compute)
    if _model is None or _model_key != key:
        unload()
        try:
            _model = WhisperModel(config.WHISPER_MODEL, device=dev, compute_type=compute)
        except (RuntimeError, ValueError) as e:
            # A CUDA-capable card with missing/incompatible runtime DLLs is common
            # enough that failing the whole job over it is the wrong trade.
            if dev != "cuda":
                raise
            log.warning("CUDA init failed (%s); falling back to CPU int8", e)
            dev, compute, key = "cpu", "int8", (config.WHISPER_MODEL, "cpu", "int8")
            _model = WhisperModel(config.WHISPER_MODEL, device=dev, compute_type=compute)
        _model_key = key
    return _model


def unload() -> None:
    """Free VRAM before Ollama loads a model — both do not fit in 8GB together."""
    global _model, _model_key
    if _model is not None:
        del _model
        _model = None
        _model_key = None
        gc.collect()


def transcribe(audio_path: Path) -> dict:
    model = load_model()
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        # large-v3 invents repeated sentences over music and silence; VAD plus
        # dropping the previous-text conditioning is what actually stops it.
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
        temperature=[0.0, 0.2, 0.4],
    )
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments if s.text.strip()]
    text = " ".join(s["text"] for s in segs).strip()
    # On a silent track whisper still reports a confident language ("cy", "nn", ...).
    # Recording that would poison the archive, so an empty transcript has no language.
    language = info.language if text else ""
    probability = round(float(info.language_probability or 0.0), 3) if text else 0.0
    return {
        "text": text,
        "segments": segs,
        "language": language,
        "language_probability": probability,
        "device": _model_key[1],
    }
