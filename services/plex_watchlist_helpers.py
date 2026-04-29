"""
services/plex_watchlist_helpers.py - Plex Discover watchlist fetch helpers.

Henter brugerens Plex Discover watchlist via PlexAPI's myPlexAccount().watchlist().
Returnerer normaliserede dicts med tmdb_id, title, year, media_type.

Dette modul lever separat fra plex_service.py for at holde plex_service.py
fokuseret på bibliotek-operationer. Watchlist-relaterede helpers er en
naturligt afgrænset bekymring der er nemmere at vedligeholde isoleret.

CHANGES (v0.1.0 — initial):
  - fetch_plex_watchlist() async wrapper.
  - _fetch_plex_watchlist_sync() synkron implementering.
  - Robust GUID-parsing: udtrækker TMDB ID fra Plex's interne IDs.
  - Filtrerer items uden TMDB ID væk (vi kan ikke bruge dem).
"""

import asyncio
import logging
from functools import partial

from plexapi.server import PlexServer

from config import PLEX_TOKEN, PLEX_URL
from services.plex_service import _extract_tmdb_id_from_guids

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_watchlist_item(item) -> dict | None:
    """
    Normaliser et Plex watchlist-item til vores standard-format.

    Returnerer None hvis item mangler TMDB ID (kan ikke bruges hos os).
    """
    try:
        tmdb_id = _extract_tmdb_id_from_guids(item)
        if not tmdb_id:
            logger.debug(
                "watchlist item mangler TMDB ID: %s",
                getattr(item, "title", "?"),
            )
            return None

        # Plex bruger 'movie' og 'show' — vi standardiserer til 'movie'/'tv'
        item_type = getattr(item, "type", None)
        if item_type == "movie":
            media_type = "movie"
        elif item_type == "show":
            media_type = "tv"
        else:
            logger.debug("watchlist item har ukendt type: %s", item_type)
            return None

        return {
            "tmdb_id":    tmdb_id,
            "media_type": media_type,
            "title":      getattr(item, "title", "Ukendt"),
            "year":       getattr(item, "year", None),
            "rating":     getattr(item, "audienceRating", None) or getattr(item, "rating", None),
        }
    except Exception as e:
        logger.warning("_normalize_watchlist_item fejl: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Sync implementation
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_plex_watchlist_sync(plex_username: str | None = None) -> list[dict]:
    """
    Synkron implementering — kører i thread pool via asyncio.to_thread.

    Returns:
      Liste af normaliserede dicts. Tom liste hvis bruger ikke har watchlist
      eller ved fejl.
    """
    try:
        # Vi behøver PlexServer for at få myPlexAccount() — bruger admin-token
        # fordi watchlist er konto-baseret, ikke server-baseret.
        plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
        account = plex.myPlexAccount()

        # Hvis plex_username er angivet OG forskellig fra admin, skal vi
        # impersonate'e en Plex Home User. Plex's watchlist API understøtter
        # IKKE direkte at hente andre brugeres watchlists — det er en
        # privat liste per konto.
        #
        # NOTE: Dette betyder at hvis vi har 5 testere, vil vi se ADMIN's
        # watchlist for alle dem. Dette er en kendt begrænsning af Plex API.
        # For produktions-features skal hver bruger have deres egen Plex
        # konto-token (out of scope for nu).

        if plex_username and plex_username != account.username:
            logger.warning(
                "fetch_plex_watchlist: bruger '%s' er ikke admin '%s' — "
                "returnerer admin's watchlist (Plex API begrænsning)",
                plex_username, account.username,
            )

        # Hent watchlist
        watchlist_items = account.watchlist() or []

        # Normaliser hvert item
        normalized = []
        skipped_no_tmdb = 0
        for item in watchlist_items:
            normalized_item = _normalize_watchlist_item(item)
            if normalized_item is not None:
                normalized.append(normalized_item)
            else:
                skipped_no_tmdb += 1

        logger.info(
            "fetch_plex_watchlist: hentede %d items (sprang %d uden TMDB ID over)",
            len(normalized), skipped_no_tmdb,
        )
        return normalized

    except Exception as e:
        logger.error("_fetch_plex_watchlist_sync error: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Async public API
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_plex_watchlist(plex_username: str | None = None) -> list[dict]:
    """
    Hent brugerens Plex Discover watchlist async.

    Args:
      plex_username: Plex-brugernavn. None = admin (default).
                     NOTE: Plex API kan kun returnere admin-kontoens watchlist
                     for nu. Andre brugere kræver deres egne Plex tokens.

    Returns:
      Liste af dicts: [{tmdb_id, media_type, title, year, rating}]
      Tom liste ved fejl eller hvis bruger har ingen watchlist.
    """
    try:
        return await asyncio.to_thread(
            partial(_fetch_plex_watchlist_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("fetch_plex_watchlist error: %s", e)
        return []