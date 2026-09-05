import json
import logging
from pathlib import Path
import sys
import tempfile
from PIL import Image

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.WARNING)

from src import config, extract, pipeline


def run_tests():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        config.DATA_DIR = tmp_path

        # Test kadr rasmini yaratish (64x64 piksel JPEG)
        test_frame = tmp_path / "frame_00.jpg"
        img = Image.new("RGB", (64, 64), color="blue")
        img.save(test_frame, format="JPEG")
        frame_paths = [str(test_frame)]

        # 1. Fallback sinovi: VISION_BACKEND=vertex va noto'g'ri vertex model bilan
        print("1. Fallback sinovi: noto'g'ri vertex model bilan read_frames()...")
        config.VISION_BACKEND = "vertex"
        config.VERTEX_MODEL = "gemini-invalid-model"

        flags = []
        onscreen = extract.read_frames(frame_paths, flags=flags)
        print("   Flags (direct):", flags)
        assert "vision: ollama-fallback" in flags, "Fallback bayrog'i flags ro'yxatida topilmadi!"
        assert isinstance(onscreen, str) and len(onscreen) > 0, "read_frames bo'sh bo'lmagan matn qaytarishi kerak!"
        print("   -> read_frames() fallback muvaffaqiyatli o'tdi.")

        # 2. extract() integratsiyasi: fallback bayrog'i extract() flags da saqlanishi
        print("2. extract() integratsiyasi sinovi...")
        meta = {"uploader": "test_user", "duration": 10}
        data = extract.extract("Test video transcript", "", onscreen, meta)
        extract_flags = data.get("flags", [])
        print("   Flags (extract):", extract_flags)
        assert "vision: ollama-fallback" in extract_flags, "extract() flags da vision fallback bayrog'i topilmadi!"
        print("   -> extract() flags ga vision fallback muvaffaqiyatli qo'shildi.")

        # 3. Tracker sinovi: olchov trackerda vision_backend maydoni
        print("3. Tracker sinovi: vision_backend maydoni...")
        tracker = pipeline._OlchovTracker("test_sc", "video")
        assert hasattr(tracker, "vision_backend"), "Tracker'da vision_backend maydoni yo'q!"
        record = {"shortcode": "test_sc", "flags": ["vision: ollama-fallback"], "transcript": "test"}
        tracker.update_from_record(record)
        assert tracker.vision_backend == "ollama-fallback", (
            f"Kutilgan vision_backend 'ollama-fallback', lekin '{tracker.vision_backend}'"
        )
        d = tracker.to_dict()
        assert d.get("vision_backend") == "ollama-fallback", "Tracker to_dict da vision_backend yo'q yoki noto'g'ri!"
        print("   -> Tracker vision_backend muvaffaqiyatli qayd etildi.")

        # 4. Regressiya sinovi: VISION_BACKEND=ollama bilan ishlash
        print("4. Regressiya sinovi: VISION_BACKEND=ollama to'g'ridan-to'g'ri ishlashi...")
        config.VISION_BACKEND = "ollama"
        flags_ollama = []
        onscreen_ollama = extract.read_frames(frame_paths, flags=flags_ollama)
        print("   Flags (ollama mode):", flags_ollama)
        assert "vision: ollama-fallback" not in flags_ollama, "ollama rejimida fallback bayrog'i bo'lmasligi kerak!"
        assert isinstance(onscreen_ollama, str) and len(onscreen_ollama) > 0

        tracker_ollama = pipeline._OlchovTracker("test_sc2", "video")
        tracker_ollama.update_from_record({"shortcode": "test_sc2", "flags": [], "transcript": "test"})
        assert tracker_ollama.vision_backend == config.MODEL_VISION
        assert tracker_ollama.to_dict().get("vision_backend") == config.MODEL_VISION
        print("   -> VISION_BACKEND=ollama regressiyasiz to'g'ri ishladi.")

    print("\nBarcha vision fallback sinovlari muvaffaqiyatli yakunlandi!")


if __name__ == "__main__":
    try:
        run_tests()
    except Exception as e:
        print(f"Sinovda xatolik: {e}")
        sys.exit(1)
