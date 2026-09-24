import sys
import os
import pytest
from pathlib import Path

# Add Graphify directory to path for imports to work during tests
GRAPHIFY_DIR = Path(__file__).parent.parent.parent / "Graphify"
sys.path.insert(0, str(GRAPHIFY_DIR))

@pytest.fixture(autouse=True)
def isolate_graphify_dbs(tmp_path, monkeypatch):
    """
    Ensure tests never write to live Graphify DBs.
    Patches keyword_index.DB_PATH and yozuv lock paths to tmp_path.
    """
    import keyword_index
    import yozuv
    
    # 1. Patch keyword_index
    fake_fts_db = tmp_path / "keyword_index.sqlite3"
    monkeypatch.setattr(keyword_index, "DB_PATH", fake_fts_db)
    
    # Singleton ni tozalab, testlar uchun yangidan ochamiz
    keyword_index._index_singleton = None
    
    # 2. Patch yozuv DB_DIR
    fake_kg_db = tmp_path / "kg_db"
    fake_kg_db.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(yozuv, "DB_DIR", fake_kg_db)
    monkeypatch.setattr(yozuv, "LOCK_FILE", fake_kg_db / ".yozuv.lock")
    monkeypatch.setattr(yozuv, "PENDING_FILE", fake_kg_db / ".pending_fts.json")

    yield
    
    # Teardown
    keyword_index._index_singleton = None
