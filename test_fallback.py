import json
import logging
from pathlib import Path
import sys
import tempfile

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.WARNING)

from src import config, extract


def run_tests():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Nuqson 4: Haqiqiy data/tools_cache.json ga yozmaslik uchun vaqtinchalik papkaga yo'naltiramiz
        tmp_path = Path(tmpdir)
        config.DATA_DIR = tmp_path
        config.VERIFY_NAMES = True
        config.EXTRACT_BACKEND = "vertex"
        config.VERTEX_MODEL = "gemini-invalid-model"

        # Nuqson 1 & 2 sinovi: Vertex ataylab yiqitilganda fallback va unverified
        print("1. Fallback sinovi: noto'g'ri vertex model bilan extract()...")
        transcript = "This is a test transcript mentioning a tool like ChatGPT and Perplexity."
        meta = {"uploader": "test", "duration": 10}

        data = extract.extract(transcript, "", "", meta)
        flags = data.get("flags", [])
        print("   Flags:", flags)

        assert "llm: gemma4-fallback" in flags, "Fallback bayrog'i topilmadi!"
        assert not any(f.startswith("unchecked:") for f in flags), "unchecked bayrog'i bo'lmasligi kerak!"

        tmp_cache = tmp_path / "tools_cache.json"
        if tmp_cache.exists():
            with open(tmp_cache, "r", encoding="utf-8") as f:
                cache = json.load(f)
            assert not any(v.get("status") == "error" for v in cache.values()), (
                "Vaqtinchalik keshga status: error yozilmasligi kerak!"
            )
        print("   -> Fallback muvaffaqiyatli o'tdi, kesh zaharlanmadi.")

        # Nuqson 1 sinovi: Keshda eski formatdagi {"status": "error"} yozuv turganda kod yiqilmasligi
        print("2. Eski formatdagi kesh yozuvi sinovi...")
        legacy_cache = {
            "ChatGPT": {"status": "error"},
            "Perplexity": {"status": "error"}
        }
        with open(tmp_cache, "w", encoding="utf-8") as f:
            json.dump(legacy_cache, f, ensure_ascii=False, indent=2)

        data_legacy = extract.extract(transcript, "", "", meta)
        flags_legacy = data_legacy.get("flags", [])
        print("   Flags (legacy cache):", flags_legacy)
        assert "llm: gemma4-fallback" in flags_legacy
        assert not any(f.startswith("unchecked:") for f in flags_legacy)
        print("   -> Eski formatdagi kesh bilan ham kod yiqilmadi.")

        # Nuqson 1 sinovi (davomi): Eski formatdagi kesh yozuvi Vertex ishlab turganda
        # eskirgan deb hisoblanib, qayta tekshiruvga kirishi va keshni yangilashi
        print("3. Eski formatdagi kesh + ishlayotgan Vertex (qayta tekshirish) sinovi...")
        from unittest.mock import patch
        legacy_cache_reverify = {"ChatGPT": {"status": "error"}}
        with open(tmp_cache, "w", encoding="utf-8") as f:
            json.dump(legacy_cache_reverify, f, ensure_ascii=False, indent=2)

        mock_payload = {
            "items": [{"kind": "tool", "name_en": "ChatGPT", "content": "AI bot", "note_uz": "AI bot"}]
        }
        with patch("src.vertex.chat_json", return_value=mock_payload), \
             patch("src.vertex.verify_tool", return_value={"found": True, "url": "https://chatgpt.com"}) as mock_verify:
            config.VERTEX_MODEL = "gemini-3.7-flash"
            data_reverify = extract.extract(transcript, "", "", meta)
            assert mock_verify.called, "Eski formatdagi kesh yozuvi qayta tekshirilmadi!"
            assert not any(f.startswith("unchecked:") for f in data_reverify.get("flags", []))
            with open(tmp_cache, "r", encoding="utf-8") as f:
                cache_after = json.load(f)
            assert cache_after.get("ChatGPT", {}).get("found") is True
            assert cache_after.get("ChatGPT", {}).get("url") == "https://chatgpt.com"
            print("   -> Eski formatdagi kesh muvaffaqiyatli eskirgan deb hisoblandi va qayta tekshirildi.")

    print("\nBarcha fallback sinovlari muvaffaqiyatli yakunlandi!")


if __name__ == "__main__":
    try:
        run_tests()
    except Exception as e:
        print(f"Sinovda xatolik: {e}")
        sys.exit(1)
