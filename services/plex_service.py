"""
services/plex_service.py - Plex Media Server integration via python-plexapi.

Used to check whether a title already exists in the Plex library before
sending a download request via Seerr. PlexAPI calls are synchronous so
we run them in a thread pool to avoid blocking the async event loop.
"""

import asyncio
import logging
from functools import partial

from plexapi.exceptions import NotFound, Unauthorized
from plexapi.server import PlexServer

from config import PLEX_TOKEN, PLEX_URL

logger = logging.getLogger(__name__)

# ── Status constants ──────────────────────────────────────────────────────────

STATUS_FOUND   = "found"
STATUS_MISSING = "missing"
STATUS_ERROR   = "error"

# ── Plex library section names ────────────────────────────────────────────────
# Adjust these if your Plex library sections have different names.

_MOVIE_SECTIONS = ("Movies", "Film", "Animationsfilm", "Danske film")
_TV_SECTIONS    = ("TV Shows", "Serier", "TV Programmer")


# ── Sync helper (runs in thread pool) ────────────────────────────────────────

def _check_sync(title: str, year: int | None, media_type: str) -> dict:
    """
    Synchronous Plex library lookup. Called via asyncio.to_thread().

    Searches all relevant library sections and matches on title + year.
    Year matching uses a ±1 window to handle release date discrepancies
    between TMDB and Plex metadata agents.

    Args:
        title:      The title to search for.
        year:       Release year from TMDB (None = skip year check).
        media_type: "movie" or "tv".

    Returns:
        dict with keys: status, title (if found), year (if found), library.
    """
    try:
        plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=10)
    except Unauthorized:
        logger.error("Plex auth failed — check PLEX_TOKEN")
        return {"status": STATUS_ERROR, "message": "Ugyldig Plex-token."}
    except Exception as e:
        logger.error("Plex connection error: %s", e)
        return {"status": STATUS_ERROR, "message": f"Kunne ikke forbinde til Plex: {e}"}

    target_sections = _MOVIE_SECTIONS if media_type == "movie" else _TV_SECTIONS

    for section_name in target_sections:
        try:
            section = plex.library.section(section_name)
        except NotFound:
            continue  # This section doesn't exist on this server — skip it.
        except Exception as e:
            logger.warning("Could not access Plex section '%s': %s", section_name, e)
            continue

        try:
            results = section.search(title=title)
        except Exception as e:
            logger.warning("Plex search error in section '%s': %s", section_name, e)
            continue

        for item in results:
            item_title = getattr(item, "title", "") or ""
            item_year  = getattr(item, "year",  None)

            # Title match: case-insensitive exact match.
            if item_title.lower() != title.lower():
                continue

            # Year match: if we have a year, allow ±1 tolerance.
            if year and item_year:
                if abs(item_year - year) > 1:
                    continue

            logger.info(
                "Plex HIT: '%s' (%s) in section '%s'",
                item_title, item_year, section_name,
            )
            return {
                "status": STATUS_FOUND,
                "title": item_title,
                "year": item_year,
                "library": section_name,
            }

    return {"status": STATUS_MISSING}


# ── Public async function ─────────────────────────────────────────────────────

async def check_library(
    title: str,
    year: int | None,
    media_type: str,
) -> dict:
    """
    Async wrapper around the synchronous Plex lookup.

    Runs the blocking PlexAPI call in a thread pool so it doesn't block
    the Telegram bot's event loop.

    Args:
        title:      Title to look up (from TMDB).
        year:       Release year from TMDB. Pass None if unknown.
        media_type: "movie" or "tv".

    Returns:
        dict with:
          status   → "found" | "missing" | "error"
          title    → matched Plex title (if found)
          year     → matched Plex year (if found)
          library  → Plex section name (if found)
          message  → error description (if error)
    """
    try:
        result = await asyncio.to_thread(
            partial(_check_sync, title=title, year=year, media_type=media_type)
        )
        return result
    except Exception as e:
        logger.error("Unexpected error in check_library: %s", e)
        return {"status": STATUS_ERROR, "message": f"Uventet fejl: {e}"}