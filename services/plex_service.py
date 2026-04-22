"""
services/plex_service.py - Plex Media Server integration via python-plexapi.

Dynamically scans all library sections on the server — no hardcoded names.
Uses fuzzy title matching to handle variations like 'Olsen-banden' vs 'Olsen Banden'.
PlexAPI calls are synchronous so we run them in a thread pool to avoid
blocking the async event loop.
"""

import asyncio
import logging
import re
import unicodedata
from functools import partial

from plexapi.exceptions import NotFound, Unauthorized
from plexapi.server import PlexServer

from config import PLEX_TOKEN, PLEX_URL

logger = logging.getLogger(__name__)

# ── Status constants ──────────────────────────────────────────────────────────

STATUS_FOUND   = "found"
STATUS_MISSING = "missing"
STATUS_ERROR   = "error"

# Plex library types we care about
_MOVIE_TYPE = "movie"
_TV_TYPE    = "show"


# ── Title normalisation ───────────────────────────────────────────────────────

def _normalise(title: str) -> str:
    """
    Normalise a title for fuzzy comparison.

    Steps:
      1. Unicode NFKD decomposition → strip combining characters (accents etc.)
      2. Lowercase
      3. Replace hyphens and underscores with a space
      4. Remove all non-alphanumeric characters except spaces
      5. Collapse multiple spaces into one and strip edges
      6. Remove common articles that differ between languages
         (the, a, an, den, det, en, et) when they appear at the start.

    Examples:
      'Olsen-banden'   → 'olsen banden'
      'Olsen Banden'   → 'olsen banden'
      'The Dark Knight' → 'dark knight'
      'Oppenheimer.'   → 'oppenheimer'
    """
    # Decompose unicode and drop combining marks
    nfkd = unicodedata.normalize("NFKD", title)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))

    # Lowercase
    s = ascii_str.lower()

    # Hyphens and underscores → space
    s = re.sub(r"[-_]", " ", s)

    # Keep only alphanumeric and spaces
    s = re.sub(r"[^a-z0-9\s]", "", s)

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Strip leading articles
    s = re.sub(r"^(the|a|an|den|det|en|et)\s+", "", s)

    return s


def _titles_match(title_a: str, title_b: str) -> bool:
    """Return True if two titles are equivalent after normalisation."""
    return _normalise(title_a) == _normalise(title_b)


# ── Sync helper (runs in thread pool) ────────────────────────────────────────

def _check_sync(title: str, year: int | None, media_type: str) -> dict:
    """
    Synchronous Plex library lookup across ALL sections dynamically.

    Args:
        title:      The title to search for (from TMDB).
        year:       Release year (None = skip year check).
        media_type: "movie" or "tv".

    Returns:
        dict with keys:
          status   → "found" | "missing" | "error"
          title    → matched Plex title (if found)
          year     → matched Plex year (if found)
          library  → Plex section name (if found)
          message  → error description (if error)
    """
    # Map our internal type to Plex section type strings
    plex_type = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE

    try:
        plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=10)
    except Unauthorized:
        logger.error("Plex auth failed — check PLEX_TOKEN")
        return {"status": STATUS_ERROR, "message": "Ugyldig Plex-token."}
    except Exception as e:
        logger.error("Plex connection error: %s", e)
        return {"status": STATUS_ERROR, "message": f"Kunne ikke forbinde til Plex: {e}"}

    # ── Dynamically fetch all sections and filter by type ─────────────────────
    try:
        all_sections = plex.library.sections()
    except Exception as e:
        logger.error("Could not fetch Plex sections: %s", e)
        return {"status": STATUS_ERROR, "message": f"Kunne ikke hente Plex-biblioteker: {e}"}

    relevant_sections = [s for s in all_sections if s.type == plex_type]

    if not relevant_sections:
        logger.warning("No Plex sections found for type '%s'", plex_type)
        return {"status": STATUS_MISSING}

    logger.debug(
        "Searching %d section(s) for '%s' (%s): %s",
        len(relevant_sections),
        title,
        media_type,
        [s.title for s in relevant_sections],
    )

    # ── Search each section ───────────────────────────────────────────────────
    for section in relevant_sections:
        try:
            # Use Plex's own title search to get a candidate list,
            # then apply our own fuzzy matching on top.
            results = section.search(title=title)
        except Exception as e:
            logger.warning("Search error in section '%s': %s", section.title, e)
            continue

        for item in results:
            item_title = getattr(item, "title", "") or ""
            item_year  = getattr(item, "year", None)

            # Fuzzy title match
            if not _titles_match(item_title, title):
                continue

            # Year match: allow ±1 tolerance for metadata discrepancies
            if year and item_year:
                if abs(item_year - year) > 1:
                    continue

            logger.info(
                "Plex HIT: '%s' (%s) in section '%s'",
                item_title, item_year, section.title,
            )
            return {
                "status": STATUS_FOUND,
                "title": item_title,
                "year": item_year,
                "library": section.title,
            }

    return {"status": STATUS_MISSING}


# ── Public async function ─────────────────────────────────────────────────────

async def check_library(
    title: str,
    year: int | None,
    media_type: str,
) -> dict:
    """
    Async wrapper — runs the blocking PlexAPI call in a thread pool.

    Args:
        title:      Title to look up (from TMDB).
        year:       Release year. Pass None if unknown.
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