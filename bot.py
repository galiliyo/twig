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
from core.ai import choose_placement

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


async def _save_item(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extract, place, and save a single item. Replies with result or error."""
    _in_flight.add(text)
    wm: WiseMapping = context.bot_data["wm"]
    try:
        item = await extract(text)
        branches = await wm.get_branches()
        placement = await choose_placement(branches, item)

        placement.url = item.url
        placement.note = item.summary.strip() if item.summary else None
        log.info("placement.url=%r  note_len=%s", placement.url, len(placement.note) if placement.note else 0)
        saved_path = await wm.add_node(placement)

        _recent.append(text)
        if len(_recent) > _RECENT_MAX:
            _recent.pop(0)

        note_preview = f"\n📝 {len(placement.note)}c: {placement.note[:80]}" if placement.note else "\n📝 (no note)"
        log.info("Saved: %s", saved_path)
        await update.message.reply_text(f"✓ Saved to {saved_path}{note_preview}")

    except WiseMappingError as exc:
        log.error("WiseMapping error: %s", exc)
        msg = "Could not authenticate with WiseMapping" if "auth" in str(exc).lower() else "Could not save map update"
        await update.message.reply_text(f"❌ {msg}")

    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        await update.message.reply_text("❌ Something went wrong")

    finally:
        _in_flight.discard(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # "force" reply to a duplicate warning → bypass duplicate check
    if text.lower() == "force" and update.message.reply_to_message:
        original = _force_pending.pop(update.message.reply_to_message.message_id, None)
        if original:
            log.info("Force-saving: %r", original[:120])
            await _save_item(original, update, context)
        else:
            await update.message.reply_text("Nothing to force-save (warning expired or already saved).")
        return

    log.info("Received message: %r", text[:120])

    # Early duplicate check — catches repeated sends before expensive Exa/AI calls
    if text in _in_flight or text in _recent:
        warning = await update.message.reply_text("↺ Already processing or recently saved — reply force to save anyway")
        _force_pending[warning.message_id] = text
        return

    await _save_item(text, update, context)


async def testnote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /testnote <url>")
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

    await update.message.reply_text("\n".join(lines)[:4000])


async def showxml_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
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
        await update.message.reply_text("\n\n".join(lines)[:4000])
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    wm: WiseMapping = context.bot_data["wm"]
    try:
        branches = await wm.get_branches()
        if not branches:
            await update.message.reply_text("⚠️ No branches found (empty map or parse error)")
            return
        lines = "\n".join(f"• {b}" for b in branches)
        if len(lines) > 4000:
            lines = lines[:4000] + "\n…(truncated)"
        await update.message.reply_text(f"🗺 Branches ({len(branches)}):\n\n{lines}")
    except WiseMappingError as exc:
        await update.message.reply_text(f"❌ {exc}")


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

    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("testnote", testnote_command))
    app.add_handler(CommandHandler("showxml", showxml_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Twig bot starting (polling)…")
    app.run_polling()


if __name__ == "__main__":
    main()
