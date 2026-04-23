"""
main.py - Buddy bot entry point.

Security model:
  - Unknown users are silently ignored; admin gets an approval button.
  - Approved users without a Plex username are guided through onboarding.
  - /skift_plex lets any approved user update their Plex username.

CHANGES vs previous version:
  - Removed the in-memory `_awaiting_plex_username` set.
    Onboarding state is now persisted in the `users.onboarding_state`
    column so Railway restarts never lose users mid-onboarding.
  - _guard() and _needs_plex_setup() now call the DB for state.
  - admin_handlers.py no longer needs to import from main.py.
"""

import logging
import sys

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Guards ────────────────────────────────────────────────────────────────────

async def _guard(update: Update) -> bool:
    """
    Return True if the user may proceed.
    Unknown users trigger an admin notification and are silently dropped.
    """
    user = update.effective_user
    if user is None:
        return False

    await database.upsert_user(user.id, user.username or user.first_name)

    if not await database.is_whitelisted(user.id):
        await notify_admin_new_user(update)
        return False

    return True


async def _needs_plex_setup(update: Update) -> bool:
    """
    Return True (and send the onboarding prompt) if the user has no Plex
    username yet AND is not already mid-onboarding.

    State is read from the DB — survives Railway restarts.
    """
    user = update.effective_user

    plex_username = await database.get_plex_username(user.id)
    if plex_username:
        return False

    onboarding_state = await database.get_onboarding_state(user.id)

    # Already waiting for their reply — don't send a second prompt.
    if onboarding_state == "awaiting_plex":
        return True

    # First time: set state and send prompt.
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
    """Validate and save the Plex username supplied by the user."""
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
    await database.set_plex_username(user.id, verified)  # also clears onboarding_state

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
    """/skift_plex — lets an approved user update their Plex username."""
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

    # Onboarding intercept — check DB state, not an in-memory set.
    onboarding_state = await database.get_onboarding_state(user.id)
    if onboarding_state == "awaiting_plex":
        await _handle_plex_input(update, text)
        return

    # First-time Plex setup (catches edge cases where state wasn't set yet).
    if await _needs_plex_setup(update):
        return

    # Normal Claude flow.
    await update.message.chat.send_action("typing")
    plex_username = await database.get_plex_username(user.id)
    reply = await get_ai_response(
        telegram_id=user.id,
        user_message=text,
        plex_username=plex_username,
    )
    await update.message.reply_text(reply, parse_mode="Markdown")
    await database.log_message(user.id, "outgoing", reply)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def on_startup(application: Application) -> None:
    await database.setup_db()
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
<<<<<<< HEAD
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
=======
    # FIX: drop_pending_updates=True sikrer at ventende opdateringer fra en
    # tidligere instans droppes ved opstart. Det forhindrer 409 Conflict-fejlen
    # når Railway starter en ny container mens den gamle stadig er aktiv.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
>>>>>>> b6089dcf2804e487730e79b280a4439225f6dc89


if __name__ == "__main__":
    main()