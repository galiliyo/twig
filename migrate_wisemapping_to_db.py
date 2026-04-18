"""
One-shot migration: import all saved articles from WiseMapping into Postgres.

Usage:
    python migrate_wisemapping_to_db.py [--dry-run]

Walks the WiseMapping XML, finds every leaf node (topic with a <link> child),
reconstructs its branch_path from ancestor topics, and inserts it into the
`items` table with a fresh embedding.  Skips rows whose url already exists in
the DB to make the script safe to re-run.
"""

import asyncio
import os
import sys
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

DRY_RUN = "--dry-run" in sys.argv


@dataclass
class LeafNode:
    title: str
    url: str | None
    note: str | None
    branch_path: list[str]


def _extract_tree(xml_text: str) -> tuple[list[list[str]], list[LeafNode]]:
    """Returns (category_paths, leaf_nodes) from the XML."""
    root = ET.fromstring(xml_text)
    central = root.find(".//topic[@central='true']") or root.find(".//topic")
    if central is None:
        return [], []

    categories: list[list[str]] = []
    leaves: list[LeafNode] = []
    queue: deque[tuple[ET.Element, list[str]]] = deque()
    for child in central:
        if child.tag == "topic":
            queue.append((child, []))

    while queue:
        node, ancestors = queue.popleft()
        text = node.get("text", "").strip()
        link_el = node.find("link")
        note_el = node.find("note")

        if link_el is not None:
            leaves.append(LeafNode(
                title=text,
                url=link_el.get("url"),
                note=note_el.get("text") if note_el is not None else None,
                branch_path=ancestors,
            ))
        else:
            categories.append(ancestors + [text])
            for child in node:
                if child.tag == "topic":
                    queue.append((child, ancestors + [text]))

    return categories, leaves


async def main() -> None:
    from core.db import init_db, save_item, add_category
    from core.ai import embed_texts
    from core.wisemapping import WiseMapping

    print("Connecting to DB…")
    await init_db()

    print("Fetching WiseMapping XML…")
    wm = WiseMapping()
    xml_text = await wm._fetch_xml()
    await wm.aclose()

    categories, leaves = _extract_tree(xml_text)
    print(f"Found {len(categories)} categories, {len(leaves)} notes")

    if DRY_RUN:
        print("\nCategories:")
        for path in categories:
            print(f"  {' > '.join(path)}")
        print("\nNotes (structure only, no content imported):")
        for leaf in leaves:
            print(f"  {' > '.join(leaf.branch_path)} > {leaf.title}")
        return

    # Import categories (idempotent)
    print("Importing categories…")
    for path in categories:
        await add_category(path)
        print(f"  ✓ {' > '.join(path)}")

    if not leaves:
        print("\nDone. No notes to import.")
        return

    # Check which URLs already exist so re-runs are safe
    import asyncpg
    check_conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=False)
    rows = await check_conn.fetch("SELECT url FROM items WHERE url IS NOT NULL")
    existing_urls = {r["url"] for r in rows}
    await check_conn.close()

    to_insert = [leaf for leaf in leaves if leaf.url not in existing_urls]
    print(f"\nNotes: {len(leaves) - len(to_insert)} already imported, {len(to_insert)} new")

    if to_insert:
        print("Generating embeddings…")
        embed_inputs = [
            f"{' > '.join(leaf.branch_path)}: {leaf.title}".strip()
            for leaf in to_insert
        ]
        embeddings = await embed_texts(embed_inputs)

        print("Importing notes…")
        for leaf, embedding in zip(to_insert, embeddings):
            await save_item(
                text_input=leaf.url or leaf.title,
                title=leaf.title,
                url=leaf.url,
                branch_path=leaf.branch_path,
                tags=[],
                note=None,
                embedding=embedding,
            )
            print(f"  ✓ {' > '.join(leaf.branch_path)} > {leaf.title}")

    print(f"\nDone. {len(categories)} categories + {len(to_insert)} notes imported.")


if __name__ == "__main__":
    asyncio.run(main())
