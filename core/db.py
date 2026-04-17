import json
import logging
import os

import asyncpg
from pgvector.asyncpg import register_vector

_log = logging.getLogger(__name__)
_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def init_db(dsn: str | None = None, pool: asyncpg.Pool | None = None) -> None:
    global _pool
    if pool is not None:
        _pool = pool
    else:
        dsn = dsn or os.environ["DATABASE_URL"]
        _pool = await asyncpg.create_pool(dsn, init=_init_conn)

    async with _pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id          SERIAL PRIMARY KEY,
                text_input  TEXT        NOT NULL,
                title       TEXT        NOT NULL,
                url         TEXT,
                branch_path TEXT[]      NOT NULL DEFAULT '{}',
                tags        TEXT[]      NOT NULL DEFAULT '{}',
                note        TEXT,
                embedding   vector(1536),
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key        TEXT PRIMARY KEY,
                value      JSONB       NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS items_embedding_idx
            ON items USING hnsw (embedding vector_cosine_ops)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS items_tags_idx
            ON items USING GIN (tags)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS items_trgm_idx
            ON items USING GIN ((title || ' ' || COALESCE(note, '')) gin_trgm_ops)
        """)
    _log.info("DB schema ready")


async def save_item(
    *,
    text_input: str,
    title: str,
    url: str | None,
    branch_path: list[str],
    tags: list[str],
    note: str | None,
    embedding: list[float],
) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO items (text_input, title, url, branch_path, tags, note, embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            text_input, title, url, branch_path, tags, note, embedding,
        )
    return row["id"]


async def get_recent(n: int = 50) -> list[str]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT text_input FROM items ORDER BY created_at DESC LIMIT $1", n
        )
    return [r["text_input"] for r in rows]


async def get_last_saved() -> dict | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM bot_state WHERE key = 'last_saved'"
        )
    return row["value"] if row else None


async def set_last_saved(item: dict) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES ('last_saved', $1, NOW())
            ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()
            """,
            item,
        )


async def update_item_path(item_id: int, branch_path: list[str]) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE items SET branch_path = $1 WHERE id = $2",
            branch_path, item_id,
        )


def _rrf_merge(
    sem_rows: list[dict],
    fuzz_rows: list[dict],
    top_k: int,
    k: int = 60,
) -> list[dict]:
    scores: dict[int, float] = {}
    rows_by_id: dict[int, dict] = {}
    for rank, row in enumerate(sem_rows):
        rid = row["id"]
        scores[rid] = scores.get(rid, 0) + 1 / (k + rank + 1)
        rows_by_id[rid] = row
    for rank, row in enumerate(fuzz_rows):
        rid = row["id"]
        scores[rid] = scores.get(rid, 0) + 1 / (k + rank + 1)
        rows_by_id[rid] = row
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    return [rows_by_id[rid] | {"score": score} for rid, score in ranked]


async def search(
    query_text: str,
    query_embedding: list[float],
    top_k: int = 5,
) -> list[dict]:
    async with _pool.acquire() as conn:
        sem_rows = [
            dict(r) for r in await conn.fetch(
                """
                SELECT id, title, url, branch_path, tags, note,
                       embedding <=> $1 AS dist
                FROM items
                WHERE embedding IS NOT NULL
                ORDER BY dist
                LIMIT 20
                """,
                query_embedding,
            )
        ]
        fuzz_rows = [
            dict(r) for r in await conn.fetch(
                """
                SELECT id, title, url, branch_path, tags, note,
                       similarity(title || ' ' || COALESCE(note, ''), $1) AS sim
                FROM items
                WHERE title || ' ' || COALESCE(note, '') % $1
                ORDER BY sim DESC
                LIMIT 20
                """,
                query_text,
            )
        ]
    results = _rrf_merge(sem_rows, fuzz_rows, top_k=top_k)
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "path": " > ".join(list(r["branch_path"])),
            "tags": list(r["tags"]),
            "note_snippet": (r.get("note") or "")[:300],
            "score": r["score"],
        }
        for r in results
    ]
