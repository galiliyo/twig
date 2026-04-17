# Migrate Mind Map to DB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `migrate_to_db.py` — a one-shot script that reads all saved article nodes from the WiseMapping XML and inserts them into Postgres with embeddings and AI-generated tags.

**Architecture:** Pure XML extraction function → per-node tag generation via OpenRouter → batched OpenAI embeddings → idempotent bulk insert via existing `save_item()`. No changes to existing modules.

**Tech Stack:** Python asyncio, `xml.etree.ElementTree`, `httpx`, `asyncpg` (via `core/db.py`), OpenAI embeddings (via `core/ai.py`), OpenRouter for tags.

---

### Task 1: XML leaf extraction (pure function + tests)

**Files:**
- Create: `migrate_to_db.py`
- Create: `tests/test_migrate_to_db.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate_to_db.py`:

```python
import xml.etree.ElementTree as ET
import pytest
from migrate_to_db import extract_leaves


def _xml(body: str) -> ET.Element:
    return ET.fromstring(f"<map>{body}</map>")


def test_extract_single_leaf_with_url_and_note():
    root = _xml("""
        <topic id="1" text="Root" central="true">
          <topic id="2" text="AI &amp; ML">
            <topic id="3" text="Models">
              <topic id="4" text="GPT-4 article">
                <link url="https://example.com/gpt4" type="url"/>
                <note text="Summary of GPT-4."/>
              </topic>
            </topic>
          </topic>
        </topic>
    """)
    leaves = extract_leaves(root)
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf["title"] == "GPT-4 article"
    assert leaf["url"] == "https://example.com/gpt4"
    assert leaf["note"] == "Summary of GPT-4."
    assert leaf["branch_path"] == ["AI & ML", "Models"]
    assert leaf["text_input"] == "https://example.com/gpt4"


def test_extract_leaf_without_url_uses_title_as_text_input():
    root = _xml("""
        <topic id="1" text="Root" central="true">
          <topic id="2" text="Ideas">
            <topic id="3" text="My plain note">
              <link url="" type="url"/>
            </topic>
          </topic>
        </topic>
    """)
    leaves = extract_leaves(root)
    assert len(leaves) == 1
    assert leaves[0]["text_input"] == "My plain note"
    assert leaves[0]["url"] is None


def test_category_nodes_excluded():
    root = _xml("""
        <topic id="1" text="Root" central="true">
          <topic id="2" text="AI &amp; ML">
            <topic id="3" text="Models"/>
          </topic>
        </topic>
    """)
    leaves = extract_leaves(root)
    assert leaves == []


def test_invalid_node_skipped():
    root = _xml("""
        <topic id="1" text="Root" central="true">
          <topic id="2" text="">
            <link url="" type="url"/>
          </topic>
        </topic>
    """)
    leaves = extract_leaves(root)
    assert leaves == []


def test_multiple_leaves_different_branches():
    root = _xml("""
        <topic id="1" text="Root" central="true">
          <topic id="2" text="Tech">
            <topic id="3" text="Article A">
              <link url="https://a.com" type="url"/>
            </topic>
          </topic>
          <topic id="4" text="Science">
            <topic id="5" text="Article B">
              <link url="https://b.com" type="url"/>
            </topic>
          </topic>
        </topic>
    """)
    leaves = extract_leaves(root)
    assert len(leaves) == 2
    titles = {l["title"] for l in leaves}
    assert titles == {"Article A", "Article B"}
    paths = {l["title"]: l["branch_path"] for l in leaves}
    assert paths["Article A"] == ["Tech"]
    assert paths["Article B"] == ["Science"]


def test_no_note_returns_none():
    root = _xml("""
        <topic id="1" text="Root" central="true">
          <topic id="2" text="Tech">
            <topic id="3" text="Article">
              <link url="https://x.com" type="url"/>
            </topic>
          </topic>
        </topic>
    """)
    leaves = extract_leaves(root)
    assert leaves[0]["note"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:/Users/yonga/Programming/twig
pytest tests/test_migrate_to_db.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `migrate_to_db` doesn't exist yet.

- [ ] **Step 3: Implement `extract_leaves` in `migrate_to_db.py`**

Create `migrate_to_db.py`:

```python
"""
One-shot migration: read all saved article nodes from the WiseMapping XML
and insert them into the Postgres items table with embeddings and tags.

Usage:
    python migrate_to_db.py            # full migration
    python migrate_to_db.py --dry-run  # preview only, no DB writes
"""

import asyncio
import json
import logging
import os
import sys
import xml.etree.ElementTree as ET
from collections import deque

import httpx
from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_TAG_SYSTEM = """\
Given a title and optional content summary, return a JSON array of 2-4 lowercase
single-word tags that best describe the topic.
Example: ["python", "performance", "async", "database"]
Return ONLY the JSON array, no prose.
"""


def extract_leaves(root: ET.Element) -> list[dict]:
    """Return all leaf nodes (topics with a <link> child) with their extracted fields."""
    central = root.find(".//topic[@central='true']") or root.find(".//topic")
    if central is None:
        return []

    leaves = []
    queue: deque[tuple[ET.Element, list[str]]] = deque()
    for child in central:
        if child.tag == "topic":
            queue.append((child, [child.get("text", "")]))

    while queue:
        node, path = queue.popleft()
        link = node.find("link")
        title = node.get("text", "")

        if link is not None:
            url = link.get("url") or None
            note_el = node.find("note")
            note = note_el.get("text") if note_el is not None else None
            text_input = url if url else title
            if not title and not url:
                _log.warning("Skipping invalid node (no title, no url) at path %s", path)
                continue
            leaves.append({
                "title": title,
                "url": url,
                "note": note,
                "branch_path": path[:-1],
                "text_input": text_input,
            })
        else:
            for child in node:
                if child.tag == "topic":
                    queue.append((child, path + [child.get("text", "")]))

    return leaves
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_migrate_to_db.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add migrate_to_db.py tests/test_migrate_to_db.py
git commit -m "feat: add extract_leaves and tests for migrate_to_db"
```

---

### Task 2: Tag generation helper + tests

**Files:**
- Modify: `migrate_to_db.py` — add `_generate_tags()`
- Modify: `tests/test_migrate_to_db.py` — add tag generation tests

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrate_to_db.py`:

```python
import unittest.mock as mock
from migrate_to_db import _generate_tags


def _mock_client(status: int, content: str):
    """Return a mock httpx.AsyncClient context manager with a preset POST response."""
    fake_resp = mock.MagicMock()
    fake_resp.status_code = status
    fake_resp.text = content
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    mock_instance = mock.AsyncMock()
    mock_instance.post = mock.AsyncMock(return_value=fake_resp)
    mock_cm = mock.MagicMock()
    mock_cm.__aenter__ = mock.AsyncMock(return_value=mock_instance)
    mock_cm.__aexit__ = mock.AsyncMock(return_value=False)
    return mock_cm


async def test_generate_tags_returns_list_from_api():
    cm = _mock_client(200, '["python", "async", "database"]')
    with mock.patch("migrate_to_db.httpx.AsyncClient", return_value=cm):
        tags = await _generate_tags("Python async DB patterns", "Article about asyncpg and connection pooling.")
    assert tags == ["python", "async", "database"]


async def test_generate_tags_returns_empty_list_on_api_failure():
    cm = _mock_client(500, "Internal Server Error")
    with mock.patch("migrate_to_db.httpx.AsyncClient", return_value=cm):
        tags = await _generate_tags("Some Title", None)
    assert tags == []


async def test_generate_tags_handles_markdown_fenced_json():
    cm = _mock_client(200, '```json\n["ml", "models"]\n```')
    with mock.patch("migrate_to_db.httpx.AsyncClient", return_value=cm):
        tags = await _generate_tags("ML Models Overview", None)
    assert tags == ["ml", "models"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_migrate_to_db.py::test_generate_tags_returns_list_from_api -v
```

Expected: `ImportError` — `_generate_tags` not defined yet.

- [ ] **Step 3: Implement `_generate_tags` in `migrate_to_db.py`**

Add after `extract_leaves` in `migrate_to_db.py`:

```python
async def _generate_tags(title: str, note: str | None) -> list[str]:
    """Call OpenRouter to generate 2-4 topic tags for a node."""
    snippet = (note or "")[:400].replace("\n", " ").strip()
    user_msg = f"Title: {title}" + (f"\n\nContent: {snippet}" if snippet else "")

    payload = {
        "model": os.environ["OPENROUTER_MODEL"],
        "messages": [
            {"role": "system", "content": _TAG_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                _OPENROUTER_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                    "HTTP-Referer": "https://github.com/galiliyo/twig",
                },
            )
        if resp.status_code != 200:
            _log.warning("Tag generation HTTP %s for %r — using []", resp.status_code, title)
            return []
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content.strip())
    except Exception as exc:
        _log.warning("Tag generation failed for %r: %s — using []", title, exc)
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_migrate_to_db.py -v
```

Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add migrate_to_db.py tests/test_migrate_to_db.py
git commit -m "feat: add _generate_tags to migrate_to_db"
```

---

### Task 3: Main migration loop

**Files:**
- Modify: `migrate_to_db.py` — add `main()` and embed-text helper

- [ ] **Step 1: Write the failing test for embed text construction**

Append to `tests/test_migrate_to_db.py`:

```python
from migrate_to_db import _embed_text


def test_embed_text_with_note():
    result = _embed_text("My Article", "This is a long note about something important.")
    assert result == "My Article. This is a long note about something important."


def test_embed_text_without_note():
    result = _embed_text("My Article", None)
    assert result == "My Article"


def test_embed_text_truncates_long_note():
    long_note = "x" * 1000
    result = _embed_text("Title", long_note)
    assert result == "Title. " + "x" * 800
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_migrate_to_db.py::test_embed_text_with_note -v
```

Expected: `ImportError` — `_embed_text` not defined.

- [ ] **Step 3: Add `_embed_text` and `main()` to `migrate_to_db.py`**

Add `_embed_text` after `_generate_tags`:

```python
def _embed_text(title: str, note: str | None) -> str:
    if note:
        return f"{title}. {note[:800]}"
    return title
```

Then add `main()` at the bottom of `migrate_to_db.py`:

```python
async def main() -> None:
    dry_run = "--dry-run" in sys.argv

    from core.wisemapping import WiseMapping
    from core.db import init_db, save_item, _pool
    from core.ai import embed_texts

    await init_db()
    wm = WiseMapping()
    await wm.login()

    xml_text = await wm._fetch_xml()
    root = ET.fromstring(xml_text)
    leaves = extract_leaves(root)
    await wm.aclose()

    _log.info("Found %d leaf nodes in mind map", len(leaves))

    # Pre-flight duplicate check
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT url FROM items WHERE url IS NOT NULL")
    existing_urls: set[str] = {r["url"] for r in rows}

    to_migrate = []
    skipped_dup = 0
    for leaf in leaves:
        if leaf["url"] and leaf["url"] in existing_urls:
            _log.info("  [skip duplicate] %s", leaf["title"])
            skipped_dup += 1
        else:
            to_migrate.append(leaf)

    _log.info("Skipping %d already in DB, migrating %d", skipped_dup, len(to_migrate))

    if not to_migrate:
        _log.info("Nothing to migrate.")
        return

    if dry_run:
        _log.info("[DRY-RUN] Would migrate %d nodes:", len(to_migrate))
        for leaf in to_migrate:
            _log.info("  %s | %s", " > ".join(leaf["branch_path"] + [leaf["title"]]), leaf["url"] or "")
        return

    # Generate tags per node
    _log.info("Generating tags...")
    for leaf in to_migrate:
        leaf["tags"] = await _generate_tags(leaf["title"], leaf["note"])

    # Batch embed
    _log.info("Generating embeddings...")
    embed_inputs = [_embed_text(l["title"], l["note"]) for l in to_migrate]
    embeddings = await embed_texts(embed_inputs)

    # Insert
    inserted = 0
    failed = 0
    for leaf, embedding in zip(to_migrate, embeddings):
        try:
            await save_item(
                text_input=leaf["text_input"],
                title=leaf["title"],
                url=leaf["url"],
                branch_path=leaf["branch_path"],
                tags=leaf["tags"],
                note=leaf["note"],
                embedding=embedding,
            )
            inserted += 1
            _log.info("  [✓] %s", leaf["title"])
        except Exception as exc:
            _log.error("  [✗] %s — %s", leaf["title"], exc)
            failed += 1

    _log.info(
        "\nDone — migrated %d nodes (%d duplicates skipped, %d failed)",
        inserted, skipped_dup, failed,
    )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run all tests to verify they pass**

```bash
pytest tests/test_migrate_to_db.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add migrate_to_db.py tests/test_migrate_to_db.py
git commit -m "feat: add main migration loop to migrate_to_db"
```

---

### Task 4: Smoke test with `--dry-run`

**Files:**
- No file changes — this is a manual validation step

- [ ] **Step 1: Run with `--dry-run`**

```bash
python migrate_to_db.py --dry-run
```

Expected output (example):
```
Found 47 leaf nodes in mind map
Skipping 0 already in DB, migrating 47
[DRY-RUN] Would migrate 47 nodes:
  AI & ML > Models > GPT-4 article | https://example.com/gpt4
  ...
```

Verify: node titles look correct, branch paths are sensible (not empty, not including the leaf title), URLs are present for article nodes.

- [ ] **Step 2: If anything looks wrong, fix `extract_leaves` and re-run tests**

Common issues to watch for:
- `branch_path` includes the leaf title (off-by-one in `path[:-1]`)
- Central node text appearing in branch_path (should never happen — BFS starts from central's children)
- Category nodes appearing as leaves (they have no `<link>` child — should be excluded)

- [ ] **Step 3: Run the real migration**

```bash
python migrate_to_db.py
```

Expected final line:
```
Done — migrated N nodes (0 duplicates skipped, 0 failed)
```

- [ ] **Step 4: Verify rows in DB**

Connect to Postgres and run:
```sql
SELECT id, title, url, branch_path, tags, array_length(embedding::text::float[], 1)
FROM items
ORDER BY id
LIMIT 10;
```

Check: `tags` is non-empty array, `branch_path` matches expected hierarchy, embedding column is populated (not NULL).

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: migrate_to_db.py complete and validated"
```
