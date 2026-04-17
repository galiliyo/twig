"""
Semantic search over WiseMapping mind-map nodes.

Index lifecycle:
- search_index.json is built lazily on the first /search after a save.
- It is invalidated (deleted) whenever a node is added or moved.
- /reindex forces a rebuild at any time.
"""

import json
import logging
import math
import os
import xml.etree.ElementTree as ET

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

from .ai import embed_texts, embed_query

_log = logging.getLogger(__name__)

INDEX_PATH = os.environ.get("SEARCH_INDEX_PATH", "search_index.json")

# Max chars of note text fed to the embedder per node
_EMBED_NOTE_CHARS = 800
# Max chars of note snippet stored in the index (for display)
_SNIPPET_CHARS = 300


def _cosine(a: list[float], b: list[float]) -> float:
    if _HAS_NUMPY:
        av, bv = np.array(a), np.array(b)
        denom = np.linalg.norm(av) * np.linalg.norm(bv)
        return float(np.dot(av, bv) / denom) if denom else 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    return dot / (mag_a * mag_b) if (mag_a and mag_b) else 0.0


def _build_parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    """Map each element to its parent so we can walk upward to build paths."""
    parents: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[child] = parent
    return parents


def _path_to(node: ET.Element, parents: dict) -> list[str]:
    """Walk up the tree to build a branch path list (excluding the node itself)."""
    path = []
    current = parents.get(node)
    while current is not None:
        text = current.get("text")
        if text and not current.get("central"):
            path.append(text)
        current = parents.get(current)
    path.reverse()
    return path


def extract_leaf_nodes(xml_text: str) -> list[dict]:
    """Parse WiseMapping XML and return one dict per node that has a URL link."""
    root = ET.fromstring(xml_text)
    parents = _build_parent_map(root)
    nodes = []
    for topic in root.iter("topic"):
        link = topic.find("link")
        if link is None:
            continue
        url = link.get("url", "")
        if not url:
            continue
        title = topic.get("text", "")
        note_el = topic.find("note")
        note_text = note_el.get("text", "") if note_el is not None else ""
        branch_path = _path_to(topic, parents)
        path_str = " > ".join(branch_path) if branch_path else ""
        nodes.append({
            "id": topic.get("id", ""),
            "title": title,
            "path": path_str,
            "url": url,
            "note_snippet": note_text[:_SNIPPET_CHARS],
            # Text fed to the embedder: path + title + note content
            "_embed_text": f"{path_str}: {title}. {note_text[:_EMBED_NOTE_CHARS]}".strip(),
        })
    return nodes


async def build_index(wm) -> list[dict]:
    """Fetch the live map, embed all nodes, write search_index.json. Returns entries."""
    from .wisemapping import WiseMapping  # avoid circular at module level
    xml_text = await wm._fetch_xml()
    nodes = extract_leaf_nodes(xml_text)
    if not nodes:
        _log.warning("search: no URL nodes found in map")
        return []

    texts = [n["_embed_text"] for n in nodes]
    _log.info("search: embedding %d nodes", len(nodes))
    embeddings = await embed_texts(texts)

    entries = []
    for node, emb in zip(nodes, embeddings):
        entry = {k: v for k, v in node.items() if k != "_embed_text"}
        entry["embedding"] = emb
        entries.append(entry)

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    _log.info("search: index written to %s (%d entries)", INDEX_PATH, len(entries))
    return entries


def load_index() -> list[dict] | None:
    """Load search_index.json if it exists. Returns None if missing."""
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def invalidate_index() -> None:
    """Delete the index file so the next /search triggers a rebuild."""
    try:
        os.remove(INDEX_PATH)
        _log.info("search: index invalidated")
    except FileNotFoundError:
        pass


async def search(query: str, wm, top_k: int = 5) -> list[dict]:
    """
    Semantic search over the mind map.
    Loads (or builds) the index, embeds the query, returns top_k results
    sorted by cosine similarity, each with title/path/url/note_snippet/score.
    """
    entries = load_index()
    if entries is None:
        _log.info("search: no index found, building now")
        entries = await build_index(wm)

    if not entries:
        return []

    q_emb = await embed_query(query)

    scored = [
        {**{k: v for k, v in e.items() if k != "embedding"},
         "score": _cosine(q_emb, e["embedding"])}
        for e in entries
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
