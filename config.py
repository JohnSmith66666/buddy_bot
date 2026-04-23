"""
config.py - Centralized configuration loaded from environment variables.
The application will raise an error at startup if any required variable is missing.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: '{key}'. "
            "Please set it in Railway or your local .env file."
        )
    return value


# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_TOKEN")
ADMIN_TELEGRAM_ID: int  = int(_require("ADMIN_TELEGRAM_ID"))

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL: str = _require("DATABASE_URL")

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")

# FIX: Model string moved here so it can be changed without touching ai_handler.py.
# Switch to "claude-sonnet-4-5" for smarter (but slower/pricier) responses.
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# ── TMDB ──────────────────────────────────────────────────────────────────────
TMDB_API_KEY: str = _require("TMDB_API_KEY")

# ── Seerr ─────────────────────────────────────────────────────────────────────
SEERR_API_KEY: str = _require("SEERR_API")
SEERR_URL: str     = _require("SEERR_URL")

# ── Plex ──────────────────────────────────────────────────────────────────────
PLEX_URL: str   = _require("PLEX_URL")
PLEX_TOKEN: str = _require("PLEX_TOKEN")

# ── Media root folders ────────────────────────────────────────────────────────
ROOT_MOVIE_ANIMATION: str = "/mnt/unionfs/Media/Movies/Animation"
ROOT_MOVIE_DANSK: str     = "/mnt/unionfs/Media/Movies/Dansk"
ROOT_MOVIE_STANDARD: str  = "/mnt/unionfs/Media/Movies/Film"
ROOT_TV_STANDARD: str     = "/mnt/unionfs/Media/TV/Serier"
ROOT_TV_PROGRAMMER: str   = "/mnt/unionfs/Media/TV/TV"

<<<<<<< HEAD
# ── Tautulli ──────────────────────────────────────────────────────────────────
TAUTULLI_URL:     str = _require("TAUTULLI_URL")
TAUTULLI_API_KEY: str = _require("TAUTULLI_API_KEY")

=======
# ── Tautulli ──────────────────────────────────────────────────────────────────
TAUTULLI_URL:     str = _require("TAUTULLI_URL")       # e.g. http://192.168.1.10:8181
TAUTULLI_API_KEY: str = _require("TAUTULLI_API_KEY")

>>>>>>> b6089dcf2804e487730e79b280a4439225f6dc89
# ── Optional / runtime settings ───────────────────────────────────────────────
ENVIRONMENT: str       = os.getenv("ENVIRONMENT", "dev")
LOG_HISTORY_LIMIT: int = int(os.getenv("LOG_HISTORY_LIMIT", "500"))