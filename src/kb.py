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
        except Exception as e:
            # Never fall back silently here: Graphify would keep embedding with
            # its configured model while we wrote Chroma's default vectors into
            # the same collection, corrupting the vector space with no error.
            raise RuntimeError(
                f"Graphify embedder ishga tushmadi ({e}). GRAPHIFY_DIR ko'rsatilgan "
                f"bo'lsa bot AYNAN o'sha embedder bilan yozishi shart — aks holda "
                f"vektorlar bir bazada aralashadi. Bot to'xtatildi."
            ) from e
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
        if _using_graphify() and embed_fn is not None:
            # Same reason as above: dropping embed_fn here would mix models.
            raise
        _col = client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    return _col


def _get_chunking_module():
    import sys
    from . import config
    if str(config.GRAPHIFY_DIR) not in sys.path:
        sys.path.insert(0, str(config.GRAPHIFY_DIR))
    import chunking
    return chunking

def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _reopen():
    """Kolleksiya keshini bekor qilib, mavjud _collection() orqali qayta ochadi."""
    global _col
    _col = None
    return _collection()


def _is_stale_error(exc: Exception) -> bool:
    """Kolleksiya indeksi eskirgani yoki topilmaganini bildiruvchi xatoliklar."""
    msg = str(exc).lower()
    # "does not exist" — ChromaDB kolleksiya qayta yaratilganda (tashqi indekslash
    # UUID ni o'zgartiradi) aynan shu matnni qaytaradi; ro'yxatda bo'lmagani uchun
    # 2026-09-05 da uchta ish qayta ochilmay yiqilgan edi.
    return any(p in msg for p in ("error finding id", "not found", "no such", "stale", "does not exist"))


def _retry(fn, *args, **kwargs):
    """Baza amallarini takrorlash:
    - SQLite 'locked' bo'lsa kutib qayta urinadi (maksimal 5 marta, 0.5s * urinish).
    - Kolleksiya eskirgan bo'lsa ('error finding id', 'not found', 'no such' kabi)
      kolleksiyani qayta ochib bir marta qayta urinadi.
    - Boshqa har qanday xato darhol otiladi.

    Tanlangan yo'l: fn bound method bo'lsa (masalan col.upsert yoki col.query),
    kolleksiya qayta ochilganda fn yangi kolleksiya obyekti metodiga qayta
    bog'lanadi (re-bind: fn = getattr(new_col, fn.__name__)).
    Nega: _retry(fn, *args, **kwargs) signaturasi va mavjud chaqiruv shakllari
    o'zgarmaydi, shu bilan birga eskirgan kolleksiya obyekti bilan qayta urinish
    xavfi to'liq bartaraf etiladi.
    """
    locked_attempts = 0
    reopen_attempts = 0
    max_locked = 5
    max_reopen = 2

    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_msg = str(e).lower()
            if "locked" in err_msg:
                locked_attempts += 1
                if locked_attempts >= max_locked:
                    raise
                time.sleep(0.5 * locked_attempts)
            elif _is_stale_error(e):
                reopen_attempts += 1
                if reopen_attempts > max_reopen:
                    raise
                time.sleep(0.5 * reopen_attempts)
                log.warning("ChromaDB kolleksiyasi eskirgan (%s), qayta ochilib qayta urinilmoqda...", e)
                new_col = _reopen()
                if hasattr(fn, "__self__") and hasattr(fn, "__name__"):
                    fn = getattr(new_col, fn.__name__)
            else:
                raise


def build_documents(record: dict, first_author: str = "", contributors: str = "", author_names: str = "") -> list:
    """Record -> the list of (id, document, metadata) rows that go into Chroma."""
    sc = record["shortcode"]
    url = record.get("url") or f"instagram-file:{sc}"
    tags = ", ".join(t for t in (record.get("tags_en") or []) if t)
    today = record.get("date", "")
    first_author = str(first_author or record.get("first_author", "") or "").strip()
    contributors = str(contributors or record.get("contributors", "") or "").strip()
    author_names = str(author_names or record.get("author_names", "") or "").strip()
    rows = []

    summary_lines = [f"{record.get('title_en', '')} [{record.get('content_type', 'other')}]"]
    summary_lines += [f"- {s}" for s in record.get("summary_uz", [])]
    if record.get("links"):
        summary_lines.append("Havolalar: " + ", ".join(record["links"]))
    if record.get("apply_suggestions_uz"):
        summary_lines.append("Qo'llash tavsiyasi (tavsiya, qo'llanilmagan):")
        summary_lines += [f"- {s}" for s in record["apply_suggestions_uz"]]
    if record.get("items"):
        names = ", ".join(i.get("name_en") or "?" for i in record["items"])
        summary_lines.append(f"Extracted items: {names}")
    summary_doc = "\n".join(l for l in summary_lines if l.strip())

    rows.append((
        f"ig:{sc}:summary",
        summary_doc,
        {"title": record.get("title_en") or sc, "kind": "summary", "tags": tags,
         "source": url, "project": config.PROJECT_LABEL, "date": today,
         "shortcode": sc, "idea_title": record.get("title_en") or sc, "origin": "instagram",
         # `author` is what Graphify's search_knowledge renders (server.py:222);
         # first_author/contributors/author_names stay for the bot's own filters.
         "author": author_names or first_author,
         "first_author": first_author, "contributors": contributors, "author_names": author_names,
         "est_tokens": _est_tokens(summary_doc)},
    ))

    for i, item in enumerate(record.get("items", [])):
        kind = "prompt" if item.get("kind") == "prompt" else "note"
        doc = "\n".join([
            f"{item.get('name_en') or '?'} ({item.get('kind') or '?'}"
            + (f" / {item['subtype']}" if item.get("subtype") else "")
            + ")",
            (item.get("content") or "").strip(),
            f"Izoh: {item.get('note_uz') or ''}".strip(),
            f"Manba: {url}",
        ])
        rows.append((
            f"ig:{sc}:item:{i}",
            doc,
            {"title": item.get("name_en") or f"{sc} item {i}", "kind": kind,
             "tags": tags, "source": url, "project": config.PROJECT_LABEL, "date": today,
             "shortcode": sc, "origin": "instagram",
             "author": author_names or first_author,
             "first_author": first_author, "contributors": contributors, "author_names": author_names,
             # `kind` stays collapsed to prompt/note for Graphify compatibility;
             # item_kind/subtype carry the real taxonomy for filtered search.
             "item_kind": item.get("kind") or "tool", "subtype": item.get("subtype") or "other",
             "verified": bool(item.get("verified")), "est_tokens": _est_tokens(doc)},
        ))

    if record.get("transcript"):
        doc = record["transcript"].strip()
        if doc:
            rows.append((
                f"ig:{sc}:transcript:0",
                doc,
                {"title": f"{record.get('title_en') or sc} (Transcript)", "kind": "transcript",
                 "tags": tags, "source": url, "project": config.PROJECT_LABEL, "date": today,
                 "shortcode": sc, "origin": "instagram",
                 "author": author_names or first_author,
                 "first_author": first_author, "contributors": contributors, "author_names": author_names,
                 "est_tokens": _est_tokens(doc)},
            ))

    if record.get("onscreen"):
        doc = record["onscreen"].strip()
        if doc:
            rows.append((
                f"ig:{sc}:ocr:0",
                doc,
                {"title": f"{record.get('title_en') or sc} (OCR)", "kind": "ocr",
                 "tags": tags, "source": url, "project": config.PROJECT_LABEL, "date": today,
                 "shortcode": sc, "origin": "instagram",
                 "author": author_names or first_author,
                 "first_author": first_author, "contributors": contributors, "author_names": author_names,
                 "est_tokens": _est_tokens(doc)},
            ))

    if record.get("caption"):
        doc = record["caption"].strip()
        if doc:
            rows.append((
                f"ig:{sc}:caption:0",
                doc,
                {"title": f"{record.get('title_en') or sc} (Caption)", "kind": "caption",
                 "tags": tags, "source": url, "project": config.PROJECT_LABEL, "date": today,
                 "shortcode": sc, "origin": "instagram",
                 "author": author_names or first_author,
                 "first_author": first_author, "contributors": contributors, "author_names": author_names,
                 "est_tokens": _est_tokens(doc)},
            ))

    final_rows = []
    chunking = _get_chunking_module()
    for _id, _doc, meta in rows:
        chunks = chunking.chunk_by_tokens(_doc, parent_id=_id)
        if len(chunks) == 1:
            meta["id"] = _id
            meta["parent_id"] = _id
            meta["canonical_id"] = _id
            meta["chunk_index"] = 0
            meta["chunk_total"] = 1
            meta["token_count"] = chunks[0]["token_count"]
            final_rows.append((_id, chunks[0]["text"], meta))
        else:
            for c in chunks:
                cmeta = dict(meta)
                cid = f"{_id}:part:{c['chunk_index']}"
                cmeta["id"] = cid
                cmeta["parent_id"] = _id
                cmeta["canonical_id"] = cid
                cmeta["chunk_index"] = c["chunk_index"]
                cmeta["chunk_total"] = c["chunk_total"]
                cmeta["token_count"] = c["token_count"]
                final_rows.append((cid, c["text"], cmeta))

    return final_rows


def upsert_record(record: dict, author_id: str = "", author_name: str = "") -> int:
    sc = record["shortcode"]
    if not record.get("usable"):
        delete_shortcode(sc)
        return 0
        
    author_id = str(author_id or record.get("author_id", "") or "").strip()
    author_name = str(author_name or record.get("author_name", "") or "").strip()
    col = _collection()

    existing_first = ""
    existing_contrib = ""
    existing_names = ""
    has_existing = False

    try:
        res = _retry(col.get, where={"shortcode": sc}, include=["metadatas"])
        if res and res.get("metadatas"):
            has_existing = True
            for m in res["metadatas"]:
                if m:
                    if not existing_first and m.get("first_author"):
                        existing_first = m["first_author"]
                    if not existing_contrib and m.get("contributors"):
                        existing_contrib = m["contributors"]
                    if not existing_names and m.get("author_names"):
                        existing_names = m["author_names"]
                    if existing_first:
                        break
    except Exception as e:
        log.warning("could not read existing metadata for %s: %s", sc, e)

    if has_existing:
        first_author = existing_first or author_id or str(record.get("first_author", "") or "").strip()
        contrib_list = [c.strip() for c in existing_contrib.split(",") if c.strip()]
        name_list = [n.strip() for n in existing_names.split(",") if n.strip()]

        if not contrib_list and existing_first:
            contrib_list.append(existing_first)

        if author_id:
            if author_id not in contrib_list:
                contrib_list.append(author_id)
                if author_name:
                    name_list.append(author_name)
        elif not contrib_list and record.get("contributors"):
            contrib_list = [c.strip() for c in str(record["contributors"]).split(",") if c.strip()]
            if record.get("author_names"):
                name_list = [n.strip() for n in str(record["author_names"]).split(",") if n.strip()]

        contributors = ", ".join(contrib_list)
        author_names = ", ".join(name_list)
    else:
        first_author = str(record.get("first_author", "") or "").strip() or author_id
        contrib_list = [c.strip() for c in str(record.get("contributors", "") or "").split(",") if c.strip()]
        name_list = [n.strip() for n in str(record.get("author_names", "") or "").split(",") if n.strip()]

        if not contrib_list and first_author:
            contrib_list.append(first_author)

        if author_id:
            if author_id not in contrib_list:
                contrib_list.append(author_id)
                if author_name:
                    name_list.append(author_name)
        elif not contrib_list and author_name:
            name_list.append(author_name)

        contributors = ", ".join(contrib_list) if contrib_list else author_id
        author_names = ", ".join(name_list) if name_list else author_name

    record["first_author"] = first_author
    record["contributors"] = contributors
    record["author_names"] = author_names

    rows = build_documents(record, first_author=first_author, contributors=contributors, author_names=author_names)
    # Find existing IDs
    existing_res = _retry(col.get, where={"shortcode": sc}, include=[])
    existing_ids = set(existing_res.get("ids", [])) if existing_res else set()

    _retry(col.upsert,
           ids=[r[0] for r in rows],
           documents=[r[1] for r in rows],
           metadatas=[r[2] for r in rows])
           
    if _using_graphify():
        import sys
        if str(config.GRAPHIFY_DIR) not in sys.path:
            sys.path.insert(0, str(config.GRAPHIFY_DIR))
        import keyword_index
        kw_idx = keyword_index.get_index()
        kw_idx.upsert(
            [r[0] for r in rows],
            [r[1] for r in rows],
            [r[2] for r in rows]
        )
        
    # Delete orphaned IDs
    new_ids = set(r[0] for r in rows)
    orphaned_ids = list(existing_ids - new_ids)
    if orphaned_ids:
        _retry(col.delete, ids=orphaned_ids)
        if _using_graphify():
            kw_idx.delete(orphaned_ids)

    return len(rows)


def delete_shortcode(shortcode: str) -> None:
    existing_res = _retry(_collection().get, where={"shortcode": shortcode}, include=[])
    existing_ids = existing_res.get("ids", []) if existing_res else []
    
    _retry(_collection().delete, where={"shortcode": shortcode})
    
    if _using_graphify() and existing_ids:
        import sys
        if str(config.GRAPHIFY_DIR) not in sys.path:
            sys.path.insert(0, str(config.GRAPHIFY_DIR))
        import keyword_index
        kw_idx = keyword_index.get_index()
        kw_idx.delete(existing_ids)


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
        
    if _using_graphify():
        import sys
        if str(config.GRAPHIFY_DIR) not in sys.path:
            sys.path.insert(0, str(config.GRAPHIFY_DIR))
        import search_core, keyword_index
        kw_idx = keyword_index.get_index()
        hybrid_res = search_core.hybrid_search(
            query=query,
            collection=col,
            kw_index=kw_idx,
            top_k=top_k,
            where=where if where else None
        )
        # hybrid_search might return dict with 'results' key or list
        if isinstance(hybrid_res, dict) and "results" in hybrid_res:
            hybrid_res = hybrid_res["results"]
        elif not isinstance(hybrid_res, list):
            hybrid_res = []
            
        formatted_results = []
        for r in hybrid_res:
            item = dict(r.get("meta", {}))
            item["id"] = r.get("id")
            item["similarity"] = r.get("similarity", 0.0)
            item["raw_similarity"] = r.get("raw_similarity", item["similarity"])
            item["snippet"] = r.get("text", "")[:200]
            if "shortcode" not in item and item.get("id", "").startswith("ig:"):
                parts = item["id"].split(":")
                if len(parts) >= 2:
                    item["shortcode"] = parts[1]
            formatted_results.append(item)
        return formatted_results
        
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
