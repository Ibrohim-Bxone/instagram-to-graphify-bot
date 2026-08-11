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
    monkeypatch.setattr(kb, "upsert_record", lambda r: 1)
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
