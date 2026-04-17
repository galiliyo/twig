# Twig

A personal Telegram bot that captures links, ideas, and reminders directly into a WiseMapping mind map using AI for placement.

## What it does

Send any message to the bot and it:

1. Detects the input type (URL, YouTube link, idea, reminder)
2. Fetches article metadata via direct HTTP or Exa (for Cloudflare-blocked sites)
3. Reads your WiseMapping mind map
4. Uses an AI model (via OpenRouter) to choose the best branch
5. Saves the item as a new node — with the original URL as a link and article text as a note
6. Replies with the saved path: `✓ Saved to Front-End > Angular`

## Setup

### 1. Clone and install

```bash
pip install -r requirements.txt
```

### 2. Configure environment

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
| `OPENAI_API_KEY` | OpenAI API key — used for `/search` embeddings (`text-embedding-3-small`) |
| `SEARCH_INDEX_PATH` | Override default index file path (optional, default: `search_index.json`) |

### 3. Run

```bash
python bot.py
```

## Deploy to Railway

The repo includes `railway.toml`. Push to a Railway project — it will run `python bot.py` as a worker (no HTTP server needed).

## Commands

| Command | Description |
|---|---|
| `/debug` | List all branches currently in the map |
| `/replace` | Relocate the last saved item to a different branch |
| `/testnote <url>` | Test content extraction for a URL without saving |
| `/showxml` | Show raw XML of the last 2 nodes in the map |

## Enriching notes (`update_notes.py`)

A local maintenance script that backfills or refreshes notes on nodes whose content was blocked or empty when originally saved (e.g. Cloudflare-challenged pages).

Run it from your machine — it uses your local IP and `medium_cookies.json` to fetch full article content, summarizes via OpenRouter, and writes the result back to WiseMapping.

```bash
python update_notes.py            # refresh blocked/empty notes only
python update_notes.py --dry-run  # preview what would change, no saves
python update_notes.py --all      # refresh every node that has a URL
```

**What it does per node:**

1. Walks all topics in the map XML and collects those with a URL link
2. Flags notes that contain bot-challenge markers (e.g. "Just a moment", "Attention Required") or are empty
3. Fetches the full page via Playwright (same stealth approach as the bot)
4. Summarizes with `summarize_bullets` and writes the result back as a `<note text="…"/>` attribute
5. Saves the updated XML to WiseMapping in one batch at the end

Requires `medium_cookies.json` in the project root for paywalled sites.

## Project structure

```
bot.py              — Telegram handler, duplicate guard, force-save, /replace
update_notes.py     — Local script to backfill/refresh blocked or empty notes
core/
  extractor.py      — Input detection, HTTP fetch, yt-dlp for YouTube, Exa fallback
  wisemapping.py    — WiseMapping REST API, XML read/write, node move
  ai.py             — OpenRouter: placement, relocation, summarization
```

## Notes

- Only one bot instance can run at a time (socket lock on port 47832)
- Duplicate messages are detected before any expensive API calls; reply `force` to a duplicate warning to save anyway
- WiseMapping notes appear as a small icon on each node — click to expand
- Exa content for paywalled articles (Medium etc.) is limited to the public preview
- `/replace` state (`_last_saved`) is in-memory and lost on bot restart
