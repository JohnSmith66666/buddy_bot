"""
admin_handlers.py - Admin approval logic for Buddy.

Kept in its own file so it can be moved to a dedicated bot later
without touching main.py or any other module.
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

    - Whitelists the user in the database.
    - Deletes the approval message from the Buddy chat.
    - Sends a confirmation DM to the admin.
    - Sends a plain-text welcome to the new user asking for Plex username.
    - Registers the new user in _awaiting_plex_username so their first
      reply is handled correctly without needing a second prompt.
    """
    query = update.callback_query
    await query.answer()

    # Only the admin may approve
    if query.from_user.id != ADMIN_TELEGRAM_ID:
        await query.answer("Du har ikke adgang til at godkende brugere.", show_alert=True)
        return

    # Parse telegram_id from callback_data ("approve_user:123456")
    try:
        new_user_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        logger.error("Malformed callback_data: %s", query.data)
        return

    # Approve in DB
    await database.approve_user(new_user_id)

    user_row = await database.get_user(new_user_id)
    display = user_row.get("telegram_name") or str(new_user_id) if user_row else str(new_user_id)

    # Delete the approval message from the Buddy chat
    try:
        await query.delete_message()
    except Exception:
        pass

    # Send confirmation directly to admin as a private DM
    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=f"✅ {display} er nu godkendt og kan bruge Buddy.",
        )
    except Exception as e:
        logger.error("Could not send admin confirmation: %s", e)

    # Welcome the new user — plain text only, no parse_mode
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
        # Immediately register the user as awaiting their Plex username.
        # This prevents _needs_plex_setup() from sending a second prompt
        # before the user has had a chance to reply to this one.
        # Import here to avoid circular import at module level.
        from main import _awaiting_plex_username
        _awaiting_plex_username.add(new_user_id)
        logger.info("User %s registered in _awaiting_plex_username", new_user_id)
    except Exception as e:
        logger.error("Could not send welcome to user %s: %s", new_user_id, e)

    logger.info("User %s approved by admin.", new_user_id)