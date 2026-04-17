"""
WiseMapping REST API integration.

Endpoints used:
  POST /api/restful/authenticate           → returns JWT token
  GET  /api/restful/maps/{id}/document/xml → returns XML
  PUT  /api/restful/maps/{id}/document/xml → saves XML
Authentication: JWT Bearer token obtained at startup; re-fetched on 401.
"""

import os
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field

import httpx


@dataclass
class Placement:
    branch_path: list[str]   # e.g. ["Front-End", "Angular"]
    new_branch: str | None   # intermediate branch to create, if any
    title: str               # short leaf node title
    url: str | None = None   # original URL, attached as a clickable link
    note: str | None = None  # extracted article content, attached as a note
    tags: list[str] = field(default_factory=list)  # AI-generated topic tags


class WiseMappingError(Exception):
    pass


class WiseMapping:
    def __init__(self) -> None:
        self._base = os.environ["WISEMAPPING_BASE_URL"].rstrip("/")
        self._map_id = os.environ["WISEMAPPING_MAP_ID"]
        self._email = os.environ["WISEMAPPING_EMAIL"]
        self._password = os.environ["WISEMAPPING_PASSWORD"]
        self._token: str | None = None
        self._client = httpx.AsyncClient(timeout=15)

    async def get_branches(self) -> list[str]:
        """Return all branch paths as 'Parent > Child' strings (excludes root)."""
        xml_text = await self._fetch_xml()
        root = ET.fromstring(xml_text)
        return _flatten_branches(root)

    async def add_node(self, placement: Placement) -> str:
        """
        Insert a leaf node at the given placement, optionally creating one
        intermediate branch. Returns the final display path string.
        """
        xml_text = await self._fetch_xml()
        root = ET.fromstring(xml_text)

        target = _find_or_create_path(root, placement.branch_path)

        if placement.new_branch:
            mid = ET.SubElement(target, "topic")
            mid.set("id", str(_next_id(root)))
            mid.set("text", placement.new_branch)
            target = mid

        leaf = ET.SubElement(target, "topic")
        leaf.set("id", str(_next_id(root)))
        leaf.set("text", placement.title)

        if placement.url:
            link = ET.SubElement(leaf, "link")
            link.set("url", placement.url)
            link.set("type", "url")

        if placement.note:
            note_el = ET.SubElement(leaf, "note")
            note_el.set("text", placement.note)

        _assign_positions(root)
        await self._save_xml(ET.tostring(root, encoding="unicode", xml_declaration=False))

        parts = list(placement.branch_path)
        if placement.new_branch:
            parts.append(placement.new_branch)
        parts.append(placement.title)
        return " > ".join(parts)

    async def get_top_level_branches(self) -> list[str]:
        """Return the names of all direct children of the central node (top-level categories)."""
        xml_text = await self._fetch_xml()
        root = ET.fromstring(xml_text)
        central = root.find(".//topic[@central='true']") or root.find(".//topic")
        if central is None:
            return []
        return [c.get("text", "") for c in central if c.tag == "topic"]

    async def get_sub_branches(self, top_level: str) -> list[str]:
        """Return 'TopLevel > Child' paths for all category children under a top-level branch."""
        xml_text = await self._fetch_xml()
        root = ET.fromstring(xml_text)
        central = root.find(".//topic[@central='true']") or root.find(".//topic")
        if central is None:
            return []
        parent = next((c for c in central if c.tag == "topic" and c.get("text") == top_level), None)
        if parent is None:
            return []
        paths = []
        for child in parent:
            if child.tag == "topic" and child.find("link") is None:
                paths.append(f"{top_level} > {child.get('text', '')}")
        return paths

    async def move_node(self, old_path: list[str], old_title: str, new_placement: 'Placement') -> str:
        """Remove a leaf node at old_path/old_title and add it at new_placement. Returns new display path."""
        xml_text = await self._fetch_xml()
        root = ET.fromstring(xml_text)

        # Find and detach the old node
        old_parent = _find_topic(root, old_path)
        if old_parent is not None:
            for child in list(old_parent):
                if child.tag == "topic" and child.get("text") == old_title:
                    old_parent.remove(child)
                    # Preserve URL and note from old node if not set on new placement
                    if new_placement.url is None:
                        link_el = child.find("link")
                        if link_el is not None:
                            new_placement.url = link_el.get("url")
                    if new_placement.note is None:
                        note_el = child.find("note")
                        if note_el is not None:
                            new_placement.note = note_el.get("text")
                    break

        # Add to new location
        target = _find_or_create_path(root, new_placement.branch_path)

        if new_placement.new_branch:
            mid = ET.SubElement(target, "topic")
            mid.set("id", str(_next_id(root)))
            mid.set("text", new_placement.new_branch)
            target = mid

        leaf = ET.SubElement(target, "topic")
        leaf.set("id", str(_next_id(root)))
        leaf.set("text", new_placement.title)

        if new_placement.url:
            link = ET.SubElement(leaf, "link")
            link.set("url", new_placement.url)
            link.set("type", "url")

        if new_placement.note:
            note_el = ET.SubElement(leaf, "note")
            note_el.set("text", new_placement.note)

        _assign_positions(root)
        await self._save_xml(ET.tostring(root, encoding="unicode", xml_declaration=False))

        parts = list(new_placement.branch_path)
        if new_placement.new_branch:
            parts.append(new_placement.new_branch)
        parts.append(new_placement.title)
        return " > ".join(parts)

    async def login(self) -> None:
        resp = await self._client.post(
            f"{self._base}/api/restful/authenticate",
            json={"email": self._email, "password": self._password},
        )
        if resp.status_code != 200:
            raise WiseMappingError(f"Could not authenticate with WiseMapping: HTTP {resp.status_code}")
        self._token = resp.text.strip().strip('"')

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def _fetch_xml(self) -> str:
        url = f"{self._base}/api/restful/maps/{self._map_id}/document/xml"
        resp = await self._client.get(url, headers={**self._auth_headers(), "Accept": "application/xml"})
        if resp.status_code == 401:
            await self.login()
            resp = await self._client.get(url, headers={**self._auth_headers(), "Accept": "application/xml"})
        if resp.status_code != 200:
            raise WiseMappingError(f"Could not fetch map: HTTP {resp.status_code}")
        return resp.text

    async def _save_xml(self, xml_text: str) -> None:
        url = f"{self._base}/api/restful/maps/{self._map_id}/document/xml"
        resp = await self._client.put(
            url,
            content=xml_text.encode(),
            headers={**self._auth_headers(), "Content-Type": "text/plain"},
        )
        if resp.status_code not in (200, 204):
            raise WiseMappingError(f"Could not save map: HTTP {resp.status_code}")

    async def aclose(self) -> None:
        await self._client.aclose()


# ── XML helpers ───────────────────────────────────────────────────────────────

def _flatten_branches(root: ET.Element) -> list[str]:
    """BFS over topics; return paths for CATEGORY nodes only.

    A category is a topic without a <link url=.../> child. Topics with a link
    are saved articles (leaves) and must not be offered as placement targets —
    otherwise the AI will nest new items under an existing article.
    """
    central = root.find(".//topic[@central='true']") or root.find(".//topic")
    if central is None:
        return []

    paths: list[str] = []
    queue: deque[tuple[ET.Element, list[str]]] = deque()

    for child in central:
        if child.tag == "topic":
            queue.append((child, [child.get("text", "")]))

    while queue:
        node, path = queue.popleft()
        # Skip saved articles (have a URL link) — they're leaves, not categories
        if node.find("link") is not None:
            continue
        paths.append(" > ".join(path))
        for child in node:
            if child.tag == "topic":
                queue.append((child, path + [child.get("text", "")]))

    return paths


def _assign_positions(root: ET.Element) -> None:
    """Assign position/order to any topics that are missing them."""
    central = root.find(".//topic[@central='true']") or root.find(".//topic")
    if central is None:
        return
    if not central.get("id"):
        central.set("id", "1")
    _assign_subtree(central, depth=0, parent_x=0, parent_y=0)


def _assign_subtree(node: ET.Element, depth: int, parent_x: float, parent_y: float) -> None:
    children = [c for c in node if c.tag == "topic"]
    n = len(children)
    for i, child in enumerate(children):
        if not child.get("order"):
            child.set("order", str(i))

        if not child.get("position"):
            if depth == 0:
                # Top-level: alternate left/right, spread vertically
                side = 1 if i % 2 == 0 else -1
                cx = side * 200
                cy = (i // 2) * 60 - (n // 4) * 60
            else:
                side = 1 if parent_x >= 0 else -1
                cx = parent_x + side * 130
                cy = parent_y + (i - n // 2) * 45
            child.set("position", f"{int(cx)},{int(cy)}")
        else:
            parts = child.get("position").split(",")
            cx, cy = float(parts[0]), float(parts[1])

        _assign_subtree(child, depth + 1, cx, cy)


def _find_or_create_path(root: ET.Element, path: list[str]) -> ET.Element:
    """Walk the path from the central node, creating any missing branches."""
    central = root.find(".//topic[@central='true']") or root.find(".//topic")
    node = central
    for label in path:
        child = next(
            (c for c in node if c.tag == "topic" and c.get("text") == label),
            None,
        )
        if child is None:
            child = ET.SubElement(node, "topic")
            child.set("id", str(_next_id(root)))
            child.set("text", label)
        node = child
    return node


def _find_topic(root: ET.Element, path: list[str]) -> ET.Element | None:
    """Walk the tree following path labels; return the matching element."""
    central = root.find(".//topic[@central='true']") or root.find(".//topic")
    if central is None:
        return None

    node = central
    for label in path:
        node = next(
            (c for c in node if c.tag == "topic" and c.get("text") == label),
            None,
        )
        if node is None:
            return None
    return node


def _next_id(root: ET.Element) -> int:
    ids = [int(t.get("id", 0)) for t in root.iter("topic") if t.get("id", "").isdigit()]
    return max(ids, default=0) + 1
