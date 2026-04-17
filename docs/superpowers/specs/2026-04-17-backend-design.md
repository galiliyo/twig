# Twig Backend: Postgres + Reliable Search

**Date:** 2026-04-17  
**Status:** Approved

## Problem

The current bot has three compounding issues:

1. All state (`_recent`, `_last_saved`, `_replace_pending`) is in-memory — wiped on every restart. `/replace` silently breaks after any Railway restart.
2. Search is unreliable — it rebuilds embeddings from the full WiseMapping XML on every cache miss, stores them in a flat `search_index.json` file, and has no fuzzy text matching.
3. WiseMapping is used as the source of truth — every write reads the full XML, manipulates it, and PUTs it back. This is brittle and race-prone.

## Solution

Make Postgres the source of truth. WiseMapping becomes a display mirror — the bot pushes to it after the DB write succeeds, fire-and-forget. Search runs entirely against the DB.

## Database Schema

### `items` table

| Column       | Type           | Notes                                      |
|--------------|----------------|--------------------------------------------|
| `id`         | serial PK      |                                            |
| `text_input` | text           | Original message the user sent             |
| `title`      | text           | Leaf node title (≤6 words)                 |
| `url`        | text           | Nullable                                   |
| `branch_path`| text[]         | e.g. `["Tech", "AI", "Tools"]`             |
| `tags`       | text[]         | AI-generated, e.g. `["ai", "research"]`    |
| `note`       | text           | Bullet summary, nullable                   |
| `embedding`  | vector(1536)   | OpenAI text-embedding-3-small              |
| `created_at` | timestamptz    | Set on insert                              |

Indexes:
- `embedding vector_cosine_ops` via `ivfflat` (pgvector semantic search)
- GIN on `tags` (containment queries)
- GIN trigram on `title || ' ' || COALESCE(note, '')` (fuzzy text search)

### `bot_state` table

| Column       | Type        | Notes                    |
|--------------|-------------|--------------------------|
| `key`        | text PK     | e.g. `"last_saved"`      |
| `value`      | jsonb       |                          |
| `updated_at` | timestamptz |                          |

Used for: `last_saved` (persists across restarts for `/replace`). The stored value includes the `items.id` so `/replace` can update the correct row.

### Postgres Extensions

Both enabled in `init_db()` on startup:
- `pgvector` — vector similarity search
- `pg_trgm` — trigram fuzzy text search

## Write Path

1. **Extract** — `core/extractor.py` (unchanged)
2. **AI placement** — `core/ai.py` returns `branch_path`, `title`, `tags` (tags added to JSON schema)
3. **Summarize** — `core/ai.py` generates bullet note (unchanged)
4. **Embed** — `core/ai.py` `embed_texts()` generates the embedding at write time
5. **DB write** — `INSERT INTO items ...` — this is the commit point
6. **WiseMapping sync** — fire-and-forget `wm.add_node()` after DB write. Failure is logged but does not fail the user-facing operation.

### `/replace` flow

1. Update `branch_path` in the `items` row (DB write first)
2. Update `bot_state.last_saved`
3. Fire-and-forget `wm.move_node()`

### State previously in memory

| Was                  | Now                                                         |
|----------------------|-------------------------------------------------------------|
| `_recent: list[str]` | `SELECT text_input FROM items ORDER BY created_at DESC LIMIT 50` |
| `_last_saved: dict`  | `bot_state` key `"last_saved"` (jsonb)                      |
| `_replace_pending`   | Unchanged — in-memory only (per-request, not cross-restart) |

## Search

`core/search.py` is rewritten. No XML fetching, no flat file, no `/reindex` needed (embeddings are written at save time).

### Two modes, combined via Reciprocal Rank Fusion (RRF)

**Semantic search** (pgvector):
```sql
SELECT *, embedding <=> $1 AS dist
FROM items
ORDER BY dist
LIMIT 20
```

**Fuzzy text search** (pg_trgm):
```sql
SELECT *, similarity(title || ' ' || COALESCE(note, ''), $1) AS sim
FROM items
WHERE title || ' ' || COALESCE(note, '') % $1
ORDER BY sim DESC
LIMIT 20
```

RRF merges both ranked lists: `score = 1/(k + rank_semantic) + 1/(k + rank_fuzzy)` where `k=60`.

### Tag filtering

If any word in the query exactly matches a tag, results are filtered to `tags @> ARRAY[$word]` before ranking. This makes tag-based retrieval precise and fast.

### `/reindex`

Becomes a maintenance command that backfills `embedding` for any rows where it is NULL. No longer a required operation for search to work.

## New Module: `core/db.py`

Public interface:

```python
async def init_db() -> None
async def save_item(text_input, title, url, branch_path, tags, note, embedding) -> int
async def get_recent(n: int) -> list[str]
async def get_last_saved() -> dict | None
async def set_last_saved(item: dict) -> None
async def update_item_path(item_id: int, branch_path: list[str]) -> None
async def search(query_text: str, query_embedding: list[float], top_k: int) -> list[dict]
```

Uses `asyncpg` directly (no ORM). Connection pool initialized in `init_db()`, stored as a module-level singleton.

## Files Changed

| File               | Change                                                                 |
|--------------------|------------------------------------------------------------------------|
| `core/db.py`       | **New** — all DB logic                                                 |
| `core/ai.py`       | Add `tags` to `choose_placement` JSON schema and `Placement` dataclass |
| `core/search.py`   | Rewrite to query DB                                                    |
| `bot.py`           | Replace in-memory state with DB calls; call `init_db()` on startup; WiseMapping sync fire-and-forget |
| `core/extractor.py`| Unchanged                                                              |
| `core/wisemapping.py` | Unchanged                                                           |
| `requirements.txt` | Add `asyncpg`, `pgvector`                                              |

## Railway Setup

1. Add **Postgres** addon via Railway dashboard → `DATABASE_URL` env var set automatically
2. `pgvector` must be enabled — Railway's Postgres supports it: `CREATE EXTENSION IF NOT EXISTS vector`
3. No schema migrations needed beyond `init_db()` running on startup

## `update_notes.py` Integration

This local maintenance script backfills notes on nodes whose content was blocked/empty at save time. With DB as source of truth it must also update the DB when it writes a refreshed note back to WiseMapping.

Changes needed:
- **DB write first**: `UPDATE items SET note = $1, embedding = $2 WHERE url = $3` — this is the commit point
- **WiseMapping sync after**: push the updated note to WiseMapping as fire-and-forget (same pattern as the bot write path)
- Requires `DATABASE_URL` in the local `.env` to connect from the dev machine

## Out of Scope

- REST API / web dashboard
- Webhooks (keep polling)
- WiseMapping reconciliation / drift detection (future work)
- Multi-user support
