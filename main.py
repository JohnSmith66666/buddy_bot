"""
main.py - Buddy bot entry point.

CHANGES vs previous version:
  - Tilføjet webhook-routes for Radarr og Sonarr On Import notifikationer.
  - Webhook-server kører på port 8080 ved siden af Telegram polling.
  - Fjernet alle Seerr-referencer.

Webhook URLs (sæt i Radarr/Sonarr → Settings → Connect → Webhook):
  POST https://<railway-url>/webhook/radarr
  POST https://<railway-url>/webhook/sonarr
  Trigger: On Import
"""

import asyncio
import logging
import sys
from aiohttp import web

from telegram import Update
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
from ai_handler import clear_history, get_ai_response
from services.plex_service import validate_plex_user
from services.webhook_service import handle_radarr_webhook, handle_sonarr_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


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
            "Tjek stavningen og prøv igen — skriv blot dit brugernavn.",
            parse_mode="Markdown",
        )
        return

    verified = result["username"]
    await database.set_plex_username(user.id, verified)

    await update.message.reply_text(
        f"✅ Perfekt! Du er nu koblet til Plex som *{verified}*.\n\n"
        "Hvad kan jeg hjælpe dig med? 🚀",
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
    await update.message.reply_text(reply, parse_mode="Markdown")
    await database.log_message(user.id, "outgoing", reply)


# ── Webhook HTTP server (Radarr / Sonarr) ────────────────────────────────────

async def _webhook_radarr(request: web.Request) -> web.Response:
    """Receive On Import webhook from Radarr."""
    try:
        payload = await request.json()
        logger.info("Radarr webhook received: eventType=%s", payload.get("eventType"))
        asyncio.create_task(handle_radarr_webhook(payload))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error("Radarr webhook error: %s", e)
        return web.Response(status=400, text=str(e))


async def _webhook_sonarr(request: web.Request) -> web.Response:
    """Receive On Import webhook from Sonarr."""
    try:
        payload = await request.json()
        logger.info("Sonarr webhook received: eventType=%s", payload.get("eventType"))
        asyncio.create_task(handle_sonarr_webhook(payload))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error("Sonarr webhook error: %s", e)
        return web.Response(status=400, text=str(e))


async def _start_webhook_server() -> None:
    """Start aiohttp webhook server on port 8080."""
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
    await _start_webhook_server()
    logger.info("Buddy started in '%s' environment.", config.ENVIRONMENT)


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
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve_user:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting polling …")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()