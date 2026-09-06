import pytest

import sys

from pathlib import Path



# Mock config to test kb in isolation

class MockConfig:

    GRAPHIFY_DIR = Path("D:/claude projects/graphify-ekotizim/Graphify")

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





