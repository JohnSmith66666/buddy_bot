"""
config.py - Centralized configuration loaded from environment variables.
The application will raise an error at startup if any required variable is missing.
"""

import os
from dotenv import load_dotenv

# Load .env file when running locally; Railway injects variables directly.
load_dotenv()


def _require(key: str) -> str:
    """Fetch a required environment variable or raise a clear error."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: '{key}'. "
            "Please set it in Railway or your local .env file."
        )
    return value


# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL: str = _require("DATABASE_URL")

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")

# ── Optional / runtime settings ───────────────────────────────────────────────
# The environment name ("dev" or "main") — useful for logging.
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "dev")

# Maximum number of messages stored per user in the interaction log.
LOG_HISTORY_LIMIT: int = int(os.getenv("LOG_HISTORY_LIMIT", "500"))
