"""Semantic vector store — ChromaDB + sentence-transformers for knowledge retrieval.

Provides persistent semantic search over the knowledge base so agents can find
conceptually similar insights even when keywords don't match. Falls back
gracefully if ChromaDB or sentence-transformers are not installed.

Storage: DATA_DIR/vector_store (persistent, survives restarts).
Model: all-MiniLM-L6-v2 — 22M parameters, 384-dim embeddings, ~0.1s/embed on CPU.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
_VECTOR_DIR = DATA_DIR / "vector_store"
_COLLECTION_NAME = "ot_knowledge"
_MODEL_NAME = "all-MiniLM-L6-v2"

# Lazy singletons — initialised on first use
_client = None
_collection = None
_model = None


def _available() -> bool:
    """Return True if ChromaDB and sentence-transformers are importable."""
    try:
        import chromadb  # noqa: F401
        from sentence_transformers import SentenceTransformer  # noqa: F401
        return True
    except ImportError:
        return False


def _get_collection():
    global _client, _collection
    if _collection is not None:
        return _collection
    import chromadb
    _VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(_VECTOR_DIR))
    _collection = _client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    return _collection


def _get_model():
    global _model
    if _model is not None:
        return _model
    from sentence_transformers import SentenceTransformer
    _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _embed(text: str) -> list[float]:
    return _get_model().encode(text, normalize_embeddings=True).tolist()


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def insert_knowledge_embedding(row_id: int, title: str, content: str, category: str) -> None:
    """Embed and upsert a knowledge record into ChromaDB.

    Called from store.insert_knowledge() — non-blocking; silently skips if
    vector store is unavailable.
    """
    if not _available():
        return
    try:
        text = f"{title}\n{content}"
        emb = _embed(text)
        _get_collection().upsert(
            ids=[str(row_id)],
            embeddings=[emb],
            documents=[text],
            metadatas=[{"title": title, "category": category, "row_id": row_id}],
        )
        log.debug("Vector store upserted knowledge id=%d", row_id)
    except Exception as e:
        log.warning("Vector store insert failed for id=%d: %s", row_id, e)


def insert_batch(records: list[dict]) -> None:
    """Bulk upsert a list of knowledge records.

    Each record: {"id": int, "title": str, "content": str, "category": str}
    """
    if not _available() or not records:
        return
    try:
        ids, embeddings, documents, metadatas = [], [], [], []
        for r in records:
            text = f"{r['title']}\n{r['content']}"
            ids.append(str(r["id"]))
            embeddings.append(_embed(text))
            documents.append(text)
            metadatas.append({
                "title": r["title"],
                "category": r.get("category", ""),
                "row_id": r["id"],
            })
        _get_collection().upsert(ids=ids, embeddings=embeddings,
                                  documents=documents, metadatas=metadatas)
        log.info("Vector store bulk upserted %d records", len(records))
    except Exception as e:
        log.warning("Vector store batch insert failed: %s", e)


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def semantic_search(
    query: str,
    n_results: int = 5,
    category: str | None = None,
) -> list[dict]:
    """Return knowledge items semantically similar to query.

    Args:
        query: Natural-language search query.
        n_results: Max number of results.
        category: Optional category filter (e.g. "strategy", "lesson").

    Returns:
        List of {"content": str, "title": str, "category": str,
                 "row_id": int, "distance": float}
        Lower distance = more similar (cosine distance: 0=identical, 2=opposite).
        Empty list if store unavailable or query fails.
    """
    if not _available():
        return []
    try:
        emb = _embed(query)
        where = {"category": category} if category else None
        coll = _get_collection()
        if coll.count() == 0:
            return []
        results = coll.query(
            query_embeddings=[emb],
            n_results=min(n_results, coll.count()),
            where=where,
        )
        out = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            out.append({
                "content": doc,
                "title": meta.get("title", ""),
                "category": meta.get("category", ""),
                "row_id": meta.get("row_id", -1),
                "distance": round(results["distances"][0][i], 4),
            })
        return out
    except Exception as e:
        log.warning("Vector store search failed: %s", e)
        return []


def search_similar_signals(description: str, n_results: int = 3) -> list[dict]:
    """Find past knowledge entries contextually similar to a signal description.

    Useful for agents to surface relevant lessons before acting on a signal.
    """
    return semantic_search(description, n_results=n_results, category=None)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def rebuild_from_db() -> int:
    """Rebuild vector store from the full knowledge table in SQLite.

    Run this once after installing chromadb to backfill existing knowledge.
    Returns number of records indexed.
    """
    if not _available():
        log.warning("Cannot rebuild: chromadb or sentence-transformers not installed.")
        return 0
    try:
        from trading.db.store import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, title, content, category FROM knowledge ORDER BY id"
            ).fetchall()
        records = [
            {"id": r["id"], "title": r["title"] or "", "content": r["content"] or "",
             "category": r["category"] or ""}
            for r in rows
        ]
        insert_batch(records)
        return len(records)
    except Exception as e:
        log.error("Vector store rebuild failed: %s", e)
        return 0


def collection_size() -> int:
    """Return number of embeddings currently stored."""
    if not _available():
        return 0
    try:
        return _get_collection().count()
    except Exception:
        return 0
