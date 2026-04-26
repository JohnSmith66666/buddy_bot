"""
admin_handlers.py - Admin approval logic for Buddy.

CHANGES vs previous version:
  - Removed the circular import of `_awaiting_plex_username` from main.py.
    Onboarding state is now written to the DB via database.approve_user(),
    which sets onboarding_state = 'awaiting_plex' atomically with the
    whitelist approval. No in-memory state needed.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
from config import ADMIN_TELEGRAM_ID

logger = logging.getLogger(__name__)


async def notify_admin_new_user(update: Update) -> None:
    """
    Send the admin a notification with an approval button when an
    unknown user tries to use Buddy.
    Plain text only — no parse_mode — to avoid Markdown errors.
    """
    user = update.effective_user
    if user is None:
        return

    display = f"@{user.username}" if user.username else (user.first_name or "Ukendt")
    text = (
        f"🔔 Ny bruger ønsker adgang til Buddy\n\n"
        f"Navn: {display}\n"
        f"Telegram ID: {user.id}\n\n"
        f"Vil du godkende denne bruger?"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Godkend ✅",
            callback_data=f"approve_user:{user.id}",
        )
    ]])

    try:
        await update.get_bot().send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=text,
            reply_markup=keyboard,
        )
        logger.info("Admin notified about new user telegram_id=%s", user.id)
    except Exception as e:
        logger.error("Failed to notify admin: %s", e)


async def handle_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the 'Godkend' button press from the admin.

    - Whitelists the user in the database and sets onboarding_state = 'awaiting_plex'.
    - Deletes the approval message from the admin chat.
    - Sends a confirmation DM to the admin.
    - Sends a plain-text welcome to the new user asking for Plex username.

    No circular imports needed — state is fully DB-driven.
    """
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_TELEGRAM_ID:
        await query.answer("Du har ikke adgang til at godkende brugere.", show_alert=True)
        return

    try:
        new_user_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        logger.error("Malformed callback_data: %s", query.data)
        return

    # approve_user() sets is_whitelisted=TRUE and onboarding_state='awaiting_plex'
    # in a single DB call — no in-memory state needed.
    await database.approve_user(new_user_id)

    user_row = await database.get_user(new_user_id)
    display = user_row.get("telegram_name") or str(new_user_id) if user_row else str(new_user_id)

    try:
        await query.delete_message()
    except Exception:
        pass

    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=f"✅ {display} er nu godkendt og kan bruge Buddy.",
        )
    except Exception as e:
        logger.error("Could not send admin confirmation: %s", e)

    welcome = (
        "🎩 Du er nu godkendt!\n\n"
        "For at jeg kan give dig personlige svar, skal jeg kende dit "
        "Plex-brugernavn.\n\n"
        "Skriv det herunder - jeg tjekker det med det samme 🎬"
    )
    try:
        await context.bot.send_message(
            chat_id=new_user_id,
            text=welcome,
        )
    except Exception as e:
        logger.error("Could not send welcome to user %s: %s", new_user_id, e)

    logger.info("User %s approved by admin.", new_user_id)