import json
import os

import asyncpg
import pytest
import pytest_asyncio
from dotenv import load_dotenv
from pgvector.asyncpg import register_vector

load_dotenv()

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
    if not TEST_DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    # Extensions must exist before the pool starts (register_vector runs on connect)
    setup_conn = await asyncpg.connect(TEST_DSN, ssl=False)
    try:
        await setup_conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await setup_conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    finally:
        await setup_conn.close()
    pool = await asyncpg.create_pool(TEST_DSN, ssl=False, init=_init_conn)
    yield pool
    await pool.close()


@pytest_asyncio.fixture()
async def clean_tables(test_pool):
    yield
    async with test_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS items, bot_state CASCADE")
        await conn.execute(
            "DROP INDEX IF EXISTS items_embedding_idx, items_tags_idx, items_trgm_idx"
        )
