"""Phase 3: ChromaDB vector store over segment text (local, free embeddings).

ChromaDB holds ONLY vectors + lightweight metadata; the metadata's `segment_id`
is the join key back to the authoritative text/context in SQLite (the system of
record). Embeddings are produced locally by sentence-transformers all-MiniLM-L6-v2
-- no API, no cost. The model downloads (~90MB) on first use, then caches.
"""
from __future__ import annotations

import sqlite3

from financial_analysis_agent.utils import config

COLLECTION = "segments"
EMBED_MODEL = "all-MiniLM-L6-v2"

_client = None
_collection = None


def get_collection():
    """Lazily build a persistent Chroma collection with local embeddings (cosine)."""
    global _client, _collection
    if _collection is not None:
        return _collection
    import chromadb
    from chromadb.utils import embedding_functions

    config.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(config.CHROMA_PATH))
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    _collection = _client.get_or_create_collection(
        name=COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    return _collection


def _meta(row: sqlite3.Row) -> dict:
    # Chroma metadata values must be str/int/float/bool -- no None.
    return {
        "segment_id": int(row["segment_id"]),
        "call_id": int(row["call_id"]),
        "company_id": int(row["company_id"]),
        "ticker": row["ticker"] or "",
        "speaker_role": row["speaker_role"] or "",
        "section": row["section"] or "",
        "fiscal_year": int(row["fiscal_year"]) if row["fiscal_year"] is not None else 0,
        "fiscal_quarter": int(row["fiscal_quarter"]) if row["fiscal_quarter"] is not None else 0,
    }


def index_call(conn: sqlite3.Connection, call_id: int, *, batch: int = 256) -> int:
    """Embed + upsert all segments of one call. Idempotent (id == segment id)."""
    rows = conn.execute(
        "SELECT s.id segment_id, s.call_id, s.speaker_role, s.section, s.text, "
        "       c.company_id, c.fiscal_year, c.fiscal_quarter, co.ticker "
        "FROM segments s JOIN calls c ON c.id = s.call_id "
        "JOIN companies co ON co.id = c.company_id "
        "WHERE s.call_id = ? AND s.text IS NOT NULL AND length(trim(s.text)) > 0 "
        "ORDER BY s.seq",
        (call_id,),
    ).fetchall()
    if not rows:
        return 0
    coll = get_collection()
    total = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        coll.upsert(
            ids=[str(r["segment_id"]) for r in chunk],
            documents=[r["text"] for r in chunk],
            metadatas=[_meta(r) for r in chunk],
        )
        total += len(chunk)
    return total


def index_all(conn: sqlite3.Connection) -> dict[int, int]:
    """Index every call in the DB. Returns {call_id: segments_indexed}."""
    call_ids = [r["id"] for r in conn.execute("SELECT id FROM calls ORDER BY id")]
    return {cid: index_call(conn, cid) for cid in call_ids}


def query(question: str, *, n_results: int = 6, where: dict | None = None) -> list[dict]:
    """Similarity search. Returns ranked hits with segment_id + distance."""
    coll = get_collection()
    res = coll.query(
        query_texts=[question],
        n_results=n_results,
        where=where or None,
    )
    hits = []
    ids = res.get("ids", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    docs = res.get("documents", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for i, _id in enumerate(ids):
        hits.append({
            "segment_id": int(_id),
            "distance": dists[i] if i < len(dists) else None,
            "document": docs[i] if i < len(docs) else None,
            "metadata": metas[i] if i < len(metas) else {},
        })
    return hits


def count() -> int:
    return get_collection().count()
