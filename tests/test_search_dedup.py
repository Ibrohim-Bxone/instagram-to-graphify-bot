"""Tests for search and deduplication functionality."""

import sys
from pathlib import Path

import pytest
import chromadb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kb, pipeline, config


@pytest.fixture
def mock_chroma(monkeypatch):
    client = chromadb.EphemeralClient()
    col = client.get_or_create_collection("test_col", metadata={"hnsw:space": "cosine"})
    monkeypatch.setattr(kb, "_collection", lambda: col)
    return col


def test_kb_search_filters(mock_chroma):
    mock_chroma.add(
        ids=["ig:1:summary", "ig:2:summary", "ig:3:summary", "ig:1:item"],
        documents=["Cat video", "Dog video", "Cat video 2", "Cat prompt"],
        metadatas=[
            {"shortcode": "1", "project": config.PROJECT_LABEL, "kind": "summary", "title": "C1"},
            {"shortcode": "2", "project": config.PROJECT_LABEL, "kind": "summary", "title": "D1"},
            {"shortcode": "3", "project": "other", "kind": "summary", "title": "C2"},
            {"shortcode": "1", "project": config.PROJECT_LABEL, "kind": "prompt", "title": "P1"},
        ]
    )
    
    # 1. Test project filter (default project)
    res = kb.search("cat", project=config.PROJECT_LABEL)
    assert any(r["shortcode"] == "1" for r in res)
    assert not any(r["shortcode"] == "3" for r in res)  # other project

    # 2. Test kind filter
    res = kb.search("cat", kind="prompt")
    assert len(res) == 1
    assert res[0]["kind"] == "prompt"

    # 3. Test exclude_shortcode
    res = kb.search("video", exclude_shortcode="1", project="all")
    assert not any(r["shortcode"] == "1" for r in res)
    assert any(r["shortcode"] == "2" for r in res)
    assert any(r["shortcode"] == "3" for r in res)


def test_pipeline_deduplication(mock_chroma, monkeypatch):
    # Add an existing record to Chroma
    mock_chroma.add(
        ids=["ig:dup1:summary"],
        documents=["Similar video title similar summary uzbek"],
        metadatas=[
            {"shortcode": "dup1", "project": config.PROJECT_LABEL, "kind": "summary", "title": "Original"}
        ]
    )
    
    monkeypatch.setattr(pipeline.archive, "write", lambda r: Path(f"/mock/{r['shortcode']}.md"))
    monkeypatch.setattr(kb, "upsert_record", lambda r, author_id="", author_name="": 1)
    monkeypatch.setattr(pipeline.extract, "extract", lambda t, c, o, m: {"usable": True})
    monkeypatch.setattr(pipeline.queue_db, "set_stage", lambda i, s: None)
    
    job = {"id": 1, "shortcode": "dup2", "source_type": "url", "source": "abc"}
    media = {}
    record = {
        "shortcode": "dup2",
        "title_en": "Similar video title",
        "summary_uz": ["similar summary uzbek"],
        "usable": True,
        "flags": [],
        "transcript": "",
        "caption": "",
        "onscreen": ""
    }
    
    def cleanup(keep=False): pass
    
    # Set threshold low enough to ensure it triggers
    monkeypatch.setattr(config, "DUPLICATE_SIMILARITY_THRESHOLD", 0.0)
    
    result = pipeline._finish(job, record, media, cleanup, dry_run=False)

    assert any("possible_duplicate:dup1" in f for f in result["flags"])
    assert "duplicate_of" in result
    assert result["duplicate_of"]["shortcode"] == "dup1"


def test_kb_backend_standalone_when_graphify_dir_unset(monkeypatch):
    monkeypatch.setattr(config, "GRAPHIFY_DIR", None)
    assert kb._using_graphify() is False


def test_kb_backend_standalone_when_graphify_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GRAPHIFY_DIR", tmp_path / "does-not-exist")
    assert kb._using_graphify() is False


def test_kb_backend_graphify_linked_when_dir_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GRAPHIFY_DIR", tmp_path)
    assert kb._using_graphify() is True


def test_upsert_record_author_collision_preserves_first_author(mock_chroma):
    record1 = {
        "shortcode": "coll1",
        "title_en": "First Submit",
        "content_type": "prompt",
        "summary_uz": ["Xulosa 1"],
        "tags_en": ["tag1"],
        "items": [{"kind": "prompt", "name_en": "P1", "content": "c1", "note_uz": "n1", "verified": True}],
        "apply_suggestions_uz": [],
        "usable": True,
    }
    # First upsert with author_id="user_alpha", author_name="Alpha"
    kb.upsert_record(record1, author_id="user_alpha", author_name="Alpha")

    res1 = mock_chroma.get(where={"shortcode": "coll1"}, include=["metadatas"])
    assert len(res1["metadatas"]) == 2
    for m in res1["metadatas"]:
        assert m["first_author"] == "user_alpha"
        assert m["contributors"] == "user_alpha"
        assert m["author_names"] == "Alpha"

    # Second upsert with same shortcode but different author_id="user_beta", author_name="Beta"
    record2 = dict(record1)
    record2["title_en"] = "Second Submit"
    kb.upsert_record(record2, author_id="user_beta", author_name="Beta")

    res2 = mock_chroma.get(where={"shortcode": "coll1"}, include=["metadatas"])
    assert len(res2["metadatas"]) == 2
    for m in res2["metadatas"]:
        assert m["first_author"] == "user_alpha"
        assert m["contributors"] == "user_alpha, user_beta"
        assert m["author_names"] == "Alpha, Beta"

    # Third upsert with user_alpha again - should not duplicate in contributors
    kb.upsert_record(record2, author_id="user_alpha", author_name="Alpha")
    res3 = mock_chroma.get(where={"shortcode": "coll1"}, include=["metadatas"])
    for m in res3["metadatas"]:
        assert m["first_author"] == "user_alpha"
        assert m["contributors"] == "user_alpha, user_beta"
        assert m["author_names"] == "Alpha, Beta"
