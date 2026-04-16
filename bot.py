import logging
import os
import socket
import sys

from dotenv import load_dotenv
load_dotenv()

# ── Single-instance lock ───────────────────────────────────────────────────────
_LOCK_SOCKET = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _LOCK_SOCKET.bind(("127.0.0.1", 47832))
except OSError:
    print("Another bot instance is already running. Exiting.")
    sys.exit(1)

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from core.extractor import extract
from core.wisemapping import WiseMapping, WiseMappingError
from core.ai import choose_placement, choose_relocation, summarize_bullets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

ALLOWED_USER_ID = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])

# Duplicate guard: raw message texts in-flight or recently saved
_recent: list[str] = []
_RECENT_MAX = 50
_in_flight: set[str] = set()

# Maps bot warning message_id → original user text, so "force" replies work
_force_pending: dict[int, str] = {}

# Last saved item — used by /replace to know what to move
_last_saved: dict | None = None  # {branch_path, title, url, note}

# Maps bot /replace message_id → list of top-level branch names, so numbered reply works
_replace_pending: dict[int, list[str]] = {}


async def _save_item(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extract, place, and save a single item. Replies with result or error."""
    message = update.effective_message
    if message is None:
        return
    _in_flight.add(text)
    wm: WiseMapping = context.bot_data["wm"]
    try:
        item = await extract(text)
        branches = await wm.get_branches()
        placement = await choose_placement(branches, item)

        placement.url = item.url
        # Try to summarize the raw content into bullets; fall back to truncated raw text on failure
        bullets = await summarize_bullets(item.summary, title=item.title) if item.summary else None
        if bullets:
            placement.note = bullets
            log.info("placement.url=%r  note_len=%s (bulleted)", placement.url, len(placement.note))
        elif item.summary:
            placement.note = item.summary.strip()[:1000]
            log.info("placement.url=%r  note_len=%s (raw fallback)", placement.url, len(placement.note))
        else:
            placement.note = None
            log.info("placement.url=%r  note_len=0", placement.url)
        saved_path = await wm.add_node(placement)

        _recent.append(text)
        if len(_recent) > _RECENT_MAX:
            _recent.pop(0)

        # Track last saved item for /replace
        global _last_saved
        _last_saved = {
            "branch_path": list(placement.branch_path) + ([placement.new_branch] if placement.new_branch else []),
            "title": placement.title,
            "url": placement.url,
            "note": placement.note,
        }

        note_preview = f"\n📝 {len(placement.note)}c: {placement.note[:80]}" if placement.note else "\n📝 (no note)"
        log.info("Saved: %s", saved_path)
        await message.reply_text(f"✓ Saved to {saved_path}{note_preview}")

    except WiseMappingError as exc:
        log.error("WiseMapping error: %s", exc)
        msg = "Could not authenticate with WiseMapping" if "auth" in str(exc).lower() else "Could not save map update"
        await message.reply_text(f"❌ {msg}")

    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        await message.reply_text("❌ Something went wrong")

    finally:
        _in_flight.discard(text)


async def replace_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show top-level branches so user can relocate the last saved item."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    message = update.effective_message
    if message is None:
        return

    if _last_saved is None:
        await message.reply_text("No recently saved item to replace.")
        return

    wm: WiseMapping = context.bot_data["wm"]
    try:
        top_branches = await wm.get_top_level_branches()
    except WiseMappingError as exc:
        await message.reply_text(f"❌ {exc}")
        return

    if not top_branches:
        await message.reply_text("No branches found in the map.")
        return

    old_path = " > ".join(_last_saved["branch_path"] + [_last_saved["title"]])
    lines = [f"Moving: {_last_saved['title']}", f"Currently at: {old_path}", "", "Pick a top-level branch:"]
    for i, branch in enumerate(top_branches, 1):
        lines.append(f"  {i}. {branch}")
    lines.append("\nReply with the number.")

    listing = await message.reply_text("\n".join(lines))
    _replace_pending[listing.message_id] = top_branches


async def _handle_replace_choice(
    choice: int,
    top_branches: list[str],
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Relocate the last saved item under the chosen top-level branch."""
    global _last_saved
    message = update.effective_message
    if message is None:
        return

    if _last_saved is None:
        await message.reply_text("No recently saved item to replace.")
        return

    if choice < 1 or choice > len(top_branches):
        await message.reply_text(f"Pick a number between 1 and {len(top_branches)}.")
        return

    chosen = top_branches[choice - 1]
    wm: WiseMapping = context.bot_data["wm"]

    try:
        sub_branches = await wm.get_sub_branches(chosen)
        new_placement = await choose_relocation(
            top_level=chosen,
            sub_branches=sub_branches,
            item_title=_last_saved["title"],
            item_url=_last_saved["url"],
            item_note=_last_saved["note"],
        )

        new_path = await wm.move_node(
            old_path=_last_saved["branch_path"],
            old_title=_last_saved["title"],
            new_placement=new_placement,
        )

        # Update last_saved to reflect new location
        _last_saved = {
            "branch_path": list(new_placement.branch_path) + ([new_placement.new_branch] if new_placement.new_branch else []),
            "title": new_placement.title,
            "url": new_placement.url or _last_saved.get("url"),
            "note": new_placement.note or _last_saved.get("note"),
        }

        log.info("Relocated to: %s", new_path)
        await message.reply_text(f"✓ Moved to {new_path}")

    except WiseMappingError as exc:
        log.error("WiseMapping error during replace: %s", exc)
        await message.reply_text(f"❌ Could not move: {exc}")
    except Exception as exc:
        log.exception("Replace failed: %s", exc)
        await message.reply_text(f"❌ Something went wrong: {exc}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    message = update.effective_message
    if message is None:
        return

    text = (message.text or "").strip()
    if not text:
        return

    # "force" reply to a duplicate warning → bypass duplicate check
    if text.lower() == "force" and message.reply_to_message:
        original = _force_pending.pop(message.reply_to_message.message_id, None)
        if original:
            log.info("Force-saving: %r", original[:120])
            await _save_item(original, update, context)
        else:
            await message.reply_text("Nothing to force-save (warning expired or already saved).")
        return

    # Numbered reply to a /replace listing → relocate the last saved item
    if text.isdigit() and message.reply_to_message:
        branches = _replace_pending.pop(message.reply_to_message.message_id, None)
        if branches:
            await _handle_replace_choice(int(text), branches, update, context)
            return

    log.info("Received message: %r", text[:120])

    # Early duplicate check — catches repeated sends before expensive Exa/AI calls
    if text in _in_flight or text in _recent:
        warning = await message.reply_text("↺ Already processing or recently saved — reply force to save anyway")
        _force_pending[warning.message_id] = text
        return

    await _save_item(text, update, context)


async def testnote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    message = update.effective_message
    if message is None:
        return

    args = context.args
    if not args:
        await message.reply_text("Usage: /testnote <url>")
        return

    url = args[0]
    lines = [f"Testing: {url}\n"]

    # Step 1: env
    exa_key = os.environ.get("EXA_API_KEY", "")
    lines.append(f"EXA_API_KEY: {'set (' + exa_key[:8] + '...)' if exa_key else 'MISSING'}")

    # Step 2: direct HTTP
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        lines.append(f"Direct HTTP: {resp.status_code}, {len(resp.text)} chars")
    except Exception as exc:
        lines.append(f"Direct HTTP error: {exc}")

    # Step 3: raw Exa call
    if exa_key:
        try:
            import asyncio
            from exa_py import Exa
            exa = Exa(api_key=exa_key)
            response = await asyncio.to_thread(exa.get_contents, [url], text=True)
            if response.results:
                r = response.results[0]
                txt = (getattr(r, "text", None) or "").strip()
                lines.append(f"Exa: title={getattr(r, 'title', None)!r}  text_len={len(txt)}")
                if txt:
                    lines.append(f"Exa text preview: {txt[:200]}")
                else:
                    lines.append("Exa text: EMPTY")
            else:
                lines.append("Exa: no results")
        except Exception as exc:
            lines.append(f"Exa error: {type(exc).__name__}: {exc}")
    else:
        lines.append("Exa: skipped (no key)")

    await message.reply_text("\n".join(lines)[:4000])


async def testmedium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/testmedium received from user_id=%s", update.effective_user.id)
    if update.effective_user.id != ALLOWED_USER_ID:
        log.warning("/testmedium dropped — user_id mismatch (allowed=%s)", ALLOWED_USER_ID)
        return

    message = update.effective_message
    if message is None:
        log.warning("/testmedium: effective_message is None")
        return

    args = context.args
    if not args:
        await message.reply_text("Usage: /testmedium <url>")
        return

    url = args[0]
    log.info("/testmedium: starting for %s", url)
    lines = [f"Testing paywall fetch: {url}\n"]

    # Step 1: httpx GET + paywall detection
    from core.extractor import _is_paywalled, _fetch_with_playwright
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        lines.append(f"Direct HTTP: {resp.status_code}, {len(resp.text)} chars")
        paywalled = _is_paywalled(resp.text) if resp.status_code == 200 else False
        lines.append(f"Paywall detected: {paywalled}")
        if paywalled:
            if '"isAccessibleForFree":false' in resp.text.replace(" ", "").replace("\n", ""):
                lines.append("  signal: schema.org isAccessibleForFree=false")
            import re as _re
            if _re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']Medium', resp.text, _re.I):
                lines.append("  signal: Medium generator meta tag")
    except Exception as exc:
        lines.append(f"Direct HTTP error: {exc}")
        paywalled = False

    # Step 2: playwright import
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        lines.append("✓ Playwright importable")
    except ImportError as exc:
        lines.append(f"✗ Playwright not installed: {exc}")
        await message.reply_text("\n".join(lines)[:4000])
        return

    # Step 3: cookies file
    import json
    cookie_path = os.environ.get("MEDIUM_COOKIES_PATH", "medium_cookies.json")
    try:
        with open(cookie_path) as f:
            cookies = json.load(f)
        lines.append(f"✓ Cookies loaded from {cookie_path} ({len(cookies)} cookies)")
        medium_cookies = [c for c in cookies if "medium.com" in c.get("domain", "")]
        lines.append(f"  of which {len(medium_cookies)} are *.medium.com cookies")
    except FileNotFoundError:
        lines.append(f"✗ Cookies file not found at {cookie_path}")
        await message.reply_text("\n".join(lines)[:4000])
        return
    except Exception as exc:
        lines.append(f"✗ Cookies file parse error: {exc}")
        await message.reply_text("\n".join(lines)[:4000])
        return

    # Step 4: actual Playwright fetch
    log.info("/testmedium: launching Playwright fetch")
    try:
        title, summary = await _fetch_with_playwright(url)
        log.info("/testmedium: playwright returned title=%r summary_len=%s", title, len(summary) if summary else 0)
        lines.append(f"\nTitle: {title!r}")
        lines.append(f"Summary length: {len(summary) if summary else 0}")
        if summary:
            lines.append(f"\nSummary preview:\n{summary[:400]}")
        else:
            lines.append("Summary: EMPTY")
    except Exception as exc:
        log.exception("/testmedium: fetch raised")
        lines.append(f"✗ Fetch raised: {type(exc).__name__}: {exc}")

    await message.reply_text("\n".join(lines)[:4000])


async def showxml_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    message = update.effective_message
    if message is None:
        return
    wm: WiseMapping = context.bot_data["wm"]
    try:
        import xml.etree.ElementTree as ET
        xml_text = await wm._fetch_xml()
        root = ET.fromstring(xml_text)
        topics = list(root.iter("topic"))
        lines = [f"Total topics: {len(topics)}\n--- Last 2 nodes (raw XML) ---"]
        for t in topics[-2:]:
            raw = ET.tostring(t, encoding="unicode")
            lines.append(raw[:800])
        await message.reply_text("\n\n".join(lines)[:4000])
    except Exception as exc:
        await message.reply_text(f"❌ {exc}")


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/debug received from user_id=%s (allowed=%s)", update.effective_user.id, ALLOWED_USER_ID)
    if update.effective_user.id != ALLOWED_USER_ID:
        log.warning("/debug dropped — user_id mismatch")
        return

    message = update.effective_message
    if message is None:
        return

    wm: WiseMapping = context.bot_data["wm"]
    try:
        branches = await wm.get_branches()
        if not branches:
            await message.reply_text("⚠️ No branches found (empty map or parse error)")
            return
        lines = "\n".join(f"• {b}" for b in branches)
        if len(lines) > 4000:
            lines = lines[:4000] + "\n…(truncated)"
        await message.reply_text(f"🗺 Branches ({len(branches)}):\n\n{lines}")
    except WiseMappingError as exc:
        await message.reply_text(f"❌ {exc}")


async def post_init(app) -> None:
    await app.bot_data["wm"].login()
    log.info("WiseMapping session established")


async def post_shutdown(app) -> None:
    await app.bot_data["wm"].aclose()


def main() -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    wm = WiseMapping()
    app.bot_data["wm"] = wm

    app.add_handler(CommandHandler("replace", replace_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("testnote", testnote_command))
    app.add_handler(CommandHandler("testmedium", testmedium_command))
    app.add_handler(CommandHandler("showxml", showxml_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Twig bot starting (polling)…")
    app.run_polling()


if __name__ == "__main__":
    main()
