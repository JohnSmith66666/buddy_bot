"""
services/tautulli_service.py

Handles all communication with the Tautulli API.

Key rules (from TAUTULLI_API_RULES.md):
- Use `query_days` for time filtering on get_user_stats and get_user_watch_time_stats.
- Use `time_range` ONLY for get_home_stats (the one exception).
- Never use `time_range` for get_user_stats → causes 400 Bad Request.
- Personal stats are fully accessible; server-wide aggregates must be stripped before sending to AI.
"""

import logging
import config  # Direct module import — avoids 'cannot import name config' error
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

            # Tautulli wraps everything in {"response": {"result": "success", "data": ...}}
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

async def get_user_watch_stats(plex_username: str, query_days: int = 30) -> dict | None:
    """
    Returns a combined personal statistics payload for a single user:
      - watch_time_stats : total duration and play counts (from get_user_watch_time_stats)
      - top_movies       : top 5 movies watched by this user (from get_user_stats)
      - top_tv           : top 5 TV shows watched by this user (from get_user_stats)

    Args:
        plex_username : The user's Plex username stored in the bot database.
        query_days    : Number of days to look back (default 30).

    Returns:
        A dict with keys watch_time_stats, top_movies, top_tv — or None if user not found.

    NOTE: Uses `query_days` (NOT `time_range`) for all calls except get_home_stats.
    """
    user_id = await get_tautulli_user_id(plex_username)
    if user_id is None:
        logger.error("Cannot fetch stats: user_id not resolved for '%s'.", plex_username)
        return None

    # --- 1. Watch time and play count totals ---
    # Command: get_user_watch_time_stats → requires query_days + user_id
    watch_time_data = await _tautulli_get({
        "cmd": "get_user_watch_time_stats",
        "user_id": user_id,
        "query_days": query_days,
    })

    # --- 2. Top 5 movies for this user ---
    # Command: get_user_stats → requires query_days (NOT time_range!) + stat_id + count
    top_movies_data = await _tautulli_get({
        "cmd": "get_user_stats",
        "user_id": user_id,
        "stat_id": "top_movies",
        "query_days": query_days,
        "count": 5,
    })

    # --- 3. Top 5 TV shows for this user ---
    # Command: get_user_stats → same rules as above, different stat_id
    top_tv_data = await _tautulli_get({
        "cmd": "get_user_stats",
        "user_id": user_id,
        "stat_id": "top_tv",
        "query_days": query_days,
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

async def get_popular_on_plex(stats_count: int = 10, time_range: int = 30) -> dict | None:
    """
    Returns the most popular content on the Plex server globally.

    Privacy rule: strips users_watched, total_plays, and total_duration
    before returning, so Claude only receives ordered titles + years.

    NOTE: get_home_stats is the ONE command that correctly uses `time_range`.
    """
    data = await _tautulli_get({
        "cmd": "get_home_stats",
        "time_range": time_range,   # <-- Correct for THIS command only
        "stats_count": stats_count,
    })

    if not data:
        return None

    # Strip aggregate fields to protect other users' privacy
    _STRIP_FIELDS = {"users_watched", "total_plays", "total_duration"}

    def _clean_rows(rows: list) -> list:
        cleaned = []
        for row in rows:
            cleaned.append({k: v for k, v in row.items() if k not in _STRIP_FIELDS})
        return cleaned

    cleaned_stats = []
    for stat_block in data:
        rows = stat_block.get("rows", [])
        stat_block["rows"] = _clean_rows(rows)
        cleaned_stats.append(stat_block)

    return cleaned_stats


# ---------------------------------------------------------------------------
# Currently playing (live activity)
# ---------------------------------------------------------------------------

async def get_activity() -> dict | None:
    """
    Returns the current playback activity on the Plex server.
    Useful for answering 'Is anyone watching something right now?'
    """
    return await _tautulli_get({"cmd": "get_activity"})


# ---------------------------------------------------------------------------
# Recent history for a user
# ---------------------------------------------------------------------------

async def get_user_history(plex_username: str, length: int = 10) -> list | None:
    """
    Returns the most recent watch history entries for a user.

    Args:
        plex_username : Plex username.
        length        : Number of history entries to return (default 10).
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