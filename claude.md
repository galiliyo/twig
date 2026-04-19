# Twig — Claude context

## Run

```bash
python bot.py
```

## Architecture

```
bot.py                        Telegram polling, duplicate guard, force-save flow, /replace command
migrate_wisemapping_to_db.py  One-shot import of WiseMapping nodes into Postgres
core/db.py          Postgres schema, pool, save/search/state helpers
core/extractor.py   Input type detection, HTTP fetch, yt-dlp for YouTube, Exa fallback
core/wisemapping.py WiseMapping JWT auth, XML read/write, node placement + move
core/ai.py          OpenRouter call — placement, relocation, summarization, embeddings
core/search.py      RRF merge of semantic + fuzzy search results
```

## Gotchas

- **WiseMapping auth**: JWT Bearer via `POST /api/restful/authenticate` — not Basic Auth
- **WiseMapping PUT**: `Content-Type: text/plain` required — `application/xml` returns 500
- **WiseMapping XML**: every `<topic>` needs `position="x,y"` and `order="n"` or the map renders blank
- **WiseMapping API path**: `/api/restful/` (one L) — double-L gives 404
- **Single-instance lock**: socket on port 47832 — kill old process before restarting
- **Force-save**: user replies `force` to the duplicate warning message to bypass duplicate check
- **Exa + paywalls**: Medium and similar return preview only (~150 words); full content for non-paywalled sites
- **WiseMapping notes**: rendered as a small icon on the node — must click to read, not shown inline
- **WiseMapping note XML format**: `<note text="content"/>` as attribute — NOT `<note>content</note>` as element text (WiseMapping UI only reads the `text` attr)
- **Medium/paywalled extraction**: httpx 403 → escalate to Playwright with saved cookies (`medium_cookies.json`) + stealth init script to bypass Cloudflare bot detection
- **YouTube extraction**: yt-dlp extracts title + description (channel info included); falls back to `_fetch_page` on failure
- **/replace flow**: reply-based — bot shows numbered top-level branches, user replies with number, bot AI picks 2nd-level leaf and moves the node
- **`_last_saved` state**: persisted to `bot_state` table in Postgres — survives restarts
- **Search threshold**: semantic cosine distance cutoff is 0.92 (`db.py`) — was 0.75 but that was too strict for conceptually-related queries on a small dataset; tighten if the DB grows large and results get noisy
- **`_pool` import trap**: `core/search.py` imports `core.db` as a module (`import core.db as _db`) — NOT `from core.db import _pool`. The `from ... import` form captures `None` at load time before `init_db()` runs
- **`/searchdebug <query>`**: debug command that shows raw semantic/fuzzy/LIKE hit counts and top distances — use to diagnose why a result is missing
