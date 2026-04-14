"""
OpenRouter-based branch placement.

Sends the branch list + captured item to the configured model and parses a
structured placement decision.
"""

import json
import os

import httpx

from .extractor import ExtractedInput, InputType
from .wisemapping import Placement

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


def _describe(item: ExtractedInput) -> str:
    parts = [f"type={item.type.value}"]
    if item.title:
        parts.append(f"title={item.title!r}")
    if item.url:
        parts.append(f"url={item.url}")
    if not item.title and not item.url:
        parts.append(f"text={item.raw!r}")
    return ", ".join(parts)
