"""
OpenRouter-based branch placement and article summarization.

Sends the branch list + captured item to the configured model and parses a
structured placement decision. Separately summarizes article content to bullets
for storage in WiseMapping notes.
"""

import json
import logging
import os

import httpx

from .extractor import ExtractedInput, InputType
from .wisemapping import Placement

_log = logging.getLogger(__name__)
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM = """\
You are a mind-map placement assistant.
Given a list of existing branch paths and a captured item, return a JSON object:

{
  "branch_path": ["Parent", "Child"],   // existing path to place under
  "new_branch": null,                   // or a short label if a new intermediate branch is needed
  "title": "Short leaf node title"      // ≤ 6 words
}

Rules:
- Prefer existing branches. Create new_branch only as a last resort.
- Prefer broader categories over narrow ones.
- title must be concise (≤ 6 words).
- Respond with valid JSON only, no prose.
"""


async def choose_placement(branches: list[str], item: ExtractedInput) -> Placement:
    description = _describe(item)
    user_msg = f"Branches:\n{chr(10).join(branches)}\n\nItem: {description}"

    payload = {
        "model": os.environ["OPENROUTER_MODEL"],
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
    }

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
        raise RuntimeError(f"OpenRouter error: HTTP {resp.status_code} — {resp.text}")

    content = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences if the model wraps the JSON
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]

    data = json.loads(content)
    return Placement(
        branch_path=data["branch_path"],
        new_branch=data.get("new_branch"),
        title=data["title"],
    )


_RELOCATE_SYSTEM = """\
You are a mind-map placement assistant.
The user has decided an item belongs under a specific top-level branch.
Given the sub-branches under that branch and the item description,
return a JSON object:

{
  "branch_path": ["TopLevel", "SubBranch"],  // full path from top-level down
  "new_branch": null,                        // or a short label if a new sub-branch is needed
  "title": "Same title as before"            // keep the original title unchanged
}

Rules:
- The first element of branch_path MUST be the given top-level branch.
- Prefer existing sub-branches. Create new_branch only as a last resort.
- Keep the original title exactly as given.
- Respond with valid JSON only, no prose.
"""


async def choose_relocation(
    top_level: str,
    sub_branches: list[str],
    item_title: str,
    item_url: str | None,
    item_note: str | None,
) -> Placement:
    """Choose a 2nd-level placement under a forced top-level branch."""
    parts = [f"title={item_title!r}"]
    if item_url:
        parts.append(f"url={item_url}")
    if item_note:
        snippet = item_note[:400].replace("\n", " ").strip()
        parts.append(f"note_preview={snippet!r}")
    description = ", ".join(parts)

    branches_text = "\n".join(sub_branches) if sub_branches else "(no existing sub-branches)"
    user_msg = (
        f"Top-level branch: {top_level}\n"
        f"Sub-branches:\n{branches_text}\n\n"
        f"Item: {description}"
    )

    payload = {
        "model": os.environ["OPENROUTER_MODEL"],
        "messages": [
            {"role": "system", "content": _RELOCATE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
    }

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
        raise RuntimeError(f"OpenRouter error: HTTP {resp.status_code} — {resp.text}")

    content = resp.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]

    data = json.loads(content)
    return Placement(
        branch_path=data["branch_path"],
        new_branch=data.get("new_branch"),
        title=data["title"],
    )


_SUMMARIZE_SYSTEM = """\
You summarize articles for a personal knowledge mind-map.

Output format (in this exact order, with a blank line between the paragraph and the bullets):

1. One short paragraph (1-2 sentences) describing what the article is about.
   If the author is identifiable from the title or text, end this paragraph
   with "By <Author Name>." Omit the author line entirely if unknown.
2. A blank line.
3. 3 to 6 bullets, each starting with "• " (bullet + space).
   Each bullet is one sentence, under 25 words, concrete and specific.
   Capture the article's CLAIMS, ARGUMENTS, and CONCLUSIONS —
   not setup or side anecdotes. Include recommendations/predictions if present.

Output ONLY the paragraph and bullets. No headings, no intro, no closing.
"""


async def summarize_bullets(text: str, title: str | None = None) -> str | None:
    """Summarize article text into bullet points. Returns None on failure."""
    if not text or len(text.strip()) < 200:
        return None  # too short to be worth summarizing

    user_msg = text.strip()
    if title:
        user_msg = f"Title: {title}\n\n{user_msg}"

    payload = {
        "model": os.environ["OPENROUTER_MODEL"],
        "messages": [
            {"role": "system", "content": _SUMMARIZE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _OPENROUTER_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                    "HTTP-Referer": "https://github.com/galiliyo/twig",
                },
            )
        if resp.status_code != 200:
            _log.warning("Summarizer HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        bullets = resp.json()["choices"][0]["message"]["content"].strip()
        return bullets or None
    except Exception as exc:
        _log.warning("Summarizer failed: %s", exc)
        return None


_OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
_EMBED_MODEL = "text-embedding-3-small"
_EMBED_CHUNK = 100  # max texts per batch request


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed a list of strings. Splits into chunks to stay under API limits."""
    results: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(texts), _EMBED_CHUNK):
            batch = texts[i : i + _EMBED_CHUNK]
            resp = await client.post(
                _OPENAI_EMBED_URL,
                json={"model": _EMBED_MODEL, "input": batch},
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Embedding API error: HTTP {resp.status_code} — {resp.text}")
            data = sorted(resp.json()["data"], key=lambda x: x["index"])
            results.extend(item["embedding"] for item in data)
    return results


async def embed_query(text: str) -> list[float]:
    """Embed a single query string."""
    results = await embed_texts([text])
    return results[0]


def _describe(item: ExtractedInput) -> str:
    parts = [f"type={item.type.value}"]
    if item.title:
        parts.append(f"title={item.title!r}")
    if item.url:
        parts.append(f"url={item.url}")
    if item.summary:
        # First ~800 chars give the placement model actual content to classify on,
        # not just keywords in a title. Critical for disambiguating topics like
        # "Claude Code Angular Setup" (dev tooling) from "Claude model release" (AI).
        snippet = item.summary[:800].replace("\n", " ").strip()
        parts.append(f"content={snippet!r}")
    if not item.title and not item.url:
        parts.append(f"text={item.raw!r}")
    return ", ".join(parts)
