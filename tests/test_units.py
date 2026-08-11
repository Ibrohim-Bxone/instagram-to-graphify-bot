"""Unit tests for the pure logic: grounding checks, archive round-trip, id shape."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import archive, extract, ingest, kb  # noqa: E402

SOURCE = """### Spoken transcript
Here is the prompt I use every day: "Act as a senior reviewer and list only the
three riskiest lines in this diff." It works better than asking for a full review.
"""


def test_verify_item_accepts_verbatim_quote():
    assert extract.verify_item(
        'Act as a senior reviewer and list only the three riskiest lines in this diff.',
        SOURCE)


def test_verify_item_accepts_minor_whitespace_drift():
    assert extract.verify_item(
        'Act as a senior reviewer   and list only\nthe three riskiest lines in this diff.',
        SOURCE)


def test_verify_item_rejects_model_invention():
    assert not extract.verify_item(
        'You are an expert marketer. Write five taglines for a coffee brand.', SOURCE)


def test_shortcode_parsing():
    assert ingest.shortcode_from_url("https://www.instagram.com/reel/ABC123x_-/") == "ABC123x_-"
    assert ingest.shortcode_from_url("https://instagram.com/p/XYZ/?igsh=1") == "XYZ"
    assert ingest.shortcode_from_url("https://www.instagram.com/user/reel/QQ1/") == "QQ1"
    assert ingest.shortcode_from_url("https://example.com/reel/ABC/") is None


def test_clean_url_drops_tracking_params():
    assert ingest.clean_url("https://www.instagram.com/reel/AB/?igsh=xyz") == \
        "https://www.instagram.com/reel/AB"


def test_archive_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(archive.config, "ARCHIVE_DIR", tmp_path)
    record = {
        "shortcode": "AB1", "url": "https://www.instagram.com/reel/AB1",
        "title_en": "Prompt chaining basics", "content_type": "prompt",
        "summary_uz": ["Ikki bosqichli prompt yozish usuli"],
        "tags_en": ["prompt-chaining", "llm"],
        "items": [{"kind": "prompt", "name_en": "Reviewer prompt",
                   "content": "Act as a senior reviewer.", "note_uz": "Kod ko'rish uchun",
                   "verified": True}],
        "apply_suggestions_uz": ["Graphify skilliga qo'shish mumkin"],
        "usable": True, "flags": [], "transcript": "...", "caption": "", "onscreen": "",
        "date": "2026-08-11",
    }
    path = archive.write(record)
    assert path.name == "AB1.md"
    back = archive.read(path)
    assert back == record


def test_kb_ids_are_deterministic():
    record = {"shortcode": "AB1", "title_en": "T", "content_type": "prompt", "date": "2026-08-11",
              "summary_uz": ["x"], "tags_en": ["t"], "apply_suggestions_uz": [],
              "items": [{"kind": "prompt", "name_en": "P", "content": "c",
                         "note_uz": "n", "verified": True}]}
    ids = [r[0] for r in kb.build_documents(record)]
    assert ids == ["ig:AB1:summary", "ig:AB1:item:0"]
    assert kb.build_documents(record)[0][2]["project"] == kb.config.PROJECT_LABEL
