"""
admin_bot/admin_main.py - Buddy Admin bot entry point.

CHANGES (v0.1.0 — initial):
  - Standalone bot-process der kører på sin egen Telegram-token.
  - Genbruger services/feedback_service.py via sys.path-trick (filen ligger
    i parent-mappen — vi tilføjer parent til Python path så imports virker).
  - Initialiserer database-pool via admin_database.setup_db() der verificérer
    at feedback-tabellen findes (den er oprettet af Buddy main).
  - Registrerer alle kommandoer fra feedback_handlers.

DEPLOYMENT:
  - Egen Railway-service (anbefalet) eller samme Procfile som main-buddy.
  - Env-vars: ADMIN_BOT_TOKEN, BUDDY_BOT_TOKEN, DATABASE_URL, ADMIN_TELEGRAM_ID
  - Kør med: `python admin_bot/admin_main.py`

DESIGN-PRINCIPPER:
  - Helt selvstændig proces — ingen import af main.py eller config.py.
  - Bruger samme PostgreSQL-database som Buddy main (DATABASE_URL peger på MAIN-DB).
  - Ingen webhook-server (admin-bot har kun polling — den modtager ikke webhooks).
"""

import logging
import os
import sys

# ── sys.path setup ────────────────────────────────────────────────────────────
# Admin-bot ligger i admin_bot/ undermappen, men feedback_service.py
# ligger i parent's services/ mappe. Vi tilføjer parent + parent/services
# til Python path så vi kan importere feedback_service direkte.
#
# Dette undgår at vi skal duplikere feedback_service.py — én sandhedskilde.
_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PARENT_DIR)
sys.path.insert(0, os.path.join(_PARENT_DIR, "services"))

from telegram import Update
from telegram.ext import Application, CommandHandler

import admin_database as db
from admin_config import ADMIN_BOT_TOKEN, ADMIN_TELEGRAM_ID, ENVIRONMENT
from feedback_handlers import (
    cmd_help,
    cmd_list,
    cmd_reply,
    cmd_resolve,
    cmd_seen,
    cmd_start,
    cmd_stats,
    cmd_view,
    handle_error,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | admin_bot.%(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════════

async def on_startup(application: Application) -> None:
    """Initialiser database og verificér setup."""
    await db.setup_db()
    logger.info("Buddy Admin bot started in '%s' environment.", ENVIRONMENT)
    logger.info(
        "VERSION CHECK — admin-bot v0.1.0 | "
        "feedback-management: JA | reply-via-buddy: JA | "
        "list/view/reply/resolve/seen/stats: JA"
    )
    logger.info("Admin user: telegram_id=%s", ADMIN_TELEGRAM_ID)


async def on_shutdown(application: Application) -> None:
    """Luk database-pool ved shutdown."""
    await db.close_db()
    logger.info("Admin bot shut down cleanly.")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Byg admin-bot Application og start polling."""
    app = (
        Application.builder()
        .token(ADMIN_BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # Registrér kommandoer
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("list",     cmd_list))
    app.add_handler(CommandHandler("view",     cmd_view))
    app.add_handler(CommandHandler("reply",    cmd_reply))
    app.add_handler(CommandHandler("resolve",  cmd_resolve))
    app.add_handler(CommandHandler("seen",     cmd_seen))
    app.add_handler(CommandHandler("stats",    cmd_stats))

    # Global error handler
    app.add_error_handler(handle_error)

    logger.info("Admin bot — starting polling …")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()