import pytest
import core.db as db


async def test_init_db_creates_tables(test_pool, clean_tables):
    await db.init_db(pool=test_pool)
    async with test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    names = {r["tablename"] for r in rows}
    assert "items" in names
    assert "bot_state" in names


async def test_save_item_returns_id(test_pool, clean_tables):
    await db.init_db(pool=test_pool)
    item_id = await db.save_item(
        text_input="https://example.com",
        title="Example Article",
        url="https://example.com",
        branch_path=["Tech", "Web"],
        tags=["web", "example"],
        note="A short note.",
        embedding=[0.1] * 1536,
    )
    assert isinstance(item_id, int)
    assert item_id > 0


async def test_get_recent_returns_latest_first(test_pool, clean_tables):
    await db.init_db(pool=test_pool)
    await db.save_item(
        text_input="first message", title="First", url=None,
        branch_path=["Ideas"], tags=[], note=None, embedding=[0.0] * 1536,
    )
    await db.save_item(
        text_input="second message", title="Second", url=None,
        branch_path=["Ideas"], tags=[], note=None, embedding=[0.0] * 1536,
    )
    recent = await db.get_recent(10)
    assert recent[0] == "second message"
    assert recent[1] == "first message"


async def test_get_last_saved_returns_none_when_empty(test_pool, clean_tables):
    await db.init_db(pool=test_pool)
    assert await db.get_last_saved() is None


async def test_set_and_get_last_saved(test_pool, clean_tables):
    await db.init_db(pool=test_pool)
    item = {"id": 42, "branch_path": ["Tech", "AI"], "title": "Some Article",
            "url": "https://example.com", "note": "A note"}
    await db.set_last_saved(item)
    result = await db.get_last_saved()
    assert result == item


async def test_set_last_saved_overwrites(test_pool, clean_tables):
    await db.init_db(pool=test_pool)
    await db.set_last_saved({"id": 1, "title": "Old"})
    await db.set_last_saved({"id": 2, "title": "New"})
    result = await db.get_last_saved()
    assert result["title"] == "New"


async def test_update_item_path(test_pool, clean_tables):
    await db.init_db(pool=test_pool)
    item_id = await db.save_item(
        text_input="some url", title="Old Title", url=None,
        branch_path=["Old", "Branch"], tags=[], note=None, embedding=[0.0] * 1536,
    )
    await db.update_item_path(item_id, ["New", "Branch"])
    async with test_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT branch_path FROM items WHERE id = $1", item_id)
    assert list(row["branch_path"]) == ["New", "Branch"]
