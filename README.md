# Twig

A personal Telegram bot that captures links, ideas, and reminders into a Postgres database and a WiseMapping mind map, using AI for placement and semantic search for retrieval.

## What it does

Send any message to the bot and it:

1. Detects the input type (URL, YouTube link, idea, reminder)
2. Fetches article metadata via direct HTTP, Playwright (paywalled sites), yt-dlp (YouTube), or Exa fallback
3. Uses AI to choose the best branch from your category tree
4. Saves the item to Postgres (with a vector embedding) and mirrors it to WiseMapping
5. Replies with the saved path: `Saved to AI & ML > Claude`

## Setup

### 1. Clone and install

```bash
pip install -r requirements.txt
```

### 2. Create databases

```bash
createdb twig
createdb twig_test   # for tests only
```

### 3. Configure environment

Copy `.env.example` to `.env` and fill in:

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | Your Telegram user ID (bot only responds to you) |
| `WISEMAPPING_EMAIL` | WiseMapping account email |
| `WISEMAPPING_PASSWORD` | WiseMapping account password |
| `WISEMAPPING_MAP_ID` | ID of the map to write to (from the URL) |
| `WISEMAPPING_BASE_URL` | `https://api.wisemapping.com` |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `OPENROUTER_MODEL` | e.g. `openai/gpt-4o-mini` |
| `EXA_API_KEY` | Exa API key (optional, improves content extraction) |
| `OPENAI_API_KEY` | OpenAI API key — used for embeddings (`text-embedding-3-small`) |
| `DATABASE_URL` | Postgres connection string for production |
| `TEST_DATABASE_URL` | Postgres connection string for tests |

### 4. Migrate existing WiseMapping data (first run only)

```bash
python migrate_wisemapping_to_db.py           # import all nodes from WiseMapping
python migrate_wisemapping_to_db.py --dry-run # preview without writing
```

### 5. Run

```bash
python bot.py
```

## Deploy to Railway

The repo includes `railway.toml`. Push to a Railway project — it will run `python bot.py` as a worker (no HTTP server needed).

## Commands

| Command | Description |
|---|---|
| `/search <query>` | Semantic + fuzzy search across saved items |
| `/replace` | Relocate the last saved item to a different branch |
| `/addcategory Parent > Child` | Add a new category to the tree |
| `/synctree` | Re-sync the category tree from WiseMapping into Postgres |
| `/reindex` | Regenerate embeddings for all items |
| `/debug` | List all branches currently in the category tree |
| `/testnote <url>` | Test content extraction for a URL without saving |
| `/testmedium <url>` | Test Medium/paywalled extraction via Playwright |
| `/showxml` | Show raw XML of the last 2 nodes in the map |

## Enriching notes (`update_notes.py`)

A local maintenance script that backfills or refreshes notes on nodes whose content was blocked or empty when originally saved.

```bash
python update_notes.py            # refresh blocked/empty notes only
python update_notes.py --dry-run  # preview what would change, no saves
python update_notes.py --all      # refresh every node that has a URL
```

Requires `medium_cookies.json` in the project root for paywalled sites.

## Tests

```bash
pytest
```

## Project structure

```
bot.py                        Telegram handler, duplicate guard, force-save, /replace
migrate_wisemapping_to_db.py  One-shot import of WiseMapping nodes into Postgres
update_notes.py               Backfill/refresh blocked or empty notes on existing nodes
core/
  db.py           Postgres schema, pool, save/search/state helpers
  extractor.py    Input detection, HTTP fetch, yt-dlp, Playwright, Exa fallback
  wisemapping.py  WiseMapping REST API, XML read/write, node move
  ai.py           OpenRouter: placement, relocation, summarization, embeddings
  search.py       RRF merge of semantic + fuzzy search results
```

## Notes

- Only one bot instance can run at a time (socket lock on port 47832)
- Duplicate messages are detected before any expensive API calls; reply `force` to bypass
- WiseMapping notes appear as a small icon on each node — click to expand
- `/replace` state is persisted to Postgres (`bot_state` table) and survives restarts
- Passwords containing special characters (e.g. `@`) must be percent-encoded in connection URLs
