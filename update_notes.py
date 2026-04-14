"""
Backfill/refresh notes on mind-map nodes that have URLs but placeholder/blocked notes.

Run locally — uses your residential IP + medium_cookies.json to fetch full
article content, summarizes via OpenRouter, and saves back to WiseMapping.

Usage:
    python update_notes.py            # refresh all matching nodes
    python update_notes.py --dry-run  # preview changes, don't save
    python update_notes.py --all      # refresh every node with a URL, not just blocked ones
"""

import asyncio
import sys
import xml.etree.ElementTree as ET

from dotenv import load_dotenv
load_dotenv()

from core.wisemapping import WiseMapping, _assign_positions
from core.extractor import _fetch_with_playwright
from core.ai import summarize_bullets


# Notes containing any of these strings are treated as "needs refresh"
BOT_CHALLENGE_MARKERS = [
    "website uses a security service",
    "verifies you are not a bot",
    "Attention Required",
    "Just a moment",
]


def needs_refresh(note_text: str) -> bool:
    if not note_text:
        return True  # empty note → refresh
    return any(m.lower() in note_text.lower() for m in BOT_CHALLENGE_MARKERS)


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    refresh_all = "--all" in sys.argv

    wm = WiseMapping()
    await wm.login()

    xml_text = await wm._fetch_xml()
    root = ET.fromstring(xml_text)

    # Collect topics with a URL (link child) — the ones saved from articles
    candidates: list[tuple[ET.Element, str, str]] = []
    for topic in root.iter("topic"):
        link = topic.find("link")
        note = topic.find("note")
        if link is None:
            continue
        url = link.get("url", "")
        note_text = note.get("text", "") if note is not None else ""
        if not url:
            continue
        if refresh_all or needs_refresh(note_text):
            candidates.append((topic, url, note_text))

    print(f"Found {len(candidates)} node(s) to refresh"
          + (" (all URL nodes)" if refresh_all else " (blocked/empty notes only)"))
    if not candidates:
        await wm.aclose()
        return

    updated = 0
    for i, (topic, url, old_note) in enumerate(candidates, 1):
        title_attr = topic.get("text", "(untitled)")
        print(f"\n[{i}/{len(candidates)}] {title_attr}")
        print(f"  URL:    {url}")
        print(f"  Before: {len(old_note)}c — {old_note[:80]!r}")

        try:
            fetched_title, raw_content = await _fetch_with_playwright(url)
            if not raw_content or len(raw_content) < 200:
                print(f"  ✗ Fetch returned only {len(raw_content or '')}c — skipping")
                continue

            bullets = await summarize_bullets(raw_content, title=fetched_title)
            if not bullets:
                print("  ✗ Summarization failed — skipping")
                continue

            print(f"  After:  {len(bullets)}c — {bullets[:80]!r}")

            if dry_run:
                print("  [DRY-RUN] no changes saved")
            else:
                note_el = topic.find("note")
                if note_el is None:
                    note_el = ET.SubElement(topic, "note")
                note_el.set("text", bullets)
                updated += 1

        except Exception as exc:
            print(f"  ✗ Error: {type(exc).__name__}: {exc}")

        await asyncio.sleep(1)  # polite delay between Playwright launches

    if dry_run:
        print(f"\n[DRY-RUN] {len(candidates)} candidate(s) would be processed")
    elif updated > 0:
        print(f"\nSaving {updated} updated note(s)...")
        _assign_positions(root)
        await wm._save_xml(ET.tostring(root, encoding="unicode", xml_declaration=False))
        print("Done")
    else:
        print("\nNo notes updated")

    await wm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
