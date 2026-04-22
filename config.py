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
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_TOKEN")

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL: str = _require("DATABASE_URL")

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")

# ── TMDB ──────────────────────────────────────────────────────────────────────
TMDB_API_KEY: str = _require("TMDB_API_KEY")

# ── Seerr ─────────────────────────────────────────────────────────────────────
SEERR_API_KEY: str = _require("SEERR_API")
SEERR_URL: str = _require("SEERR_URL")

# ── Plex ──────────────────────────────────────────────────────────────────────
PLEX_URL: str = _require("PLEX_URL")
PLEX_TOKEN: str = _require("PLEX_TOKEN")

# ── Media root folders ────────────────────────────────────────────────────────
ROOT_MOVIE_ANIMATION: str = "/mnt/unionfs/Media/Movies/Animation"
ROOT_MOVIE_DANSK: str     = "/mnt/unionfs/Media/Movies/Dansk"
ROOT_MOVIE_STANDARD: str  = "/mnt/unionfs/Media/Movies/Film"
ROOT_TV_STANDARD: str     = "/mnt/unionfs/Media/TV/Serier"
ROOT_TV_PROGRAMMER: str   = "/mnt/unionfs/Media/TV/TV"

# ── Optional / runtime settings ───────────────────────────────────────────────
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "dev")
LOG_HISTORY_LIMIT: int = int(os.getenv("LOG_HISTORY_LIMIT", "500"))