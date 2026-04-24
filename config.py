"""
config.py - Centralized configuration loaded from environment variables.
The application will raise an error at startup if any required variable is missing.

CHANGES vs previous version:
  - Tilføjet TAVILY_API_KEY (valgfri — mangler den, er web-søgning deaktiveret
    med en advarsel i web_service.py i stedet for en hård startup-fejl).
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
ANTHROPIC_MODEL: str   = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# ── TMDB ──────────────────────────────────────────────────────────────────────
TMDB_API_KEY: str = _require("TMDB_API_KEY")

# ── Plex ──────────────────────────────────────────────────────────────────────
PLEX_URL: str   = _require("PLEX_URL")
PLEX_TOKEN: str = _require("PLEX_TOKEN")

# ── Radarr ────────────────────────────────────────────────────────────────────
RADARR_URL: str               = _require("RADARR_URL")
RADARR_API_KEY: str           = _require("RADARR_API_KEY")
RADARR_QUALITY_PROFILE_ID:int = int(os.getenv("RADARR_QUALITY_PROFILE_ID", "1"))

# ── Sonarr ────────────────────────────────────────────────────────────────────
SONARR_URL: str               = _require("SONARR_URL")
SONARR_API_KEY: str           = _require("SONARR_API_KEY")
SONARR_QUALITY_PROFILE_ID:int = int(os.getenv("SONARR_QUALITY_PROFILE_ID", "1"))

# ── Media root folders ────────────────────────────────────────────────────────
ROOT_MOVIE_ANIMATION: str = "/mnt/unionfs/Media/Movies/Animation"
ROOT_MOVIE_STANDARD: str  = "/mnt/unionfs/Media/Movies/Film"
ROOT_TV_DANSK: str        = "/mnt/unionfs/Media/TV/TV"
ROOT_TV_STANDARD: str     = "/mnt/unionfs/Media/TV/Serier"

# ── Tautulli ──────────────────────────────────────────────────────────────────
TAUTULLI_URL:     str = _require("TAUTULLI_URL")
TAUTULLI_API_KEY: str = _require("TAUTULLI_API_KEY")

# ── Tavily (web-søgning) ──────────────────────────────────────────────────────
# Valgfri — hvis ikke sat deaktiveres web-søgning med en advarsel,
# men botten starter stadig normalt (ingen hård startup-fejl).
TAVILY_API_KEY: str | None = os.getenv("TAVILY_API_KEY") or None

# ── Optional / runtime settings ───────────────────────────────────────────────
ENVIRONMENT: str       = os.getenv("ENVIRONMENT", "dev")
LOG_HISTORY_LIMIT: int = int(os.getenv("LOG_HISTORY_LIMIT", "500"))