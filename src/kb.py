"""Writing into the knowledge-base ChromaDB.

Two backends, chosen automatically:
  - Standalone (default): a bundled ChromaDB at data/kb_db, Chroma's own default
    embedder. Works with zero extra setup.
  - Graphify-linked (opt-in, GRAPHIFY_DIR set in .env): writes into an existing
    Graphify installation's kg_db instead, reusing its embedder factory so a
    single collection is never mixed across two different embedding models.

Ids are deterministic (`ig:<shortcode>:...`) and writes are upserts, so sending
the same reel twice updates one entry instead of creating a second copy, and the
delete button has something stable to target.
"""

import logging
import sys
import time

import chromadb

from . import config

log = logging.getLogger(__name__)

COLLECTION = "graphify"
_col = None
_logged_backend = False


def _using_graphify() -> bool:
    return config.GRAPHIFY_DIR is not None and config.GRAPHIFY_DIR.is_dir()


def _collection():
    global _col, _logged_backend
    if _col is not None:
        return _col

    embed_fn = None
    if _using_graphify():
        db_path = config.GRAPHIFY_DIR / "kg_db"
        # Reuse Graphify's own embedder factory: one collection cannot mix
        # embedding models, so the bot must embed exactly the way it does.
        sys.path.insert(0, str(config.GRAPHIFY_DIR))
        try:
            from embedders import build_chroma_embedding_function
            embed_fn = build_chroma_embedding_function()
        except Exception:
            embed_fn = None
        if not _logged_backend:
            log.info("kb backend: Graphify-linked (%s)", db_path)
    else:
        db_path = config.STANDALONE_KB_DIR
        if not _logged_backend:
            log.info("kb backend: standalone (%s) — set GRAPHIFY_DIR in .env to "
                     "link an existing Graphify installation instead", db_path)
    _logged_backend = True

    client = chromadb.PersistentClient(path=str(db_path))
    kwargs = {"metadata": {"hnsw:space": "cosine"}}
    if embed_fn is not None:
        kwargs["embedding_function"] = embed_fn
    try:
        _col = client.get_or_create_collection(COLLECTION, **kwargs)
    except ValueError:
        _col = client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    return _col


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _retry(fn, *args, **kwargs):
    """The dashboard and the MCP server hold the same SQLite file open."""
    for attempt in range(5):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if "locked" not in str(e).lower() or attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))


def build_documents(record: dict) -> list:
    """Record -> the list of (id, document, metadata) rows that go into Chroma."""
    sc = record["shortcode"]
    url = record.get("url") or f"instagram-file:{sc}"
    tags = ", ".join(record.get("tags_en", []))
    today = record.get("date", "")
    rows = []

    summary_lines = [f"{record.get('title_en', '')} [{record.get('content_type', 'other')}]"]
    summary_lines += [f"- {s}" for s in record.get("summary_uz", [])]
    if record.get("links"):
        summary_lines.append("Havolalar: " + ", ".join(record["links"]))
    if record.get("apply_suggestions_uz"):
        summary_lines.append("Qo'llash tavsiyasi (tavsiya, qo'llanilmagan):")
        summary_lines += [f"- {s}" for s in record["apply_suggestions_uz"]]
    if record.get("items"):
        names = ", ".join(i.get("name_en", "?") for i in record["items"])
        summary_lines.append(f"Extracted items: {names}")
    summary_doc = "\n".join(l for l in summary_lines if l.strip())

    rows.append((
        f"ig:{sc}:summary",
        summary_doc,
        {"title": record.get("title_en") or sc, "kind": "summary", "tags": tags,
         "source": url, "project": config.PROJECT_LABEL, "date": today,
         "shortcode": sc, "idea_title": record.get("title_en") or sc, "origin": "instagram", "est_tokens": _est_tokens(summary_doc)},
    ))

    for i, item in enumerate(record.get("items", [])):
        kind = "prompt" if item.get("kind") == "prompt" else "note"
        doc = "\n".join([
            f"{item.get('name_en', '?')} ({item.get('kind', '?')})",
            item.get("content", "").strip(),
            f"Izoh: {item.get('note_uz', '')}".strip(),
            f"Manba: {url}",
        ])
        rows.append((
            f"ig:{sc}:item:{i}",
            doc,
            {"title": item.get("name_en") or f"{sc} item {i}", "kind": kind,
             "tags": tags, "source": url, "project": config.PROJECT_LABEL, "date": today,
             "shortcode": sc, "origin": "instagram",
             "verified": bool(item.get("verified")), "est_tokens": _est_tokens(doc)},
        ))

    for _id, _doc, meta in rows:
        meta["id"] = _id
    return rows


def upsert_record(record: dict) -> int:
    rows = build_documents(record)
    col = _collection()
    _retry(col.upsert,
           ids=[r[0] for r in rows],
           documents=[r[1] for r in rows],
           metadatas=[r[2] for r in rows])
    return len(rows)


def delete_shortcode(shortcode: str) -> None:
    _retry(_collection().delete, where={"shortcode": shortcode})


def count_for(shortcode: str) -> int:
    res = _retry(_collection().get, where={"shortcode": shortcode}, include=[])
    return len(res.get("ids", []))


def search(query: str, top_k: int = 5, project: str | None = None,
           kind: str | None = None, exclude_shortcode: str | None = None) -> list[dict]:
    """Semantic search over the collection."""
    col = _collection()
    where_conds = []
    
    if project is None:
        where_conds.append({"project": config.PROJECT_LABEL})
    elif project != "all":
        where_conds.append({"project": project})
        
    if kind:
        where_conds.append({"kind": kind})
        
    if exclude_shortcode:
        where_conds.append({"shortcode": {"$ne": exclude_shortcode}})
        
    where = {}
    if len(where_conds) == 1:
        where = where_conds[0]
    elif len(where_conds) > 1:
        where = {"$and": where_conds}
        
    res = _retry(col.query, query_texts=[query], n_results=top_k, where=where if where else None)
    
    results = []
    if res and res.get("ids") and res["ids"][0]:
        for i, doc_id in enumerate(res["ids"][0]):
            meta = res["metadatas"][0][i]
            dist = res["distances"][0][i]
            snippet = res["documents"][0][i][:200]
            sim = max(0.0, 1.0 - dist)
            item = dict(meta)
            item["id"] = doc_id
            item["similarity"] = sim
            item["snippet"] = snippet
            results.append(item)
            
    return results
