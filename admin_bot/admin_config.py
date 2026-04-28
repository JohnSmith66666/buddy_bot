"""
admin_bot/admin_config.py - Configuration for the Buddy Admin bot.

CHANGES (v0.1.0 — initial):
  - ADMIN_BOT_TOKEN: Egen Telegram-token (oprettes via @BotFather som fx
    Buddy_admin). Adskilt fra Buddy main-token.
  - BUDDY_BOT_TOKEN: Buddy MAIN-bot's token. Bruges når admin svarer på
    feedback — admin-botten sender beskeden via Buddy så testeren ser den
    i deres normale Buddy-chat.
  - DATABASE_URL: PostgreSQL connection-string. Skal pege på MAIN-DB
    (samme som buddy-main service bruger).
  - ADMIN_TELEGRAM_ID: Din Telegram-ID. Kun denne ID må bruge admin-bot
    kommandoer.
  - ENVIRONMENT: 'production' eller 'dev' — bruges kun til logs.

DESIGN-PRINCIPPER:
  - Samme _require() pattern som Buddys config.py.
  - Egen .env-fil tillades for lokal udvikling.
  - Ingen circular imports — admin-bot er helt selvstændig proces.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _require(key: str) -> str:
    """Hent obligatorisk env-var eller fejl med tydelig besked."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: '{key}'. "
            "Please set it in Railway or your local .env file."
        )
    return value


# ── Telegram bot tokens ───────────────────────────────────────────────────────
# Admin-bottens egen token (oprettes via @BotFather)
ADMIN_BOT_TOKEN: str = _require("ADMIN_BOT_TOKEN")

# Buddy MAIN-bottens token — bruges til at sende svar til testere via Buddy
BUDDY_BOT_TOKEN: str = _require("BUDDY_BOT_TOKEN")

# Din Telegram-ID — kun denne user må bruge admin-kommandoer
ADMIN_TELEGRAM_ID: int = int(_require("ADMIN_TELEGRAM_ID"))


# ── Database ──────────────────────────────────────────────────────────────────
# Skal pege på MAIN-database (samme som buddy-main bruger)
DATABASE_URL: str = _require("DATABASE_URL")


# ── Optional / runtime settings ───────────────────────────────────────────────
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")

# Default antal records vist i /list (kan overrides via env)
DEFAULT_LIST_LIMIT: int = int(os.getenv("DEFAULT_LIST_LIMIT", "10"))