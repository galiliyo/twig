import asyncio
import os
import re
from dataclasses import dataclass
from enum import Enum

import httpx
from bs4 import BeautifulSoup


class InputType(Enum):
    YOUTUBE = "youtube"
    URL = "url"
    REMINDER = "reminder"
    IDEA = "idea"


@dataclass
class ExtractedInput:
    type: InputType
    raw: str
    url: str | None = None
    title: str | None = None
    summary: str | None = None


_YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w\-]+"
)
_URL_RE = re.compile(r"https?://\S+")
_REMINDER_RE = re.compile(r"\bremind(?:er| me)?\b", re.IGNORECASE)


async def extract(text: str) -> ExtractedInput:
    if m := _YOUTUBE_RE.search(text):
        url = m.group(0)
        title, summary = await _fetch_page(url)
        return ExtractedInput(type=InputType.YOUTUBE, raw=text, url=url, title=title, summary=summary)

    if m := _URL_RE.search(text):
        url = m.group(0)
        title, summary = await _fetch_page(url)
        return ExtractedInput(type=InputType.URL, raw=text, url=url, title=title, summary=summary)

    if _REMINDER_RE.search(text):
        return ExtractedInput(type=InputType.REMINDER, raw=text)

    return ExtractedInput(type=InputType.IDEA, raw=text)


_BLOCKED_TITLES = {"just a moment", "access denied", "attention required", "are you a human"}
_MAX_SUMMARY_CHARS = 1000


async def _fetch_page(url: str) -> tuple[str | None, str | None]:
    """Fetch a page and return (title, summary). Falls back to Exa when blocked."""
    title = None
    summary = None

    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")

                tag = soup.find("title")
                if tag:
                    t = tag.get_text(strip=True)
                    if not any(blocked in t.lower() for blocked in _BLOCKED_TITLES):
                        title = t

                paragraphs = [
                    p.get_text(strip=True)
                    for p in soup.find_all("p")
                    if len(p.get_text(strip=True)) > 80
                ]
                if paragraphs:
                    summary = " ".join(paragraphs[:5])[:_MAX_SUMMARY_CHARS]
    except Exception:
        pass

    # If direct fetch was blocked or gave no content, try Exa
    if not title or not summary:
        exa_title, exa_summary = await _fetch_with_exa(url)
        title = title or exa_title
        summary = summary or exa_summary

    return title or _title_from_url(url), summary


_log = __import__("logging").getLogger(__name__)


async def _fetch_with_exa(url: str) -> tuple[str | None, str | None]:
    """Use Exa to retrieve article content when direct fetch is blocked."""
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        _log.warning("EXA_API_KEY not set")
        return _title_from_url(url), None
    try:
        from exa_py import Exa
        exa = Exa(api_key=api_key)
        response = await asyncio.to_thread(exa.get_contents, [url], text=True)
        _log.info("Exa raw response: %r", response)
        if response.results:
            result = response.results[0]
            title = getattr(result, "title", None) or _title_from_url(url)
            text = (getattr(result, "text", None) or "").strip()
            summary = text[:_MAX_SUMMARY_CHARS] if text else None
            _log.info("Exa result: title=%r  text_len=%s  summary_len=%s", title, len(text), len(summary) if summary else 0)
            return title, summary
        _log.warning("Exa returned no results for %s", url)
    except Exception as exc:
        _log.warning("Exa fetch failed for %s: %s", url, exc, exc_info=True)
    return _title_from_url(url), None


def _title_from_url(url: str) -> str | None:
    """Extract a readable title from the URL path as a fallback."""
    try:
        path = url.split("?")[0].rstrip("/")
        slug = path.split("/")[-1]
        title = slug.replace("-", " ").replace("_", " ").strip()
        return title.title() if title else None
    except Exception:
        return None
