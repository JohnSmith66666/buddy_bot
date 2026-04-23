"""
services/tautulli_service.py

Handles all communication with the Tautulli API.

Korrekte parametre (bekræftet via debug-logs 2026-04-23):
- get_user_watch_time_stats : bruger `query_days` + user_id
- get_home_stats med user_id : bruger `time_range` + user_id + stats_count (personlige toplister)
- get_home_stats uden user_id: bruger `time_range` + stats_count (server-wide trends)
- get_recently_added        : bruger `count`
- Tid returneres i SEKUNDER — skal divideres med 3600 for timer, 60 for minutter.
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
      - watch_time_stats : total seertid i timer/minutter + antal afspilninger
      - top_movies       : top 5 film set af denne bruger
      - top_tv           : top 5 serier set af denne bruger
    """
    user_id = await get_tautulli_user_id(plex_username)
    if user_id is None:
        logger.error("Cannot fetch stats: user_id not resolved for '%s'.", plex_username)
        return None

    # --- 1. Watch time og play count totals ---
    watch_time_raw = await _tautulli_get({
        "cmd": "get_user_watch_time_stats",
        "user_id": user_id,
        "query_days": query_days,
    })

    # FIX: Tautulli returnerer total_time i SEKUNDER → konverter til timer/minutter
    watch_time_data = None
    if watch_time_raw:
        watch_time_data = []
        for entry in watch_time_raw:
            total_seconds = entry.get("total_time", 0) or 0
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            watch_time_data.append({
                **entry,
                "total_time_hours": hours,
                "total_time_minutes": minutes,
                "total_time": hours,
            })

    # --- 2. Top 5 film for denne bruger ---
    top_movies_raw = await _tautulli_get({
        "cmd": "get_home_stats",
        "user_id": user_id,
        "time_range": query_days,
        "stats_count": 5,
        "stat_id": "top_movies",
    })
    top_movies_data = _extract_rows(top_movies_raw, "top_movies")

    # --- 3. Top 5 serier for denne bruger ---
    top_tv_raw = await _tautulli_get({
        "cmd": "get_home_stats",
        "user_id": user_id,
        "time_range": query_days,
        "stats_count": 5,
        "stat_id": "top_tv",
    })
    top_tv_data = _extract_rows(top_tv_raw, "top_tv")

    return {
        "watch_time_stats": watch_time_data,
        "top_movies": top_movies_data,
        "top_tv": top_tv_data,
    }


def _extract_rows(data, stat_id: str) -> list | None:
    """Udtræk rows fra get_home_stats response."""
    if not data:
        return None

    if isinstance(data, list):
        for block in data:
            if stat_id in (block.get("stat_id") or ""):
                return block.get("rows") or []
        if len(data) > 0:
            rows = data[0].get("rows")
            if rows is not None:
                return rows

    if isinstance(data, dict):
        return data.get("rows") or []

    return None


# ---------------------------------------------------------------------------
# Server-wide / global trends
# ---------------------------------------------------------------------------

async def get_popular_on_plex(stats_count: int = 10, time_range: int = 30) -> dict | None:
    """
    Returns the most popular content on the Plex server globally.
    Strips users_watched, total_plays og total_duration før data sendes til AI.

    Args:
        stats_count : Antal resultater per kategori (default 10).
        time_range  : Antal dage der kigges tilbage (default 30).
                      Videregives direkte fra Buddy — fx 7 for 'denne uge'.
    """
    data = await _tautulli_get({
        "cmd": "get_home_stats",
        "time_range": time_range,   # Bruger den faktiske værdi fra Buddy — ikke hardcoded!
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
# Recently added
# ---------------------------------------------------------------------------

async def get_recently_added(count: int = 10) -> dict | None:
    """
    Returns the most recently added content on the Plex server.
    Adskiller film og serier i to separate lister for nem præsentation.
    """
    data = await _tautulli_get({
        "cmd": "get_recently_added",
        "count": count,
    })

    if not data:
        return None

    items = []
    if isinstance(data, dict):
        items = data.get("recently_added", [])
    elif isinstance(data, list):
        items = data

    movies = []
    episodes = []

    for item in items:
        media_type = item.get("media_type", "")
        base = {
            "title":      item.get("title") or item.get("full_title") or "Ukendt",
            "year":       item.get("year"),
            "added_at":   item.get("added_at"),
            "rating_key": item.get("rating_key"),
            "thumb":      item.get("thumb"),
            "media_type": media_type,
        }

        if media_type == "movie":
            movies.append(base)
        elif media_type in ("episode", "show"):
            base["grandparent_title"] = item.get("grandparent_title") or item.get("title")
            base["season"]  = item.get("parent_media_index") or item.get("season")
            base["episode"] = item.get("media_index") or item.get("episode")
            episodes.append(base)
        else:
            if item.get("grandparent_title"):
                base["grandparent_title"] = item.get("grandparent_title")
                episodes.append(base)
            else:
                movies.append(base)

    return {
        "movies":   movies[:count],
        "episodes": episodes[:count],
        "total":    len(movies) + len(episodes),
    }


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
    """Returns the most recent watch history entries for a user."""
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