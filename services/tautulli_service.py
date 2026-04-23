"""
services/tautulli_service.py

Handles all communication with the Tautulli API.

Korrekte parametre (bekræftet via debug-logs):
- get_user_watch_time_stats : bruger `query_days` + user_id
- get_user_stats            : bruger `time_range` + user_id + stat_id + count
- get_home_stats            : bruger `time_range` + stats_count
"""

import logging
import config
import httpx

logger = logging.getLogger(__name__)

TAUTULLI_BASE = config.TAUTULLI_URL.rstrip("/")
API_KEY = config.TAUTULLI_API_KEY


async def _tautulli_get(params: dict) -> dict | None:
    """
    Internal helper: performs a GET request to the Tautulli API.
    Always injects the API key. Returns the 'data' payload or None on error.
    """
    params["apikey"] = API_KEY
    params["output_format"] = "json"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.get(f"{TAUTULLI_BASE}/api/v2", params=params)
            response.raise_for_status()
            body = response.json()

            result = body.get("response", {})
            if result.get("result") != "success":
                logger.error("Tautulli returned non-success: %s", result)
                return None

            return result.get("data")

        except httpx.HTTPStatusError as e:
            logger.error("Tautulli HTTP error %s: %s", e.response.status_code, e)
            return None
        except Exception as e:
            logger.error("Tautulli unexpected error: %s", e)
            return None


# ---------------------------------------------------------------------------
# User resolution
# ---------------------------------------------------------------------------

async def get_tautulli_user_id(plex_username: str) -> int | None:
    """
    Resolves a Plex username to a Tautulli user_id.
    Must be called before any personal-stats endpoints.
    """
    data = await _tautulli_get({"cmd": "get_users"})
    if not data:
        return None

    for user in data:
        if user.get("username", "").lower() == plex_username.lower():
            return user.get("user_id")

    logger.warning("Plex username '%s' not found in Tautulli user list.", plex_username)
    return None


# ---------------------------------------------------------------------------
# Personal statistics
# ---------------------------------------------------------------------------

async def get_user_watch_stats(plex_username: str, query_days: int = 365) -> dict | None:
    """
    Returns a combined personal statistics payload for a single user:
      - watch_time_stats : total duration and play counts
      - top_movies       : top 5 movies watched by this user
      - top_tv           : top 5 TV shows watched by this user

    VIGTIGE parameter-regler (bekræftet via HTTP 400-logs):
      - get_user_watch_time_stats → bruger `query_days`
      - get_user_stats            → bruger `time_range` (IKKE query_days!)
    """
    user_id = await get_tautulli_user_id(plex_username)
    if user_id is None:
        logger.error("Cannot fetch stats: user_id not resolved for '%s'.", plex_username)
        return None

    # Konverter query_days til time_range for get_user_stats
    time_range = query_days

    # --- 1. Watch time og play count totals ---
    # Denne kommando bruger query_days — korrekt
    watch_time_data = await _tautulli_get({
        "cmd": "get_user_watch_time_stats",
        "user_id": user_id,
        "query_days": query_days,
    })

    # --- 2. Top 5 film for denne bruger ---
    # VIGTIGT: get_user_stats bruger time_range, IKKE query_days
    top_movies_data = await _tautulli_get({
        "cmd": "get_user_stats",
        "user_id": user_id,
        "stat_id": "top_movies",
        "time_range": time_range,
        "count": 5,
    })

    # --- 3. Top 5 serier for denne bruger ---
    # VIGTIGT: get_user_stats bruger time_range, IKKE query_days
    top_tv_data = await _tautulli_get({
        "cmd": "get_user_stats",
        "user_id": user_id,
        "stat_id": "top_tv",
        "time_range": time_range,
        "count": 5,
    })

    return {
        "watch_time_stats": watch_time_data,
        "top_movies": top_movies_data,
        "top_tv": top_tv_data,
    }


# ---------------------------------------------------------------------------
# Server-wide / global trends
# ---------------------------------------------------------------------------

async def get_popular_on_plex(stats_count: int = 10, time_range: int = 365) -> dict | None:
    """
    Returns the most popular content on the Plex server globally.
    Strips users_watched, total_plays og total_duration før data sendes til AI.
    NOTE: get_home_stats bruger time_range — korrekt for denne kommando.
    """
    data = await _tautulli_get({
        "cmd": "get_home_stats",
        "time_range": time_range,
        "stats_count": stats_count,
    })

    if not data:
        return None

    _STRIP_FIELDS = {"users_watched", "total_plays", "total_duration"}

    def _clean_rows(rows: list) -> list:
        return [{k: v for k, v in row.items() if k not in _STRIP_FIELDS} for row in rows]

    cleaned_stats = []
    for stat_block in data:
        stat_block["rows"] = _clean_rows(stat_block.get("rows", []))
        cleaned_stats.append(stat_block)

    return cleaned_stats


# ---------------------------------------------------------------------------
# Currently playing (live activity)
# ---------------------------------------------------------------------------

async def get_activity() -> dict | None:
    """Returns the current playback activity on the Plex server."""
    return await _tautulli_get({"cmd": "get_activity"})


# ---------------------------------------------------------------------------
# Recent history for a user
# ---------------------------------------------------------------------------

async def get_user_history(plex_username: str, length: int = 10) -> list | None:
    """
    Returns the most recent watch history entries for a user.
    """
    user_id = await get_tautulli_user_id(plex_username)
    if user_id is None:
        return None

    data = await _tautulli_get({
        "cmd": "get_history",
        "user_id": user_id,
        "length": length,
    })

    if data and isinstance(data, dict):
        return data.get("data", [])
    return data