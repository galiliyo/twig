# Migrate WiseMapping Mind Map to Postgres DB

**Date:** 2026-04-18  
**Status:** Approved

## Goal

One-shot migration script (`migrate_to_db.py`) that reads all saved articles (leaf nodes with a URL) from the WiseMapping mind map XML and inserts them into the Postgres `items` table with full embeddings and AI-generated tags.

## Scope

- Source: WiseMapping XML fetched via the existing `WiseMapping._fetch_xml()` method
- Target: Postgres `items` table (schema in `core/db.py`)
- One-time script, idempotent on re-run (URL-based duplicate skip)

## Data Extraction

Walk the XML tree from the central node using BFS. Target **leaf nodes** — `<topic>` elements that have a `<link url="..."/>` child (saved articles, not category branches).

For each leaf, extract:

| DB field | Source |
|---|---|
| `title` | `topic.get("text")` |
| `url` | `link.get("url")` |
| `note` | `note.get("text")` if `<note>` child exists, else `None` |
| `branch_path` | list of ancestor `text` labels from central node down to (not including) the leaf |
| `text_input` | `url` if present, else `title` |
| `tags` | AI-generated (see below) |
| `embedding` | OpenAI batch embed (see below) |

**Validation — skip with warning if:**
- `title` is empty AND `url` is absent (malformed node)
- URL already exists in DB (duplicate, counted separately)

## Tag Generation

For each node: one OpenRouter call with the configured model, prompt:
> *"Given this title and content summary, return a JSON array of 2–4 lowercase single-word tags."*

Input: `title` + first 400 chars of `note` (or just `title` if no note).  
On failure: store `[]`, log warning, continue.  
Sequential per-node (not batched) to keep prompts simple.

## Embeddings

Embed text per node: `f"{title}. {note[:800]}"` (or `title` alone if no note) — matches the `update_notes.py` pattern exactly.

All embed texts sent in **one batched call** to `embed_texts()` after all nodes are collected.

## Duplicate Check

Pre-flight: `SELECT url FROM items WHERE url IS NOT NULL` → load into a Python `set`.  
Any node whose URL is in the set is skipped (logged, counted as duplicate).

## Insert

One-by-one via `save_item()` from `core/db.py`. No bulk insert needed (one-time, at most hundreds of rows).

## Output

```
Found N leaf nodes in mind map
Skipping K already in DB
Generating tags for N-K nodes...
Embedding N-K nodes...
Inserting...
Done — migrated N-K nodes (K duplicates skipped, L invalid skipped)
```

## Error Handling

| Failure | Behavior |
|---|---|
| WiseMapping fetch fails | Abort with error |
| DB connection fails | Abort with error |
| Tag generation fails for a node | `tags=[]`, warn, continue |
| Embedding batch fails | Abort (all-or-nothing for embeddings) |
| DB insert fails for a node | Log error, continue |

## Files

- **New:** `migrate_to_db.py` — standalone script, same pattern as `update_notes.py` and `bootstrap_tree.py`
- **No changes** to existing modules

## Usage

```bash
python migrate_to_db.py            # full migration
python migrate_to_db.py --dry-run  # preview only, no DB writes
```
