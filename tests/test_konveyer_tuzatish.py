"""Konveyer tuzatishlari uchun unit testlar (tarmoqsiz, monkeypatch).

Qamrov:
- A7: Whisper o'zbekchani qozoqcha aniqlashi va WHISPER_TURKIC_AS_UZ bo'yicha qayta yo'naltirish
- B11: Vertex maxOutputTokens, finishReason=='MAX_TOKENS', 429 VertexQuotaError, usageMetadata va tracker
- B17: Vertex 429 da defer_count siyosati va MODEL_EXTRACT_FALLBACK zaxirasi
- B10: link_verify element tokenlaridan title ni ajratish va bir nechta elementda title mosligini taqiqlash
- kk_transkript_belgila.py: buzuq transkriptlarni belgilash va indeksdan o'chirish
"""

import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import archive, config, extract, kb, link_verify, pipeline, queue_db, transcribe, vertex
from scripts import kk_transkript_belgila


# ===========================================================================
# 1. A7: transcribe.py testlari
# ===========================================================================

def test_transcribe_turkic_language_forced_to_uz(monkeypatch, tmp_path):
    """Whisper turkiy til ('kk') aniqlasa, language='uz' bilan qayta transkripsiya qilinadi."""
    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"dummy wav data")

    calls = []

    class MockSegment:
        def __init__(self, text, start=0.0, end=1.0):
            self.text = text
            self.start = start
            self.end = end

    class MockInfo:
        def __init__(self, lang, prob=0.95):
            self.language = lang
            self.language_probability = prob

    class MockModel:
        def transcribe(self, path, **kwargs):
            calls.append(kwargs)
            if "language" not in kwargs:
                # Birinchi chaqiruv: Whisper 'kk' deb aniqlaydi
                return [MockSegment("Salom dunyo")], MockInfo("kk", 0.92)
            else:
                # Ikkinchi chaqiruv: language='uz' berilganda
                return [MockSegment("Salom dunyo o'zbek tilida")], MockInfo("uz", 0.95)

    monkeypatch.setattr(transcribe, "load_model", lambda: MockModel())
    monkeypatch.setattr(transcribe, "_model_key", ("large-v3", "cpu", "int8"))

    res = transcribe.transcribe(audio_file)

    assert len(calls) == 2, "Turkiy til aniqlanganda transcribe ikkinchi marta language='uz' bilan chaqirilishi kerak"
    assert calls[1].get("language") == "uz"
    assert res["language"] == "uz", "Yakuniy ishlatilgan til 'uz' bo'lishi kerak"
    assert res["language_detected"] == "kk", "Asl aniqlangan til 'kk' bo'lishi kerak"
    assert res["language_probability"] == 0.92
    assert "Salom dunyo" in res["text"]


def test_transcribe_non_turkic_keeps_original_language(monkeypatch, tmp_path):
    """Turkiy bo'lmagan til ('ru') aniqlansa, o'zgarishsiz qoladi va ikkinchi marta chaqirilmaydi."""
    audio_file = tmp_path / "test_ru.wav"
    audio_file.write_bytes(b"dummy")

    calls = []

    class MockSegment:
        text = "Привет мир"
        start = 0.0
        end = 1.0

    class MockInfo:
        language = "ru"
        language_probability = 0.98

    class MockModel:
        def transcribe(self, path, **kwargs):
            calls.append(kwargs)
            return [MockSegment()], MockInfo()

    monkeypatch.setattr(transcribe, "load_model", lambda: MockModel())
    monkeypatch.setattr(transcribe, "_model_key", ("large-v3", "cpu", "int8"))

    res = transcribe.transcribe(audio_file)

    assert len(calls) == 1, "Turkiy bo'lmagan tilda ikkinchi chaqiruv bo'lmasligi kerak"
    assert res["language"] == "ru"
    assert res["language_detected"] == "ru"
    assert res["text"] == "Привет мир"


def test_transcribe_silent_track_preserves_empty_language(monkeypatch, tmp_path):
    """Jim trekda matn bo'sh bo'lgach, language va language_detected bo'sh qolishi shart."""
    audio_file = tmp_path / "silent.wav"
    audio_file.write_bytes(b"dummy")

    class MockInfo:
        language = "kk"
        language_probability = 0.85

    class MockModel:
        def transcribe(self, path, **kwargs):
            return [], MockInfo()  # bo'sh segmentlar (jim trek)

    monkeypatch.setattr(transcribe, "load_model", lambda: MockModel())
    monkeypatch.setattr(transcribe, "_model_key", ("large-v3", "cpu", "int8"))

    res = transcribe.transcribe(audio_file)

    assert res["text"] == ""
    assert res["language"] == ""
    assert res["language_detected"] == ""
    assert res["language_probability"] == 0.0


# ===========================================================================
# 2. B11: vertex.py testlari
# ===========================================================================

def test_vertex_generation_config_includes_max_output_tokens(monkeypatch):
    """generationConfig ichida maxOutputTokens config.VERTEX_MAX_OUTPUT_TOKENS ga teng bo'lishi kerak."""
    captured_payload = {}

    def mock_urlopen(req, timeout=None):
        nonlocal captured_payload
        captured_payload = json.loads(req.data.decode("utf-8"))
        mock_resp = MagicMock()
        payload_resp = {
            "candidates": [{
                "content": {"parts": [{"text": json.dumps({"title_en": "Test", "items": []})}]},
                "finishReason": "STOP"
            }],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50, "totalTokenCount": 150}
        }
        mock_resp.read.return_value = json.dumps(payload_resp).encode("utf-8")
        return mock_resp

    monkeypatch.setattr(vertex, "_get_token", lambda: "mock_tok")
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    monkeypatch.setattr(config, "VERTEX_MAX_OUTPUT_TOKENS", 4096)

    res = vertex.chat_json([{"role": "user", "content": "hi"}], {})

    assert captured_payload["generationConfig"]["maxOutputTokens"] == 4096
    assert res["title_en"] == "Test"
    assert "usageMetadata" in res
    assert res["prompt_tokens"] == 100
    assert res["candidates_tokens"] == 50


def test_vertex_finish_reason_max_tokens_raises_error(monkeypatch):
    """finishReason == 'MAX_TOKENS' bo'lsa kesilgan javob qabul qilinmasdan VertexError otilishi kerak."""
    def mock_urlopen(req, timeout=None):
        mock_resp = MagicMock()
        payload_resp = {
            "candidates": [{
                "content": {"parts": [{"text": "{\"partial\": "}]},
                "finishReason": "MAX_TOKENS"
            }],
            "usageMetadata": {}
        }
        mock_resp.read.return_value = json.dumps(payload_resp).encode("utf-8")
        return mock_resp

    monkeypatch.setattr(vertex, "_get_token", lambda: "mock_tok")
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    with pytest.raises(vertex.VertexError) as exc_info:
        vertex.chat_json([{"role": "user", "content": "hi"}], {})
    assert "MAX_TOKENS" in str(exc_info.value)

    # chat_vision da ham xuddi shunday
    with pytest.raises(vertex.VertexError) as exc_info_vision:
        vertex.chat_vision([{"role": "user", "content": "hi"}], [])
    assert "MAX_TOKENS" in str(exc_info_vision.value)


def test_vertex_429_raises_vertex_quota_error(monkeypatch):
    """HTTP 429 xatosi VertexQuotaError otishi va status=429 bo'lishi kerak (IK-06)."""
    def mock_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("https://aiplatform.googleapis.com", 429, "Resource Exhausted", {}, MagicMock(read=lambda: b"Quota exceeded"))

    monkeypatch.setattr(vertex, "_get_token", lambda: "mock_tok")
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    with pytest.raises(vertex.VertexQuotaError) as exc_info:
        vertex.chat_json([{"role": "user", "content": "hi"}], {})

    assert exc_info.value.status == 429
    assert isinstance(exc_info.value, vertex.VertexError)


def test_vertex_other_http_error_has_status(monkeypatch):
    """Boshqa HTTPError holatlarida VertexError.status o'rnatilishi kerak."""
    def mock_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("https://aiplatform.googleapis.com", 503, "Unavailable", {}, MagicMock(read=lambda: b"Service Unavailable"))

    monkeypatch.setattr(vertex, "_get_token", lambda: "mock_tok")
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    with pytest.raises(vertex.VertexError) as exc_info:
        vertex.chat_json([{"role": "user", "content": "hi"}], {})

    assert exc_info.value.status == 503


def test_olchov_tracker_records_usage_metadata(tmp_path, monkeypatch):
    """IK-05: _OlchovTracker usageMetadata tokenlarini qabul qilishi va olchov.jsonl ga yozishi kerak."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    olchov_file = tmp_path / "olchov.jsonl"

    tracker = pipeline._OlchovTracker("sc_test", "url")
    record = {
        "shortcode": "sc_test",
        "duration": 30,
        "transcript": "Test video",
        "flags": [],
        "usageMetadata": {
            "promptTokenCount": 512,
            "candidatesTokenCount": 128,
            "thoughtsTokenCount": 64,
            "totalTokenCount": 704
        }
    }
    tracker.update_from_record(record)
    tracker.write_to_file()

    assert olchov_file.exists()
    lines = olchov_file.read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[0])

    assert entry["prompt_tokens"] == 512
    assert entry["candidates_tokens"] == 128
    assert entry["thoughts_tokens"] == 64
    assert entry["total_tokens"] == 704
    assert "usageMetadata" in entry


# ===========================================================================
# 3. B17: extract.py va pipeline.py testlari
# ===========================================================================

def test_extract_vertex_quota_429_defers_job(monkeypatch):
    """VertexQuotaError bo'lganda job['defer_count'] < QUOTA_MAX_DEFERS bo'lsa JobDeferred ko'tariladi."""
    monkeypatch.setattr(config, "EXTRACT_BACKEND", "vertex")
    monkeypatch.setattr(config, "QUOTA_MAX_DEFERS", 3)
    monkeypatch.setattr(config, "QUOTA_DEFER_SEC", 600)

    def mock_chat_json(messages, schema):
        raise vertex.VertexQuotaError("Quota exceeded (429)", status=429)

    monkeypatch.setattr(vertex, "chat_json", mock_chat_json)

    job = {"id": "job_1", "defer_count": 0}
    with pytest.raises(queue_db.JobDeferred) as exc_info:
        extract.extract("transcript", "caption", "", {}, job=job)

    assert exc_info.value.retry_after_sec == 600


def test_extract_vertex_quota_429_exhausted_falls_back_to_model(monkeypatch):
    """defer_count >= QUOTA_MAX_DEFERS bo'lganda MODEL_EXTRACT_FALLBACK ga o'tadi."""
    monkeypatch.setattr(config, "EXTRACT_BACKEND", "vertex")
    monkeypatch.setattr(config, "QUOTA_MAX_DEFERS", 3)
    monkeypatch.setattr(config, "MODEL_EXTRACT_FALLBACK", "qwen3.6")

    def mock_vertex_chat(messages, schema):
        raise vertex.VertexQuotaError("Quota exceeded (429)", status=429)

    fallback_called = False
    def mock_llm_chat(model, messages, schema, **kw):
        nonlocal fallback_called
        fallback_called = True
        assert model == "qwen3.6"
        return {"title_en": "Fallback title", "items": [], "usable": True}

    monkeypatch.setattr(vertex, "chat_json", mock_vertex_chat)
    monkeypatch.setattr("src.llm.chat_json", mock_llm_chat)

    job = {"id": "job_1", "defer_count": 3}
    res = extract.extract("transcript", "caption", "", {}, job=job)

    assert fallback_called is True
    assert res["title_en"] == "Fallback title"
    assert "llm: qwen3.6-fallback" in res["flags"]


def test_extract_vertex_quota_429_without_job_falls_back_to_model(monkeypatch):
    """job kiritilmaganda (masalan, reprocess) JobDeferred otilmasdan to'g'ridan-to'g'ri MODEL_EXTRACT_FALLBACK ga o'tishi kerak."""
    monkeypatch.setattr(config, "EXTRACT_BACKEND", "vertex")
    monkeypatch.setattr(config, "QUOTA_MAX_DEFERS", 3)
    monkeypatch.setattr(config, "MODEL_EXTRACT_FALLBACK", "qwen3.6")

    def mock_vertex_chat(messages, schema):
        raise vertex.VertexQuotaError("Quota exceeded (429)", status=429)

    fallback_called = False
    def mock_llm_chat(model, messages, schema, **kw):
        nonlocal fallback_called
        fallback_called = True
        assert model == "qwen3.6"
        return {"title_en": "Reprocess fallback", "items": [], "usable": True}

    monkeypatch.setattr(vertex, "chat_json", mock_vertex_chat)
    monkeypatch.setattr("src.llm.chat_json", mock_llm_chat)

    # job berilmagan (job=None)
    res = extract.extract("transcript", "caption", "", meta={})

    assert fallback_called is True
    assert res["title_en"] == "Reprocess fallback"
    assert "llm: qwen3.6-fallback" in res["flags"]


def test_extract_vertex_general_error_falls_back_to_model(monkeypatch):
    """Vertex boshqa xatoga uchrasa (masalan 500 yoki Network error), darhol MODEL_EXTRACT_FALLBACK ga o'tadi."""
    monkeypatch.setattr(config, "EXTRACT_BACKEND", "vertex")
    monkeypatch.setattr(config, "MODEL_EXTRACT_FALLBACK", "qwen3.6")

    def mock_vertex_chat(messages, schema):
        raise vertex.VertexError("500 Internal Server Error", status=500)

    fallback_called = False
    def mock_llm_chat(model, messages, schema, **kw):
        nonlocal fallback_called
        fallback_called = True
        assert model == "qwen3.6"
        return {"title_en": "Recovered", "items": [], "usable": True}

    monkeypatch.setattr(vertex, "chat_json", mock_vertex_chat)
    monkeypatch.setattr("src.llm.chat_json", mock_llm_chat)

    job = {"id": "job_1", "defer_count": 0}
    res = extract.extract("transcript", "caption", "", {}, job=job)

    assert fallback_called is True
    assert res["title_en"] == "Recovered"
    assert "llm: qwen3.6-fallback" in res["flags"]


# ===========================================================================
# 4. kk_transkript_belgila.py testlari
# ===========================================================================

def test_kk_transkript_belgila_dry_run_and_write(tmp_path, monkeypatch):
    """kk_transkript_belgila skripti language=='kk' yozuvlarni topishi va o'chirishi kerak."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    monkeypatch.setattr(config, "ARCHIVE_DIR", archive_dir)

    # 1. 'kk' yozuvi
    rec_kk = {
        "shortcode": "kk_reel",
        "title_en": "Kazakh transcript reel",
        "language": "kk",
        "transcript": "Инстаграмда язған...",
        "flags": ["initial_flag"],
        "usable": True,
    }
    archive.write(rec_kk)

    # 2. 'uz' yozuvi (o'zgarmasligi kerak)
    rec_uz = {
        "shortcode": "uz_reel",
        "title_en": "Uzbek transcript reel",
        "language": "uz",
        "transcript": "O'zbekcha toza matn",
        "flags": [],
        "usable": True,
    }
    archive.write(rec_uz)

    deleted_ids = []
    class MockCol:
        def delete(self, ids=None, **kw):
            deleted_ids.extend(ids or [])

    monkeypatch.setattr(kb, "_collection", lambda: MockCol())
    monkeypatch.setattr(config, "GRAPHIFY_DIR", None)

    # Quruq rejim
    stats_dry = kk_transkript_belgila.process_kk_records(dry_run=True)
    assert stats_dry["total_checked"] == 2
    assert stats_dry["kk_found"] == 1
    assert len(deleted_ids) == 0, "Quruq rejimda indeksdan hech narsa o'chirilmasligi kerak"

    p_kk = archive.path_for("kk_reel")
    assert "transkript_buzuq:kk" not in archive.read(p_kk)["flags"]

    # Haqiqiy yozish rejimi
    stats_write = kk_transkript_belgila.process_kk_records(dry_run=False)
    assert stats_write["kk_found"] == 1
    assert stats_write["updated"] == 1
    assert stats_write["deleted_kb"] == 1
    assert "ig:kk_reel:transcript:0" in deleted_ids

    # Fayldagi bayroqni tekshirish
    rec_after = archive.read(p_kk)
    assert "transkript_buzuq:kk" in rec_after["flags"]
    assert "initial_flag" in rec_after["flags"]

    # uz_reel tegilmaganini tekshirish
    p_uz = archive.path_for("uz_reel")
    assert archive.read(p_uz)["flags"] == []


def test_kk_transkript_belgila_uses_graphify_yozuv_delete(tmp_path, monkeypatch):
    """Graphify/yozuv moduli mavjud bo'lganda yozuv.delete chaqirilishini tekshirish."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    monkeypatch.setattr(config, "ARCHIVE_DIR", archive_dir)

    rec_kk = {
        "shortcode": "sc_kk_test",
        "title_en": "Reel",
        "language": "kk",
        "transcript": "Инстаграм...",
        "flags": [],
        "usable": True,
    }
    archive.write(rec_kk)

    mock_col = MagicMock()
    monkeypatch.setattr(kb, "_collection", lambda: mock_col)

    deleted_calls = []
    class MockYozuvMod:
        @staticmethod
        def delete(col, ids=None, where=None):
            deleted_calls.append((col, ids))

    # Graphify yozuv importini mock qilish
    with patch.dict("sys.modules", {"yozuv": MockYozuvMod}):
        stats = kk_transkript_belgila.process_kk_records(dry_run=False)

    assert stats["kk_found"] == 1
    assert stats["deleted_kb"] == 1
    assert len(deleted_calls) == 1
    assert deleted_calls[0][1] == ["ig:sc_kk_test:transcript:0"]

