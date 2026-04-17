"""
Semantic + fuzzy search over saved items in Postgres.

Embeddings are written at save time — no index rebuild needed.
/reindex backfills embeddings for rows missing them (e.g. imported data).
"""

import logging

from core.ai import embed_query, embed_texts
from core.db import search as _db_search, _pool

_log = logging.getLogger(__name__)


async def search(query: str, wm=None, top_k: int = 5) -> list[dict]:
    """Search saved items using semantic + fuzzy matching combined via RRF.
    wm parameter accepted for API compatibility but unused."""
    q_emb = await embed_query(query)
    return await _db_search(query_text=query, query_embedding=q_emb, top_k=top_k)


async def build_index(wm=None) -> list[dict]:
    """Backfill embeddings for items that are missing them. Returns updated rows."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, branch_path, note FROM items WHERE embedding IS NULL"
        )

    if not rows:
        _log.info("search: all items already have embeddings")
        return []

    texts = [
        f"{' > '.join(list(r['branch_path']))}: {r['title']}. {(r['note'] or '')[:800]}".strip()
        for r in rows
    ]
    embeddings = await embed_texts(texts)

    async with _pool.acquire() as conn:
        await conn.executemany(
            "UPDATE items SET embedding = $1 WHERE id = $2",
            [(emb, row["id"]) for emb, row in zip(embeddings, rows)],
        )

    _log.info("search: backfilled embeddings for %d items", len(rows))
    return [dict(r) for r in rows]


def invalidate_index() -> None:
    """No-op — the Postgres index is always live."""
    pass
