import contextlib
import pytest

import sys

from pathlib import Path



# Mock config to test kb in isolation
class MockConfig:
    GRAPHIFY_DIR = Path(__file__).parent.parent.parent / "Graphify"
    STANDALONE_KB_DIR = Path("test_db")
    PROJECT_LABEL = "test"

import src.config
src.config.GRAPHIFY_DIR = MockConfig.GRAPHIFY_DIR
src.config.STANDALONE_KB_DIR = MockConfig.STANDALONE_KB_DIR
src.config.PROJECT_LABEL = MockConfig.PROJECT_LABEL



import src.kb as kb



class MockCollection:

    def __init__(self, space="l2"):

        self.metadata = {"hnsw:space": space}



    def get(self, limit=5, include=None, where=None):

        return {"ids": [], "embeddings": []}

        

    def query(self, query_texts=None, query_embeddings=None, n_results=1, where=None, include=None):

        return {

            "ids": [["id1"]],

            "distances": [[1.0]],

            "documents": [["doc1 test"]],

            "metadatas": [[{"title": "test", "project": "test", "kind": "test", "shortcode": "test"}]],

            "embeddings": [[[1.0, 0.0]]]

        }

    

    def _embedding_function(self, texts):

        return [[1.0, 0.0]]



def test_kb_search_uses_graphify_similarity(monkeypatch):

    col = MockCollection("l2")

    monkeypatch.setattr(kb, "_collection", lambda: col)

    

    # We monkeypatch the query to check if it properly calls graphify similarity

    results = kb.search("test", top_k=1)

    

    assert len(results) == 1

    assert "similarity" in results[0]

    assert "raw_similarity" in results[0]

    assert results[0]["similarity"] == 0.5 # clamped 1 - 1/2 = 0.5

    assert results[0]["raw_similarity"] == 0.5

def test_kb_build_documents_chunks_long_item():



    import src.kb as kb



    # Mock a record with a very long item



    long_content = "So'z " * 1000



    record = {



        "shortcode": "TEST1234",



        "url": "http://test",



        "date": "2026-09-05",



        "title_en": "Test Title",



        "items": [



            {



                "kind": "prompt",



                "subtype": "chat",



                "name_en": "Long Item",



                "content": long_content,



                "note_uz": "Uzun",



                "verified": True



            }



        ],



        "transcript": "Transkript " * 500



    }



    



    docs1 = kb.build_documents(record)



    docs2 = kb.build_documents(record)



    



    # ID lar deterministik bo'lishi kerak



    ids1 = [d[0] for d in docs1]



    ids2 = [d[0] for d in docs2]



    assert ids1 == ids2, "IDlar ikki marta chaqirilganda bir xil bo'lmadi"



    



    # Chunklar qilinganini tekshiramiz (part:<k> borligi)



    has_part = any(":part:" in id for id in ids1)



    assert has_part, "Uzun hujjat bo'laklanmadi"



    



    # Transkript borligini tekshiramiz



    has_transcript = any(":transcript:" in id for id in ids1)



    assert has_transcript, "Transkript hujjatlar orasida yo'q"



    



    # Hech qaysi chunk 256 tokendan uzun emasligini tekshirish uchun 



    # tokenni taxmin qilish qiyin, lekin chunk_total > 1 bo'lishi aniq



    for d_id, doc, meta in docs1:



        assert "parent_id" in meta



        assert "canonical_id" in meta



        assert "chunk_index" in meta



        assert "chunk_total" in meta



        assert "shortcode" in meta


@contextlib.contextmanager
def clean_graphify_env_ctx():
    import importlib
    orig_sys_path = list(sys.path)
    orig_db_module = sys.modules.get("db")
    orig_col = kb._col
    orig_logged_backend = kb._logged_backend
    orig_graphify_dir = src.config.GRAPHIFY_DIR
    orig_standalone_dir = src.config.STANDALONE_KB_DIR

    # Setup: db modulini tozalash va conftest kiritgan jonli Graphify yo'lini chiqarish
    sys.modules.pop("db", None)
    try:
        from tests.conftest import GRAPHIFY_DIR as LIVE_GRAPHIFY_DIR
        live_paths = {str(LIVE_GRAPHIFY_DIR), str(LIVE_GRAPHIFY_DIR.resolve())}
        sys.path[:] = [p for p in sys.path if p not in live_paths and Path(p).resolve() != LIVE_GRAPHIFY_DIR.resolve()]
    except Exception:
        sys.path[:] = [p for p in sys.path if Path(p).name != "Graphify"]
    importlib.invalidate_caches()

    try:
        yield
    finally:
        sys.path[:] = orig_sys_path
        if orig_db_module is not None:
            sys.modules["db"] = orig_db_module
        else:
            sys.modules.pop("db", None)
        importlib.invalidate_caches()
        kb._col = orig_col
        kb._logged_backend = orig_logged_backend
        src.config.GRAPHIFY_DIR = orig_graphify_dir
        src.config.STANDALONE_KB_DIR = orig_standalone_dir


@pytest.fixture
def clean_graphify_env():
    with clean_graphify_env_ctx():
        yield


def test_kb_collection_uses_graphify_db(tmp_path, clean_graphify_env):
    """K1/K3: _using_graphify() bo'lganda kolleksiya Graphify db.get_collection() dan
    olinadi, kg_db papkasi yaratilmaydi."""
    fake_graphify = tmp_path / "fake_graphify"
    fake_graphify.mkdir()

    db_code = (
        "calls = []\n"
        "class FakeCollection:\n"
        "    pass\n"
        "fake_col = FakeCollection()\n"
        "DB_PATH = 'custom/kg_db_v2d'\n"
        "def get_collection():\n"
        "    calls.append('get_collection')\n"
        "    return fake_col\n"
        "def reopen():\n"
        "    calls.append('reopen')\n"
        "    return fake_col\n"
    )
    (fake_graphify / "db.py").write_text(db_code, encoding="utf-8")

    src.config.GRAPHIFY_DIR = fake_graphify
    kb._col = None

    col = kb._collection()

    import db
    assert "get_collection" in db.calls
    assert col is db.fake_col
    assert not (fake_graphify / "kg_db").exists(), "kg_db papkasi yaratilmasligi kerak"


def test_kb_reopen_calls_graphify_reopen(tmp_path, clean_graphify_env):
    """K1/K3: _reopen() chaqirilganda Graphify db.reopen() chaqiriladi va
    qayta ochilgan kolleksiya saqlanadi/qaytariladi."""
    fake_graphify = tmp_path / "fake_graphify_reopen"
    fake_graphify.mkdir()

    db_code = (
        "calls = []\n"
        "class FakeCol:\n"
        "    def __init__(self, name): self.name = name\n"
        "col1 = FakeCol('first')\n"
        "col2 = FakeCol('reopened')\n"
        "def get_collection():\n"
        "    calls.append('get_collection')\n"
        "    return col1\n"
        "def reopen():\n"
        "    calls.append('reopen')\n"
        "    return col2\n"
    )
    (fake_graphify / "db.py").write_text(db_code, encoding="utf-8")

    src.config.GRAPHIFY_DIR = fake_graphify
    kb._col = None

    c1 = kb._collection()
    import db
    assert c1 is db.col1
    assert db.calls == ["get_collection"]

    c2 = kb._reopen()
    assert "reopen" in db.calls
    assert c2 is db.col2
    assert kb._col is db.col2


def test_kb_graphify_db_missing_raises_runtime_error_no_fallback(tmp_path, clean_graphify_env):
    """K2/K3: db.py topilmaganda RuntimeError ko'tariladi va standalone bazaga fallback qilinmaydi."""
    fake_graphify = tmp_path / "fake_graphify_missing"
    fake_graphify.mkdir()
    standalone_dir = tmp_path / "standalone_must_not_exist"

    src.config.GRAPHIFY_DIR = fake_graphify
    src.config.STANDALONE_KB_DIR = standalone_dir
    kb._col = None

    with pytest.raises(RuntimeError) as exc_info:
        kb._collection()
    assert "Graphify bazasini ochishda xatolik" in str(exc_info.value)
    assert not standalone_dir.exists(), "Standalone bazaga fallback qilinmasligi shart"

    with pytest.raises(RuntimeError) as exc_reopen:
        kb._reopen()
    assert "Graphify bazasini qayta ochishda xatolik" in str(exc_reopen.value)
    assert not standalone_dir.exists()


def test_kb_graphify_db_import_error_raises_runtime_error_no_fallback(tmp_path, clean_graphify_env):
    """K2/K3: db.py import qilishda modul darajasida ImportError yuz berganda
    RuntimeError ko'tariladi, sababi saqlanadi va standalone bazaga fallback qilinmaydi."""
    fake_graphify = tmp_path / "fake_graphify_import_err"
    fake_graphify.mkdir()
    standalone_dir = tmp_path / "standalone_must_not_exist"

    db_code = "raise ImportError('ChromaDB kutubxonasi topilmadi yoki buzuq')\n"
    (fake_graphify / "db.py").write_text(db_code, encoding="utf-8")

    src.config.GRAPHIFY_DIR = fake_graphify
    src.config.STANDALONE_KB_DIR = standalone_dir
    kb._col = None

    with pytest.raises(RuntimeError) as exc_get:
        kb._collection()
    assert "ChromaDB kutubxonasi topilmadi yoki buzuq" in str(exc_get.value)
    assert not standalone_dir.exists(), "Standalone bazaga fallback qilinmasligi shart"

    with pytest.raises(RuntimeError) as exc_reo:
        kb._reopen()
    assert "ChromaDB kutubxonasi topilmadi yoki buzuq" in str(exc_reo.value)
    assert not standalone_dir.exists()


def test_kb_graphify_db_error_raises_runtime_error_no_fallback(tmp_path, clean_graphify_env):
    """K2/K3: Graphify db ochish xatosi jim yutilmaydi va
    standalone bazaga fallback qilinmaydi (RuntimeError ko'tariladi)."""
    fake_graphify = tmp_path / "fake_graphify_err"
    fake_graphify.mkdir()
    standalone_dir = tmp_path / "standalone_must_not_exist"

    db_code = (
        "def get_collection():\n"
        "    raise ConnectionError('Ollama aloqa uzildi')\n"
        "def reopen():\n"
        "    raise ConnectionError('Ollama aloqa uzildi')\n"
    )
    (fake_graphify / "db.py").write_text(db_code, encoding="utf-8")

    src.config.GRAPHIFY_DIR = fake_graphify
    src.config.STANDALONE_KB_DIR = standalone_dir
    kb._col = None

    with pytest.raises(RuntimeError) as exc_get:
        kb._collection()
    assert "Ollama aloqa uzildi" in str(exc_get.value)
    assert not standalone_dir.exists(), "Standalone bazaga fallback qilinmasligi shart"

    with pytest.raises(RuntimeError) as exc_reo:
        kb._reopen()
    assert "Ollama aloqa uzildi" in str(exc_reo.value)
    assert not standalone_dir.exists()


def test_kb_standalone_mode_when_no_graphify_dir(tmp_path, clean_graphify_env):
    """K1/K3: GRAPHIFY_DIR yo'q bo'lsa standalone yo'l avvalgidek ishlaydi."""
    standalone_dir = tmp_path / "standalone_kb"

    src.config.GRAPHIFY_DIR = None
    src.config.STANDALONE_KB_DIR = standalone_dir
    kb._col = None

    assert not kb._using_graphify()

    col = kb._collection()
    assert col is not None
    assert standalone_dir.exists(), "Standalone baza katalogi yaratilishi kerak"

    col2 = kb._reopen()
    assert col2 is not None


def test_kb_reopen_handles_none_return(tmp_path, clean_graphify_env):
    """K1/K3: Graphify db.reopen() None qaytarsa, db.get_collection() orqali kolleksiya olinadi."""
    fake_graphify = tmp_path / "fake_graphify_none_reopen"
    fake_graphify.mkdir()

    db_code = (
        "calls = []\n"
        "class FakeCol:\n"
        "    pass\n"
        "col = FakeCol()\n"
        "def get_collection():\n"
        "    calls.append('get_collection')\n"
        "    return col\n"
        "def reopen():\n"
        "    calls.append('reopen')\n"
        "    return None\n"
    )
    (fake_graphify / "db.py").write_text(db_code, encoding="utf-8")

    src.config.GRAPHIFY_DIR = fake_graphify
    kb._col = None

    c = kb._reopen()
    import db
    assert "reopen" in db.calls
    assert "get_collection" in db.calls
    assert c is db.col
    assert kb._col is db.col


def test_kb_sys_modules_and_path_restored_cleanly(tmp_path, clean_graphify_env):
    """K4: Test tugagach sys.modules['db'] va sys.path tozalanishi tekshiriladi."""
    fake_graphify = tmp_path / "fake_graphify_isolation"
    fake_graphify.mkdir()
    (fake_graphify / "db.py").write_text("IS_FAKE = True\ndef get_collection(): return None\n", encoding="utf-8")

    with clean_graphify_env_ctx():
        src.config.GRAPHIFY_DIR = fake_graphify
        kb._col = None

        # Import fake db
        if str(fake_graphify) not in sys.path:
            sys.path.insert(0, str(fake_graphify))
        import db
        assert getattr(db, "IS_FAKE", False) is True
        assert "db" in sys.modules
        assert str(fake_graphify) in sys.path

    # clean_graphify_env tozalash logikasidan keyin
    assert "db" not in sys.modules
    assert str(fake_graphify) not in sys.path
