"""
services/tautulli_service.py

Handles all communication with the Tautulli API.

CHANGES vs previous version:
  - get_recently_added() fix 1 — Serie-titler:
    Sæsoner og episoder vises nu som "Taskmaster - Season 14" eller
    "Severance - S2E01 - Title" i stedet for bare "Season 14" / "Episode 3".
    Logik: grandparent_title (serienavn) > parent_title (sæsonnavn) > title.
    Helper-funktionen _format_show_title() håndterer alle kombinationer.

  - get_recently_added() fix 2 — Læsbare tidsstempler:
    added_at konverteres fra Unix timestamp til tre formater:
      added_at_iso:      "2026-04-06"            (maskinvenligt, sorterbart)
      added_at_readable: "6. april 2026 kl. 21:15" (menneskevenligt, dansk)
      days_ago:          18                       (til relative udsagn)
    LLM'en kan nu let svare på "hvad landede i går?" uden at lave
    matematik på millioner-tal.

  - get_user_watch_stats() og get_popular_on_plex():
    query_days/time_range=0 → udelader time_range fra API-kaldet (all-time).

  - get_user_history():
    Bruger 'user' parameter (brugernavn) fremfor 'user_id'.
    Parser korrekt: response['data']['data'] arrayet.
    Understøtter media_type filter.
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

# Danske måneds-navne til get_recently_added readable-format
_DK_MONTHS = {
    1: "januar", 2: "februar", 3: "marts", 4: "april",
    5: "maj", 6: "juni", 7: "juli", 8: "august",
    9: "september", 10: "oktober", 11: "november", 12: "december",
}


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _parse_unix_timestamp(raw: str | int | None) -> datetime | None:
    """Konvertér Unix timestamp (sekunder) til timezone-aware datetime. Returnerer None ved fejl."""
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, OSError, OverflowError) as e:
        logger.debug("_parse_unix_timestamp: ugyldigt timestamp '%s': %s", raw, e)
        return None


def _format_added_at(raw: str | int | None, now: datetime) -> dict:
    """
    Konvertér Unix timestamp til tre nyttige formater.

    Returnerer:
      {
        "added_at_iso":      "2026-04-06",              # til sortering
        "added_at_readable": "6. april 2026 kl. 21:15", # til LLM
        "days_ago":          18,                         # til relative svar
      }
    Alle felter er None hvis timestamp mangler eller er ugyldigt.
    """
    dt = _parse_unix_timestamp(raw)
    if dt is None:
        return {"added_at_iso": None, "added_at_readable": None, "days_ago": None}

    days_ago = (now - dt).days
    month_dk = _DK_MONTHS.get(dt.month, str(dt.month))
    readable  = f"{dt.day}. {month_dk} {dt.year} kl. {dt.strftime('%H:%M')}"

    return {
        "added_at_iso":      dt.strftime("%Y-%m-%d"),
        "added_at_readable": readable,
        "days_ago":          days_ago,
    }


# ── Title helpers ─────────────────────────────────────────────────────────────

def _format_show_title(item: dict) -> str:
    """
    Byg en læsbar visningstittel for TV-indhold.

    Prioritetsorden for serienavn:
      grandparent_title → parent_title → title

    Kombineres med den specifikke episode/sæson-titel:

    Eksempler:
      media_type=episode:
        grandparent="Severance", parent="Season 2", title="Chikhai Bardo"
        → "Severance - S2E01: Chikhai Bardo"

        grandparent="Taskmaster", parent="Season 14", title="Season 14"
        (title == parent → undgå duplikat)
        → "Taskmaster - Season 14"

      media_type=season:
        grandparent="The Bear", title="Season 3"
        → "The Bear - Season 3"

    Fallback: returnerer title som-er.
    """
    media_type       = (item.get("media_type") or "").lower()
    title            = (item.get("title") or "").strip()
    parent_title     = (item.get("parent_title") or "").strip()
    grandparent_title = (item.get("grandparent_title") or "").strip()

    # Film eller ukendt type — bare brug title
    if media_type == "movie" or (not grandparent_title and not parent_title):
        return title or "Ukendt"

    # Serienavn: brug grandparent_title hvis tilgængeligt, ellers parent_title
    show_name = grandparent_title or parent_title

    if media_type == "episode":
        # Forsøg at bygge S##E## præfiks fra media_index / parent_media_index
        season_num  = item.get("parent_media_index")
        episode_num = item.get("media_index")

        if season_num is not None and episode_num is not None:
            try:
                se_prefix = f"S{int(season_num):02d}E{int(episode_num):02d}"
                # Inkludér episode-titel hvis den adskiller sig fra parent_title
                if title and title.lower() != (parent_title or "").lower():
                    return f"{show_name} - {se_prefix}: {title}"
                else:
                    return f"{show_name} - {se_prefix}"
            except (ValueError, TypeError):
                pass

        # Fallback: brug parent_title som sæson-label
        if parent_title and parent_title.lower() != title.lower():
            return f"{show_name} - {parent_title}"
        return show_name

    if media_type == "season":
        # title er typisk "Season 14" — undgå duplikat med show_name
        if title and title.lower() != show_name.lower():
            return f"{show_name} - {title}"
        return show_name

    # Generelt TV-indhold
    if title and title.lower() != show_name.lower():
        return f"{show_name} - {title}"
    return show_name


# ── Tautulli API helper ───────────────────────────────────────────────────────

async def _tautulli_get(params: dict) -> dict | None:
    """
    Internal helper: performs a GET request to the Tautulli API.
    Always injects the API key. Returns the 'data' payload or None on error.
    """
    params["apikey"]        = API_KEY
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
    Returns combined personal statistics for a single user.
    query_days=0 → all-time (time_range udelades fra API-kaldet).
    """
    user_id = await get_tautulli_user_id(plex_username)
    if user_id is None:
        logger.error("Cannot fetch stats: user_id not resolved for '%s'.", plex_username)
        return None

    def _home_stats_params(stat_id: str) -> dict:
        p = {
            "cmd":         "get_home_stats",
            "user_id":     user_id,
            "stats_count": 5,
            "stat_id":     stat_id,
        }
        if query_days and query_days != ALL_TIME:
            p["time_range"] = query_days
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
    Returns most popular content server-wide.
    time_range=0 → all-time (udelader parameteren).
    """
    params: dict = {
        "cmd":         "get_home_stats",
        "stats_count": stats_count,
    }
    if time_range and time_range != ALL_TIME:
        params["time_range"] = time_range

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

    FIX 1 — Serie-titler:
      TV-indhold formateres nu som "Serienavn - S02E01: Episodetitel"
      eller "Serienavn - Season 3" i stedet for bare "Season 3" eller
      "Episode 1". Logik håndteres af _format_show_title().

    FIX 2 — Læsbare tidsstempler:
      added_at (Unix timestamp) konverteres til tre formater:
        added_at_iso:      "2026-04-06"              (sorterbart)
        added_at_readable: "6. april 2026 kl. 21:15" (menneskevenligt)
        days_ago:          18                         (til relative svar)
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

    # Udtræk råliste
    items: list = []
    if isinstance(data, dict):
        items = (
            data.get("recently_added")
            or data.get("data")
            or data.get("results")
            or []
        )
    elif isinstance(data, list):
        items = data

    now    = datetime.now(timezone.utc)
    movies   = []
    episodes = []

    for item in items:
        media_type = (item.get("media_type") or "").lower()

        # ── Tidsstempler ───────────────────────────────────────────────────────
        ts_data = _format_added_at(item.get("added_at"), now)

        # ── TMDB ID fra guids ─────────────────────────────────────────────────
        tmdb_id = None
        for guid in item.get("guids", []):
            if isinstance(guid, str) and guid.startswith("tmdb://"):
                try:
                    tmdb_id = int(guid.replace("tmdb://", ""))
                except ValueError:
                    pass
                break

        # ── Titler ────────────────────────────────────────────────────────────
        display_title = _format_show_title(item)

        base = {
            "display_title":   display_title,            # formateret til visning
            "title":           item.get("title") or "",  # rå titel (fallback)
            "year":            item.get("year"),
            "media_type":      media_type,
            "tmdb_id":         tmdb_id,
            **ts_data,                                   # added_at_iso, added_at_readable, days_ago
        }

        if media_type == "movie":
            movies.append(base)
        else:
            # TV: tilføj strukturelle felter til kontekst
            base["series_name"]  = item.get("grandparent_title") or item.get("parent_title") or ""
            base["season"]       = item.get("parent_media_index")
            base["episode"]      = item.get("media_index")
            base["season_title"] = item.get("parent_title") or ""
            episodes.append(base)

    logger.info(
        "get_recently_added: %d film, %d TV-indslag parseret",
        len(movies), len(episodes),
    )

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

    Bruger 'user' parameter (brugernavn som string).
    Parser korrekt: response['data']['data'] arrayet.
    Understøtter media_type filter ('movie' / 'episode').
    Tidsstempler konverteres via _format_added_at() for konsistens.
    """
    fetch_length = max(length, 100) if query else length

    params: dict = {
        "cmd":    "get_history",
        "user":   plex_username,
        "length": fetch_length,
    }
    if media_type:
        params["media_type"] = media_type

    data = await _tautulli_get(params)

    rows: list = []
    if data is None:
        logger.warning("get_user_history: Tautulli returnerede None for user='%s'", plex_username)
        return []

    if isinstance(data, dict):
        rows = data.get("data", [])
        if not rows:
            rows = data.get("rows", []) or data.get("results", [])
        logger.info(
            "get_user_history: %d poster for user='%s'", len(rows), plex_username
        )
    elif isinstance(data, list):
        rows = data

    if not rows:
        logger.warning(
            "get_user_history: tom historik for user='%s'. Rådata-keys: %s",
            plex_username,
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )

    # Titel-filtrering client-side
    if query and rows:
        q = query.lower()
        rows = [
            r for r in rows
            if q in (r.get("title") or "").lower()
            or q in (r.get("grandparent_title") or "").lower()
            or q in (r.get("full_title") or "").lower()
        ]

    # Berig historik-poster med display_title og læsbare tidsstempler
    now = datetime.now(timezone.utc)
    enriched = []
    for row in rows[:length]:
        ts_data = _format_added_at(row.get("date") or row.get("started") or row.get("added_at"), now)
        enriched.append({
            **row,
            "display_title": _format_show_title(row),
            **ts_data,
        })

    return enriched