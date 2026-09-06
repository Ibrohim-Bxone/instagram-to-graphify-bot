import pytest
import os
import sys
from pathlib import Path
import chromadb

class MockChunking:
    @staticmethod
    def chunk_by_tokens(text, parent_id=""):
        return [{"text": text, "chunk_index": 0, "chunk_total": 1, "token_count": 10}]

def test_kb_matching_upsert(tmp_path, monkeypatch):
    import src.config
    monkeypatch.setattr(src.config, "GRAPHIFY_DIR", None) # force standalone
    monkeypatch.setattr(src.config, "STANDALONE_KB_DIR", tmp_path / "kb_db")
    
    import src.kb as kb
    kb._col = None
    monkeypatch.setattr(kb, "_get_chunking_module", lambda: MockChunking)
    
    # 1. Insert 3 items
    rec1 = {
        "shortcode": "sc123",
        "url": "http://test",
        "date": "2026-09-05",
        "title_en": "Test",
        "usable": True,
        "items": [
            {"kind": "prompt", "subtype": "test", "name_en": "I1", "content": "c1"},
            {"kind": "prompt", "subtype": "test", "name_en": "I2", "content": "c2"},
            {"kind": "prompt", "subtype": "test", "name_en": "I3", "content": "c3"}
        ]
    }
    kb.upsert_record(rec1)
    
    col = kb._collection()
    res = col.get(where={"shortcode": "sc123"})
    assert len(res["ids"]) > 0
    initial_ids_count = len(res["ids"])
    assert any("item:2" in cid for cid in res["ids"]), "3rd item should exist"
    
    # 2. Update with 2 items (item 3 removed)
    rec1["items"].pop()
    kb.upsert_record(rec1)
    
    res2 = col.get(where={"shortcode": "sc123"})
    assert len(res2["ids"]) < initial_ids_count
    assert not any("item:2" in cid for cid in res2["ids"]), "3rd item should be deleted"
    
    # 3. Call again with same data (idempotency)
    delete_called = False
    original_delete = col.delete
    def mock_delete(*args, **kwargs):
        nonlocal delete_called
        delete_called = True
        return original_delete(*args, **kwargs)
    monkeypatch.setattr(col, "delete", mock_delete)
    
    kb.upsert_record(rec1)
    assert not delete_called, "Should not delete anything on identical consecutive runs"
    
    # 4. Usable = False -> deletes all
    rec1["usable"] = False
    kb.upsert_record(rec1)
    res3 = col.get(where={"shortcode": "sc123"})
    assert len(res3["ids"]) == 0, "All chunks should be deleted when usable=False"
    
def test_kb_upsert_failure_prevents_deletion(tmp_path, monkeypatch):
    import src.config
    monkeypatch.setattr(src.config, "GRAPHIFY_DIR", None)
    monkeypatch.setattr(src.config, "STANDALONE_KB_DIR", tmp_path / "kb_db")
    
    import src.kb as kb
    kb._col = None
    monkeypatch.setattr(kb, "_get_chunking_module", lambda: MockChunking)
    
    # 1. Insert 3 items
    rec1 = {
        "shortcode": "sc456",
        "url": "http://test",
        "title_en": "Test",
        "usable": True,
        "items": [
            {"kind": "prompt", "subtype": "test", "name_en": "I1", "content": "c1"},
            {"kind": "prompt", "subtype": "test", "name_en": "I2", "content": "c2"},
            {"kind": "prompt", "subtype": "test", "name_en": "I3", "content": "c3"}
        ]
    }
    kb.upsert_record(rec1)
    col = kb._collection()
    initial_ids = col.get(where={"shortcode": "sc456"})["ids"]
    assert "ig:sc456:item:2" in initial_ids
    
    # 2. Update with 2 items, but mock upsert to fail
    rec1["items"].pop()
    
    original_upsert = col.upsert
    def failing_upsert(*args, **kwargs):
        raise ValueError("Simulated DB error")
        
    monkeypatch.setattr(col, "upsert", failing_upsert)
    
    with pytest.raises(ValueError, match="Simulated DB error"):
        kb.upsert_record(rec1)
        
    monkeypatch.undo() 
    res2 = col.get(where={"shortcode": "sc456"})
    assert set(res2["ids"]) == set(initial_ids), "No IDs should be deleted if upsert fails"
