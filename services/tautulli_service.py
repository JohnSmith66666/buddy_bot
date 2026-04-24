"""
services/tautulli_service.py

Handles all communication with the Tautulli API.

CHANGES vs previous version:
  - get_user_watch_stats() og get_popular_on_plex() understøtter nu all-time:
    Hvis query_days/time_range er 0 eller None, udelades time_range-parameteren
    helt fra API-kaldet. Tautulli returnerer dermed statistik for al tid.
    Dette løser problemet med 'absolut mest sete nogensinde' der returnerede
    kun 30 dage.
  - get_user_history() rettet:
    Bruger nu korrekt Tautulli-parameter `user` (ikke `user_id`) til
    brugerfiltrering på get_history-endpoint.
    Fjernet tidsbegrænsning (length øget til 100 ved ingen query).
    Parsed korrekt: response['response']['data']['data'] arrayet.
    Tilføjet media_type=movie parameter til film-specifik historik.
  - Alle tidsbegrænsede kald bruger query_days til get_user_watch_time_stats
    og time_range til get_home_stats — konsistent med Tautulli API-dokumentation.

Korrekte parametre (bekræftet via debug-logs 2026-04-23 + 2026-04-24):
  - get_user_watch_time_stats: query_days + user_id
  - get_home_stats med user_id:  time_range + user_id + stats_count
    (udelad time_range for all-time)
  - get_history: user (brugernavn som string), media_type, length
  - Tid returneres i SEKUNDER — divider med 3600 for timer.
  - added_at returneres som Unix timestamp.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

import config

logger = logging.getLogger(__name__)

TAUTULLI_BASE = config.TAUTULLI_URL.rstrip("/")
API_KEY = config.TAUTULLI_API_KEY

# Sentinel-værdi der signalerer "alle tider" til interne funktioner
ALL_TIME = 0


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
      - watch_time_stats: total seertid i timer/minutter + antal afspilninger
      - top_movies:       top 5 film set af denne bruger
      - top_tv:           top 5 serier set af denne bruger

    FIX: query_days=0 betyder 'all time' — time_range udelades fra API-kaldet.
    Film og serier hentes parallelt med asyncio.gather.
    """
    user_id = await get_tautulli_user_id(plex_username)
    if user_id is None:
        logger.error("Cannot fetch stats: user_id not resolved for '%s'.", plex_username)
        return None

    # Byg home_stats params — udelad time_range ved all-time
    def _home_stats_params(stat_id: str) -> dict:
        p = {
            "cmd":         "get_home_stats",
            "user_id":     user_id,
            "stats_count": 5,
            "stat_id":     stat_id,
        }
        if query_days and query_days != ALL_TIME:
            p["time_range"] = query_days
        # query_days=0/None → udelad time_range → Tautulli returnerer all-time
        return p

    watch_time_raw, top_movies_raw, top_tv_raw = await asyncio.gather(
        _tautulli_get({
            "cmd":        "get_user_watch_time_stats",
            "user_id":    user_id,
            "query_days": query_days if query_days and query_days != ALL_TIME else 99999,
        }),
        _tautulli_get(_home_stats_params("top_movies")),
        _tautulli_get(_home_stats_params("top_tv")),
    )

    # Konverter sekunder til timer/minutter
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

    FIX: time_range=0 betyder 'all time' — parameteren udelades fra kaldet.
    Strips users_watched, total_plays og total_duration inden data sendes til AI.
    """
    params: dict = {
        "cmd":         "get_home_stats",
        "stats_count": stats_count,
    }
    if time_range and time_range != ALL_TIME:
        params["time_range"] = time_range
    # time_range=0/None → udelad → Tautulli returnerer all-time statistik

    data = await _tautulli_get(params)
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
    added_at fra Tautulli er et Unix timestamp — konverteres til ISO-dato.
    """
    data = await _tautulli_get({
        "cmd":   "get_recently_added",
        "count": count,
    })

    if not data:
        logger.error("get_recently_added: Tautulli returnerede ingen data")
        return None

    logger.info(
        "get_recently_added raw: type=%s, keys=%s",
        type(data).__name__,
        list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]",
    )

    items = []
    if isinstance(data, dict):
        items = (
            data.get("recently_added")
            or data.get("data")
            or data.get("results")
            or []
        )
    elif isinstance(data, list):
        items = data

    now = datetime.now(timezone.utc)
    movies   = []
    episodes = []

    for item in items:
        media_type = item.get("media_type", "")

        added_at_raw      = item.get("added_at")
        added_at_iso      = None
        added_at_readable = None
        days_ago          = None
        try:
            if added_at_raw:
                dt = datetime.fromtimestamp(int(added_at_raw), tz=timezone.utc)
                added_at_iso      = dt.strftime("%Y-%m-%d")
                added_at_readable = dt.strftime("%-d. %B %Y")
                days_ago          = (now - dt).days
        except Exception:
            pass

        tmdb_id = None
        for guid in item.get("guids", []):
            if isinstance(guid, str) and guid.startswith("tmdb://"):
                try:
                    tmdb_id = int(guid.replace("tmdb://", ""))
                except ValueError:
                    pass
                break

        base = {
            "title":             item.get("title") or item.get("full_title") or "Ukendt",
            "year":              item.get("year"),
            "added_at":          added_at_iso,
            "added_at_readable": added_at_readable,
            "days_ago":          days_ago,
            "tmdb_id":           tmdb_id,
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

    return {
        "movies":     movies[:count],
        "episodes":   episodes[:count],
        "total":      len(movies) + len(episodes),
        "fetched_at": now.strftime("%Y-%m-%d"),
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

async def get_user_history(
    plex_username: str,
    length: int = 25,
    query: str | None = None,
    media_type: str | None = None,
) -> list | None:
    """
    Returns the most recent watch history for a user.

    FIX 1: Bruger nu `user` parameter (brugernavn som string) i stedet for
    `user_id`. Tautulli's get_history endpoint accepterer `user` direkte
    og er mere pålidelig end user_id-baseret filtrering på dette endpoint.

    FIX 2: Parser korrekt: Tautulli returnerer
    response['response']['data']['data'] — det inderste 'data'-array
    indeholder de faktiske afspilningsposter. Den generelle _tautulli_get()
    returnerer allerede 'data'-niveauet, så vi henter .get('data') én gang til.

    FIX 3: Ingen tidsbegrænsning på kaldet — length øges til 100 ved
    titel-søgning for at øge chancen for at finde posten.

    FIX 4: Tilføjet media_type parameter (f.eks. 'movie') til filtrering
    direkte i API-kaldet, så Tautulli kun returnerer den ønskede type.
    """
    fetch_length = max(length, 100) if query else length

    params: dict = {
        "cmd":    "get_history",
        "user":   plex_username,   # FIX: 'user' ikke 'user_id' på dette endpoint
        "length": fetch_length,
    }

    # Tilføj media_type filter hvis angivet
    if media_type:
        params["media_type"] = media_type

    data = await _tautulli_get(params)

    # FIX: get_history returnerer {"draw": ..., "recordsTotal": ..., "data": [...]}
    # _tautulli_get() returnerer allerede det ydre 'data'-objekt,
    # så vi skal hente det inderste 'data'-array herfra.
    rows: list = []
    if data is None:
        logger.warning("get_user_history: Tautulli returnerede None for user='%s'", plex_username)
        return []

    if isinstance(data, dict):
        # Korrekt parsing: det inderste 'data'-array
        rows = data.get("data", [])
        if not rows:
            # Fallback: prøv andre kendte nøgler
            rows = data.get("rows", []) or data.get("results", [])
        logger.info(
            "get_user_history: parsed %d poster for user='%s' (dict-format)",
            len(rows), plex_username,
        )
    elif isinstance(data, list):
        rows = data
        logger.info(
            "get_user_history: parsed %d poster for user='%s' (list-format)",
            len(rows), plex_username,
        )

    if not rows:
        logger.warning(
            "get_user_history: tom historik for user='%s'. "
            "Rådata-keys: %s",
            plex_username,
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )

    # Titel-filtrering client-side (Tautulli har ingen server-side titelfilter)
    if query and rows:
        q = query.lower()
        rows = [
            r for r in rows
            if q in (r.get("title") or "").lower()
            or q in (r.get("grandparent_title") or "").lower()
            or q in (r.get("full_title") or "").lower()
        ]
        logger.info(
            "get_user_history: %d poster matchede query='%s'", len(rows), query
        )

    return rows[:length]