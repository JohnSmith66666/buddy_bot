"""
main.py - Buddy bot entry point.

CHANGES vs previous version:
  - BUGFIX SyntaxWarning: docstring-eksemplet med invalid escape sequence
    er erstattet med en beskrivende tekst. Python 3.12+ advarer om
    backslash-sekvenser selv i docstrings og kommentarer.
  - BUGFIX Trailer-knap i chat: handle_text() detekterer nu om Buddys
    svar indeholder en youtu.be-URL (trailer fra get_media_details).
    Hvis ja, strippes URL'en fra beskedteksten og sendes i stedet som
    en separat InlineKeyboardButton ("🎬 Se Trailer") under beskeden.
    Dette giver samme knap-oplevelse som i bestillingsflowet, uanset
    om brugeren spørger direkte i chat eller via bestillingsknapper.
  - _extract_trailer() hjælpefunktion tilføjet til URL-detektion og
    tekstoprydning. Bruger raw strings korrekt i alle regex-patterns.
  - escape_markdown() er bevaret og bruger raw string replacement
    internt for at undgå SyntaxWarning i Python 3.12+.
"""

import asyncio
import logging
import re
import sys
from aiohttp import web

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database
from admin_handlers import handle_approve_callback, notify_admin_new_user
from ai_handler import SEARCH_SIGNAL, clear_history, get_ai_response
from services.confirmation_service import (
    execute_order,
    show_confirmation,
    show_search_results,
)
from services.plex_service import validate_plex_user
from services.webhook_service import handle_radarr_webhook, handle_sonarr_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Matcher youtu.be kort-URL'er — bruges til at detektere trailer-links i Buddys svar
_TRAILER_RE = re.compile(r"https://youtu\.be/[A-Za-z0-9_\-]+")

# Matcher alle http(s)-URL'er — bruges til escape af underscores i tekst-beskeder
_URL_RE = re.compile(r"(https?://[^\s)\]>\"]+)")


def escape_markdown(text: str) -> str:
    """
    Escaper underscores inde i URL'er saa Telegrams Markdown-parser ikke
    misfortolker dem som kursiv-markoerer og kraesjer med 'Can't parse entities'.

    Kun tegn inde i URL'er beroeres - al anden Markdown-formatering
    (fed, kursiv, inline code osv.) i den omgivende tekst er uaendret.
    Backslash-escapingen sker via raw string replacement internt.
    """
    def _escape_url(match: re.Match) -> str:
        return match.group(1).replace("_", r"\_")

    return _URL_RE.sub(_escape_url, text)


def _extract_trailer(reply: str) -> tuple[str, str | None]:
    """
    Detekter og ekstraher en youtu.be trailer-URL fra Buddys svar.

    Returnerer (renset_tekst, trailer_url) hvor:
      - renset_tekst er svaret uden linjen der indeholder URL'en
      - trailer_url er den fundne URL, eller None hvis ingen trailer fundet
    """
    match = _TRAILER_RE.search(reply)
    if not match:
        return reply, None

    trailer_url = match.group(0)

    # Fjern hele linjen der indeholder URL'en (inkl. evt. "Se trailer her:"-prefix)
    cleaned = re.sub(r"[^\n]*" + re.escape(trailer_url) + r"[^\n]*\n?", "", reply)
    cleaned = cleaned.rstrip()

    return cleaned, trailer_url


# ── Guards ────────────────────────────────────────────────────────────────────

async def _guard(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    await database.upsert_user(user.id, user.username or user.first_name)
    if not await database.is_whitelisted(user.id):
        await notify_admin_new_user(update)
        return False
    return True


async def _needs_plex_setup(update: Update) -> bool:
    user = update.effective_user
    plex_username = await database.get_plex_username(user.id)
    if plex_username:
        return False
    onboarding_state = await database.get_onboarding_state(user.id)
    if onboarding_state == "awaiting_plex":
        return True
    await database.set_onboarding_state(user.id, "awaiting_plex")
    await update.message.reply_text(
        f"👋 Hej {user.first_name}!\n\n"
        "For at jeg kan give dig personlige svar, skal jeg kende dit "
        "Plex-brugernavn.\n\n"
        "Skriv det herunder - jeg tjekker det med det samme 🎬"
    )
    return True


# ── Plex onboarding ───────────────────────────────────────────────────────────

async def _handle_plex_input(update: Update, raw_input: str) -> None:
    user = update.effective_user
    await update.message.chat.send_action("typing")
    result = await validate_plex_user(raw_input.strip())
    if not result.get("valid"):
        await update.message.reply_text(
            f"❌ Jeg kan ikke finde *{raw_input}* på Plex-serveren.\n\n"
            "Tjek stavningen og prøv igen.",
            parse_mode="Markdown",
        )
        return
    verified = result["username"]
    await database.set_plex_username(user.id, verified)
    await update.message.reply_text(
        f"✅ Perfekt! Du er nu koblet til Plex som *{verified}*.\n\nHvad kan jeg hjælpe dig med? 🚀",
        parse_mode="Markdown",
    )
    logger.info("Onboarding complete — telegram_id=%s plex='%s'", user.id, verified)


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    clear_history(user.id)
    await database.log_message(user.id, "incoming", "/start")
    if await _needs_plex_setup(update):
        return
    reply = (
        f"👋 Hej {user.first_name}!\n\n"
        "Jeg er din personlige medie-assistent. Du kan bl.a. spørge mig om:\n"
        "• 🎬 Film og serier i dit Plex-bibliotek\n"
        "• ➕ Bestilling af ny film eller serie\n"
        "• 📺 Hvad der er på vej\n\n"
        "Hvad kan jeg hjælpe dig med?"
    )
    await update.message.reply_text(reply)
    await database.log_message(user.id, "outgoing", reply)


async def cmd_skift_plex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    await database.set_onboarding_state(user.id, "awaiting_plex")
    await database.log_message(user.id, "incoming", "/skift_plex")
    await update.message.reply_text(
        "Intet problem! 👌\nSkriv dit nye *Plex-brugernavn* herunder:",
        parse_mode="Markdown",
    )


# ── Inline Keyboard callbacks ─────────────────────────────────────────────────

async def handle_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bruger valgte et søgeresultat — vis detaljer + Bekræft/Annuller."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    token = query.data.split(":", 1)[1]
    plex_username = await database.get_plex_username(query.from_user.id)
    await show_confirmation(query, token, plex_username)


async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bruger bekræftede bestilling — send til Radarr/Sonarr."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    token = query.data.split(":", 1)[1]
    plex_username = await database.get_plex_username(query.from_user.id)
    await execute_order(query, token, plex_username)


async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bruger annullerede — ryd op."""
    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1]
    if token != "none":
        await database.get_pending_request(token)  # sletter fra DB

    await query.edit_message_text("Bestillingen blev annulleret. 👍")


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    user = update.effective_user
    text = (update.message.text or "").strip()
    await database.log_message(user.id, "incoming", text)

    onboarding_state = await database.get_onboarding_state(user.id)
    if onboarding_state == "awaiting_plex":
        await _handle_plex_input(update, text)
        return

    if await _needs_plex_setup(update):
        return

    await update.message.chat.send_action("typing")
    plex_username = await database.get_plex_username(user.id)

    reply = await get_ai_response(
        telegram_id=user.id,
        user_message=text,
        plex_username=plex_username,
    )

    # Tjek om Claude returnerer et søge-signal
    if reply.startswith(SEARCH_SIGNAL):
        parts = reply[len(SEARCH_SIGNAL):].split(":", 1)
        query_term = parts[0].strip()
        media_type = parts[1].strip() if len(parts) > 1 else "both"
        await show_search_results(update.message, query_term, media_type)
        return

    # Detekter trailer-URL i svaret — send som knap i stedet for raa tekst
    clean_reply, trailer_url = _extract_trailer(reply)

    if trailer_url:
        # Send tekstbesked uden URL + trailer-knap nedenunder
        safe_reply = escape_markdown(clean_reply)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Se Trailer", url=trailer_url)
        ]])
        await update.message.reply_text(
            safe_reply,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        logger.info("Trailer-knap sendt: %s", trailer_url)
    else:
        # Normalt svar uden trailer
        safe_reply = escape_markdown(reply)
        await update.message.reply_text(safe_reply, parse_mode="Markdown")

    await database.log_message(user.id, "outgoing", reply)


# ── Webhook HTTP server ───────────────────────────────────────────────────────

async def _webhook_radarr(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        logger.info("Radarr webhook received: eventType=%s", payload.get("eventType"))
        asyncio.create_task(handle_radarr_webhook(payload))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error("Radarr webhook error: %s", e)
        return web.Response(status=400, text=str(e))


async def _webhook_sonarr(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        logger.info("Sonarr webhook received: eventType=%s", payload.get("eventType"))
        asyncio.create_task(handle_sonarr_webhook(payload))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error("Sonarr webhook error: %s", e)
        return web.Response(status=400, text=str(e))


async def _start_webhook_server() -> None:
    app = web.Application()
    app.router.add_post("/webhook/radarr", _webhook_radarr)
    app.router.add_post("/webhook/sonarr", _webhook_sonarr)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Webhook server started on port 8080")


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def on_startup(application: Application) -> None:
    await database.setup_db()
    await database.setup_pending_requests()
    await _start_webhook_server()
    logger.info("Buddy started in '%s' environment.", config.ENVIRONMENT)
    logger.info("VERSION CHECK — trailer: JA | dato: JA | _extract_trailer: JA")


async def on_shutdown(application: Application) -> None:
    await database.close_db()
    logger.info("Buddy shut down cleanly.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("skift_plex", cmd_skift_plex))

    # Admin approval
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve_user:\d+$"))

    # Bestillingsflow
    app.add_handler(CallbackQueryHandler(handle_pick_callback,    pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(handle_confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_callback,  pattern=r"^cancel:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting polling …")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()