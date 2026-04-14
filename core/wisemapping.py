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
from dataclasses import dataclass

import httpx


@dataclass
class Placement:
    branch_path: list[str]   # e.g. ["Front-End", "Angular"]
    new_branch: str | None   # intermediate branch to create, if any
    title: str               # short leaf node title
    url: str | None = None   # original URL, attached as a clickable link
    note: str | None = None  # extracted article content, attached as a note


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
            note_el.text = placement.note

        _assign_positions(root)
        await self._save_xml(ET.tostring(root, encoding="unicode", xml_declaration=False))

        parts = list(placement.branch_path)
        if placement.new_branch:
            parts.append(placement.new_branch)
        parts.append(placement.title)
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
    """BFS over all topics; return paths like 'Branch > Sub-branch'."""
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
