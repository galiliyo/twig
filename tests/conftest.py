import json
import os

import asyncpg
import pytest_asyncio
from pgvector.asyncpg import register_vector

TEST_DSN = os.environ.get("TEST_DATABASE_URL", os.environ.get("DATABASE_URL", ""))


async def _init_conn(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


@pytest_asyncio.fixture(scope="session")
async def test_pool():
    pool = await asyncpg.create_pool(TEST_DSN, init=_init_conn)
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(test_pool):
    yield
    async with test_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS items, bot_state CASCADE")
        await conn.execute("DROP INDEX IF EXISTS items_embedding_idx, items_tags_idx, items_trgm_idx")
