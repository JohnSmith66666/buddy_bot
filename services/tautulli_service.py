"""
services/tautulli_service.py - Tautulli API integration.

Provides three public functions:
  - get_popular_on_plex()      → top 10 film + top 10 serier målt på unikke brugere
  - get_user_watch_stats()     → personlig statistik for en specifik Plex-bruger
  - get_user_history()         → afspilningshistorik for en specifik Plex-bruger

Alle requests er async via httpx.
Brugernavn → Tautulli user_id opslag sker automatisk inden bruger-specifikke kald.

TOKEN-DIÆT:
  Kun de felter AI'en har brug for returneres.
  Kunstnere, thumbnails, filstier og andre tunge felter strippes.
"""

import logging

import httpx

from config import TAUTULLI_API_KEY, TAUTULLI_URL

logger = logging.getLogger(__name__)

_MAX_ITEMS = 10   # Hård grænse på alle lister sendt til AI'en.


# ── Internal helpers ──────────────────────────────────────────────────────────

def _base() -> str:
    return TAUTULLI_URL.rstrip("/") + "/api/v2"


def _params(cmd: str, **kwargs) -> dict:
    """Build a Tautulli API parameter dict."""
    return {"apikey": TAUTULLI_API_KEY, "cmd": cmd, **kwargs}


async def _get(cmd: str, **kwargs) -> dict | None:
    """
    Execute a GET request against the Tautulli API.
    Returns the parsed 'data' payload or None on error.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(_base(), params=_params(cmd, **kwargs))
            resp.raise_for_status()
            body = resp.json()

            if body.get("response", {}).get("result") != "success":
                logger.error("Tautulli API error for cmd=%s: %s", cmd, body)
                return None

            return body["response"]["data"]

        except httpx.HTTPError as e:
            logger.error("Tautulli HTTP error (cmd=%s): %s", cmd, e)
            return None


async def _resolve_user_id(plex_username: str) -> int | None:
    """
    Look up the Tautulli user_id for a given Plex username.

    Matches against friendly_name, username and email (case-insensitive).
    Returns None if the user is not found.
    """
    data = await _get("get_users")
    if not data:
        return None

    norm = plex_username.strip().lower()

    for user in data:
        candidates = {
            (user.get("friendly_name") or "").lower(),
            (user.get("username") or "").lower(),
            (user.get("email") or "").lower(),
        }
        if norm in candidates:
            uid = user.get("user_id")
            logger.debug("Tautulli user_id=%s for plex_username=%r", uid, plex_username)
            return uid

    logger.warning("No Tautulli user found for plex_username=%r", plex_username)
    return None


def _minutes_to_human(minutes: int) -> str:
    """Convert a minute count to a readable Danish string."""
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    mins  = minutes % 60
    if mins == 0:
        return f"{hours} t"
    return f"{hours} t {mins} min"


# ── Public functions ──────────────────────────────────────────────────────────

async def get_popular_on_plex(days: int = 7) -> dict:
    """
    Hent de mest populære film og serier på Plex-serveren.

    Popularitet måles på 'users_watched' (antal UNIKKE brugere) —
    IKKE total_plays, så en person der binge-watcher ikke forvrænger listen.

    Args:
        days: Antal dage der kigges tilbage (standard: 7).

    Returns:
        dict med 'top_10_movies' og 'top_10_tv' — aldrig blandet.
    """
    data = await _get("get_home_stats", time_range=days, stats_count=30)
    if not data:
        return {"error": "Kunne ikke hente statistik fra Tautulli."}

    top_movies: list[dict] = []
    top_tv:     list[dict] = []

    for stat_block in data:
        stat_id = stat_block.get("stat_id", "")
        rows    = stat_block.get("rows", [])

        # Tautulli returns multiple stat blocks — we only want 'popular_movies'
        # and 'popular_tv'. Sort each by users_watched descending.
        if stat_id == "popular_movies" and not top_movies:
            sorted_rows = sorted(rows, key=lambda r: r.get("users_watched", 0), reverse=True)
            for item in sorted_rows[:_MAX_ITEMS]:
                # Kun titel og år — ingen tal af hensyn til privatlivet.
                top_movies.append({
                    "title": item.get("title") or item.get("grandparent_title") or "Ukendt",
                    "year":  item.get("year"),
                })

        elif stat_id == "popular_tv" and not top_tv:
            sorted_rows = sorted(rows, key=lambda r: r.get("users_watched", 0), reverse=True)
            for item in sorted_rows[:_MAX_ITEMS]:
                # Kun titel — serier har sjældent year på dette niveau.
                top_tv.append({
                    "title": item.get("grandparent_title") or item.get("title") or "Ukendt",
                })

    if not top_movies and not top_tv:
        logger.warning(
            "get_home_stats returned no popular_movies/popular_tv blocks. "
            "Available stat_ids: %s",
            [b.get("stat_id") for b in data],
        )

    return {
        "period_days":   days,
        "top_10_movies": top_movies,
        "top_10_tv":     top_tv,
    }


async def get_user_watch_stats(
    plex_username: str,
    days: int | None = None,
) -> dict:
    """
    Hent personlig Plex-statistik for en specifik bruger.

    Args:
        plex_username: Brugerens Plex-brugernavn.
        days:          Antal dage der kigges tilbage.
                       Udelad (None) for all-time statistik.

    Returns:
        dict med total spilletid, antal afspilninger og mest sete genre.
    """
    user_id = await _resolve_user_id(plex_username)
    if user_id is None:
        return {"error": f"Brugeren '{plex_username}' blev ikke fundet i Tautulli."}

    params: dict = {"user_id": user_id}
    if days is not None:
        params["query_days"] = days

    data = await _get("get_user_watch_time_stats", **params)
    if not data:
        return {"error": "Kunne ikke hente brugerstatistik fra Tautulli."}

    # get_user_watch_time_stats returns a list of rows per query_days bucket.
    # We pick the first (and usually only) bucket that matches our request.
    stats = data[0] if isinstance(data, list) and data else data

    total_minutes = int(stats.get("total_time", 0)) // 60
    total_plays   = int(stats.get("total_plays", 0))

    result: dict = {
        "plex_username":   plex_username,
        "period":          f"sidste {days} dage" if days else "all time",
        "total_time":      _minutes_to_human(total_minutes),
        "total_plays":     total_plays,
    }

    # Fetch platform breakdown as a nice bonus — slim version only.
    platform_data = await _get("get_plays_by_source_resolution",
                               user_id=user_id,
                               **({"time_range": days} if days else {}))
    if platform_data and isinstance(platform_data, dict):
        categories = platform_data.get("categories", [])
        series     = platform_data.get("series", [])
        if categories and series:
            result["top_platforms"] = [
                {"platform": cat, "plays": s.get("data", [0])[0]}
                for cat, s in zip(categories[:3], series[:3])
                if s.get("data")
            ]

    return result


async def get_user_history(
    plex_username: str,
    query: str | None = None,
) -> dict:
    """
    Søg i en brugers afspilningshistorik.

    Args:
        plex_username: Brugerens Plex-brugernavn.
        query:         Valgfri titelsøgning. Udelad for de seneste afspilninger.

    Returns:
        dict med en liste af seneste afspilninger (maks 20).
    """
    user_id = await _resolve_user_id(plex_username)
    if user_id is None:
        return {"error": f"Brugeren '{plex_username}' blev ikke fundet i Tautulli."}

    params: dict = {
        "user_id":  user_id,
        "length":   20,
        "order_column": "date",
        "order_dir":    "desc",
    }
    if query:
        params["search"] = query

    data = await _get("get_history", **params)
    if not data:
        return {"error": "Kunne ikke hente historik fra Tautulli."}

    raw_items = data.get("data", []) if isinstance(data, dict) else []

    items = []
    for item in raw_items[:20]:
        media_type = item.get("media_type", "unknown")

        entry: dict = {
            "date":       item.get("date", ""),          # Unix timestamp as string
            "title":      _resolve_title(item),
            "media_type": media_type,
            "duration":   _minutes_to_human(int(item.get("duration", 0)) // 60),
            "percent_complete": item.get("percent_complete", 0),
        }

        # For episodes, add show context.
        if media_type == "episode":
            entry["show"]    = item.get("grandparent_title")
            entry["season"]  = item.get("parent_media_index")
            entry["episode"] = item.get("media_index")

        items.append(entry)

    return {
        "plex_username": plex_username,
        "query":         query or None,
        "count":         len(items),
        "history":       items,
    }


# ── Private helpers ───────────────────────────────────────────────────────────

def _resolve_title(item: dict) -> str:
    """Pick the best human-readable title from a Tautulli history item."""
    # For episodes the item title is the episode name — use grandparent instead.
    if item.get("media_type") == "episode":
        return item.get("grandparent_title") or item.get("title") or "Ukendt"
    return item.get("title") or item.get("grandparent_title") or "Ukendt"