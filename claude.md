# Twig — Claude context

## Run

```bash
python bot.py
```

## Architecture

```
bot.py              Telegram polling, duplicate guard, force-save flow
core/extractor.py   Input type detection, HTTP fetch, Exa fallback for blocked sites
core/wisemapping.py WiseMapping JWT auth, XML read/write, node placement
core/ai.py          OpenRouter call — returns branch_path + title for new node
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
