"""Smoke test for the text-only Telegram post path (no video, no whisper)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import pipeline, queue_db  # noqa: E402


def test_text_post_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.config, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(pipeline.archive.config, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(pipeline.kb, "upsert_record", lambda rec: len(rec.get("items", [])) + 1)

    text = ("Claude Code endi subagentlarni parallel ishga tushira oladi. "
            "Batafsil: https://claude.com/blog/subagents")
    job = {"id": "test:post1", "shortcode": "tg-1-1", "source_type": "text",
           "source": text, "chat_id": 1, "message_id": 1}

    record = pipeline.process(job)

    assert record["links"] == ["https://claude.com/blog/subagents"]
    assert record["caption"] == text
    assert (tmp_path / "tg-1-1.md").exists()
