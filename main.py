"""
main.py - Entry point for the Telegram media-assistant bot.

Startup sequence:
  1. Validate environment variables (config.py raises on missing keys).
  2. Connect to PostgreSQL and create tables (database.py).
  3. Register command / message handlers.
  4. Start polling.
"""

import logging
import sys

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database
from ai_handler import clear_history, get_ai_response

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _check_whitelist(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False

    if not await database.is_whitelisted(user.id):
        logger.warning(
            "Unauthorised access attempt — telegram_id=%s username=%s",
            user.id,
            user.username,
        )
        await update.message.reply_text(
            "⛔ Du har ikke adgang til denne bot. Kontakt administratoren."
        )
        return False

    return True


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — greet the user and reset conversation history."""
    if not await _check_whitelist(update):
        return

    user = update.effective_user
    clear_history(user.id)

    await database.log_message(
        telegram_id=user.id,
        direction="incoming",
        message_text="/start",
        username=user.username,
    )

    reply = (
        f"👋 Hej {user.first_name}!\n\n"
        "Jeg er din personlige medie-assistent. "
        "Du kan bl.a. spørge mig om:\n"
        "• 🎬 Film og serier i dit Plex-bibliotek\n"
        "• ➕ Tilføjelse af ny film via Radarr\n"
        "• 📺 Status på downloads\n\n"
        "Hvad kan jeg hjælpe dig med?"
    )

    await update.message.reply_text(reply, parse_mode="Markdown")
    await database.log_message(
        telegram_id=user.id,
        direction="outgoing",
        message_text=reply,
        username=user.username,
    )


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route plain text messages through Claude and return the response."""
    if not await _check_whitelist(update):
        return

    user = update.effective_user
    text = update.message.text or ""

    # 1. Log incoming message to PostgreSQL.
    await database.log_message(
        telegram_id=user.id,
        direction="incoming",
        message_text=text,
        username=user.username,
    )

    # 2. Show typing indicator while waiting for Claude.
    await update.message.chat.send_action("typing")

    # 3. Get response from Claude.
    reply = await get_ai_response(telegram_id=user.id, user_message=text)

    # 4. Send reply to user.
    await update.message.reply_text(reply, parse_mode="Markdown")

    # 5. Log outgoing message to PostgreSQL.
    await database.log_message(
        telegram_id=user.id,
        direction="outgoing",
        message_text=reply,
        username=user.username,
    )


# ── Lifecycle hooks ───────────────────────────────────────────────────────────

async def on_startup(application: Application) -> None:
    await database.setup_db()
    logger.info("Bot started in '%s' environment.", config.ENVIRONMENT)


async def on_shutdown(application: Application) -> None:
    await database.close_db()
    logger.info("Bot shut down cleanly.")


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting polling …")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
