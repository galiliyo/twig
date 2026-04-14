import asyncio
import json
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
_MAX_SUMMARY_CHARS = 8000  # raw content cap; LLM summarizes this down to bullets

# Paywall detection: schema.org standard + Medium's generator tag (covers custom domains)
_MEDIUM_GENERATOR_RE = re.compile(
    r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']Medium', re.IGNORECASE
)


def _is_paywalled(html: str) -> bool:
    """True if the HTML indicates a paywall (schema.org) or is Medium-hosted."""
    # Normalize whitespace inside JSON-LD so 'false' right after ':' is findable either way
    compact = html.replace(" ", "").replace("\n", "")
    if '"isAccessibleForFree":false' in compact:
        return True
    if _MEDIUM_GENERATOR_RE.search(html):
        return True
    return False


def _parse_html(html: str) -> tuple[str | None, str | None]:
    """Extract (title, summary) from HTML using the standard selectors."""
    soup = BeautifulSoup(html, "lxml")

    title = None
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
    # Join paragraphs with newlines so the LLM can see structure, cap at 8000 chars
    summary = "\n\n".join(paragraphs)[:_MAX_SUMMARY_CHARS] if paragraphs else None
    return title, summary


async def _fetch_page(url: str) -> tuple[str | None, str | None]:
    """Fetch a page; escalate to Playwright on block (4xx/5xx) or paywall signal."""
    html = None
    status = None
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            status = resp.status_code
            if resp.status_code == 200:
                html = resp.text
    except Exception as exc:
        _log.info("httpx fetch failed for %s: %s", url, exc)

    # Escalate to Playwright when httpx was blocked/failed OR content is paywalled
    paywalled = bool(html) and _is_paywalled(html)
    if html is None or paywalled:
        _log.info("Escalating to Playwright for %s (status=%s, paywalled=%s)", url, status, paywalled)
        pw_title, pw_summary = await _fetch_with_playwright(url)
        if pw_summary:
            return pw_title or _title_from_url(url), pw_summary

    title, summary = _parse_html(html) if html else (None, None)

    # Still no content → try Exa as last resort
    if not title or not summary:
        exa_title, exa_summary = await _fetch_with_exa(url)
        title = title or exa_title
        summary = summary or exa_summary

    return title or _title_from_url(url), summary


_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = {runtime: {}};
"""
_REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _load_medium_cookies() -> list[dict]:
    """Load Medium cookies from env var (production) or JSON file (local dev)."""
    env_json = os.environ.get("MEDIUM_COOKIES_JSON")
    if env_json:
        return json.loads(env_json)
    cookie_path = os.environ.get("MEDIUM_COOKIES_PATH", "medium_cookies.json")
    with open(cookie_path) as f:
        return json.load(f)


async def _fetch_with_playwright(url: str) -> tuple[str | None, str | None]:
    """Fetch a page via headless Chromium using saved Medium session cookies."""
    try:
        from playwright.async_api import async_playwright

        raw_cookies = _load_medium_cookies()

        # Playwright requires sameSite to be one of these exact strings
        _SAME_SITE_MAP = {"strict": "Strict", "lax": "Lax", "no_restriction": "None", "unspecified": "None"}
        cookies = []
        for c in raw_cookies:
            cookie = {k: v for k, v in c.items() if k in ("name", "value", "domain", "path", "secure", "httpOnly")}
            # Cookie-Editor exports 'expirationDate' as float; Playwright expects integer 'expires'
            if "expirationDate" in c:
                cookie["expires"] = int(c["expirationDate"])
            if "sameSite" in c:
                cookie["sameSite"] = _SAME_SITE_MAP.get(c["sameSite"].lower(), "None")
            cookies.append(cookie)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            ctx = await browser.new_context(
                user_agent=_REAL_UA,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            await ctx.add_init_script(_STEALTH_INIT_JS)
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Cloudflare often inserts a JS challenge page first; wait briefly
            # for either the article body or networkidle, whichever comes first
            try:
                await page.wait_for_selector("article, main, [data-testid='storyContent']", timeout=10000)
            except Exception:
                pass

            html = await page.content()
            await browser.close()

        return _parse_html(html)

    except FileNotFoundError as exc:
        _log.warning("Cookies file not found: %s", exc)
    except json.JSONDecodeError as exc:
        _log.warning("Cookies JSON invalid: %s", exc)
    except Exception as exc:
        _log.warning("Playwright fetch failed for %s: %s", url, exc, exc_info=True)

    return None, None


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
