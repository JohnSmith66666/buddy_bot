"""
services/tautulli_service.py

Handles all communication with the Tautulli API.

CHANGES vs previous version:
  - get_user_watch_stats() now fires the two get_home_stats calls (top_movies
    + top_tv) in parallel via asyncio.gather — cuts latency roughly in half.
  - get_user_history() now accepts an optional `query` parameter and filters
    results client-side when provided (Tautulli has no server-side title filter
    on the history endpoint).

Korrekte parametre (bekræftet via debug-logs 2026-04-23):
- get_user_watch_time_stats : bruger `query_days` + user_id
- get_home_stats med user_id : bruger `time_range` + user_id + stats_count
- get_home_stats uden user_id: bruger `time_range` + stats_count (server-wide)
- get_recently_added        : bruger `count`
- Tid returneres i SEKUNDER — skal divideres med 3600 for timer, 60 for minutter.
- added_at returneres som Unix timestamp — konverteres til ISO-dato her.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

import config

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
    """Resolves a Plex username to a Tautulli user_id."""
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

    FIX: top_movies and top_tv are now fetched in parallel with asyncio.gather,
    cutting the latency of this function roughly in half.
    """
    user_id = await get_tautulli_user_id(plex_username)
    if user_id is None:
        logger.error("Cannot fetch stats: user_id not resolved for '%s'.", plex_username)
        return None

    # Fire all three requests concurrently.
    watch_time_raw, top_movies_raw, top_tv_raw = await asyncio.gather(
        _tautulli_get({
            "cmd": "get_user_watch_time_stats",
            "user_id": user_id,
            "query_days": query_days,
        }),
        _tautulli_get({
            "cmd": "get_home_stats",
            "user_id": user_id,
            "time_range": query_days,
            "stats_count": 5,
            "stat_id": "top_movies",
        }),
        _tautulli_get({
            "cmd": "get_home_stats",
            "user_id": user_id,
            "time_range": query_days,
            "stats_count": 5,
            "stat_id": "top_tv",
        }),
    )

    # Convert raw seconds to hours/minutes.
    watch_time_data = None
    if watch_time_raw:
        watch_time_data = []
        for entry in watch_time_raw:
            total_seconds = entry.get("total_time", 0) or 0
            hours   = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            watch_time_data.append({
                **entry,
                "total_time_hours":   hours,
                "total_time_minutes": minutes,
                "total_time":         hours,
            })

    return {
        "watch_time_stats": watch_time_data,
        "top_movies":       _extract_rows(top_movies_raw, "top_movies"),
        "top_tv":           _extract_rows(top_tv_raw, "top_tv"),
    }


def _extract_rows(data, stat_id: str) -> list | None:
    """Udtræk rows fra get_home_stats response."""
    if not data:
        return None
    if isinstance(data, list):
        for block in data:
            if stat_id in (block.get("stat_id") or ""):
                return block.get("rows") or []
        if data:
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
# Recently added
# ---------------------------------------------------------------------------

async def get_recently_added(count: int = 10) -> dict | None:
    """
    Returns the most recently added content on the Plex server.

    added_at fra Tautulli er et Unix timestamp (sekunder siden 1970).
    Vi konverterer det til ISO-dato og tilføjer added_at_readable.
    """
    data = await _tautulli_get({
        "cmd": "get_recently_added",
        "count": count,
    })

    if not data:
        logger.error("get_recently_added: Tautulli returnerede ingen data")
        return None

    logger.info(
        "get_recently_added raw: type=%s, keys=%s, sample=%s",
        type(data).__name__,
        list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]",
        str(data[0])[:150] if isinstance(data, list) and data else
        str({k: v for k, v in list(data.items())[:3]}) if isinstance(data, dict) else "tom",
    )

    items = []
    if isinstance(data, dict):
        items = (
            data.get("recently_added")
            or data.get("data")
            or data.get("results")
            or []
        )
        if not items:
            logger.warning("get_recently_added: dict uden kendte nøgler. Keys: %s", list(data.keys()))
    elif isinstance(data, list):
        items = data

    logger.info("get_recently_added: %d elementer fundet efter parsing", len(items))

    now = datetime.now(timezone.utc)
    movies   = []
    episodes = []

    for item in items:
        media_type = item.get("media_type", "")

        added_at_raw     = item.get("added_at")
        added_at_iso     = None
        added_at_readable = None
        days_ago         = None
        try:
            if added_at_raw:
                dt = datetime.fromtimestamp(int(added_at_raw), tz=timezone.utc)
                added_at_iso      = dt.strftime("%Y-%m-%d")
                added_at_readable = dt.strftime("%-d. %B %Y")
                days_ago          = (now - dt).days
        except Exception:
            pass

        base = {
            "title":             item.get("title") or item.get("full_title") or "Ukendt",
            "year":              item.get("year"),
            "added_at":          added_at_iso,
            "added_at_readable": added_at_readable,
            "days_ago":          days_ago,
            "rating_key":        item.get("rating_key"),
            "media_type":        media_type,
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

    result = {
        "movies":     movies[:count],
        "episodes":   episodes[:count],
        "total":      len(movies) + len(episodes),
        "fetched_at": now.strftime("%Y-%m-%d"),
    }

    if result["total"] == 0 and items:
        logger.warning(
            "get_recently_added: %d items modtaget men 0 parset. Første item: %s",
            len(items), str(items[0])[:200],
        )
        result["raw_sample"] = str(items[0])[:300] if items else None

    return result


# ---------------------------------------------------------------------------
# Currently playing (live activity)
# ---------------------------------------------------------------------------

async def get_activity() -> dict | None:
    """Returns the current playback activity on the Plex server."""
    return await _tautulli_get({"cmd": "get_activity"})


# ---------------------------------------------------------------------------
# Recent history for a user
# ---------------------------------------------------------------------------

async def get_user_history(
    plex_username: str,
    length: int = 10,
    query: str | None = None,
) -> list | None:
    """
    Returns the most recent watch history entries for a user.

    FIX: Accepts an optional `query` parameter. When provided, results are
    filtered client-side (Tautulli's /get_history endpoint has no title filter).
    We fetch a larger batch when filtering to increase the chance of matches.
    """
    user_id = await get_tautulli_user_id(plex_username)
    if user_id is None:
        return None

    fetch_length = max(length, 50) if query else length

    data = await _tautulli_get({
        "cmd": "get_history",
        "user_id": user_id,
        "length": fetch_length,
    })

    rows: list = []
    if data and isinstance(data, dict):
        rows = data.get("data", [])
    elif isinstance(data, list):
        rows = data

    if query:
        q = query.lower()
        rows = [
            r for r in rows
            if q in (r.get("title") or "").lower()
            or q in (r.get("grandparent_title") or "").lower()
            or q in (r.get("full_title") or "").lower()
        ]

    return rows[:length]