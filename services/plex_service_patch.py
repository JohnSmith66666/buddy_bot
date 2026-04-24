"""
plex_service_patch.py — Præcise ændringer til services/plex_service.py på GitHub.

CHANGES:
  1. _check_sync(): returnerer nu ratingKey og machineIdentifier ved STATUS_FOUND.
     Disse bruges af show_confirmation() til at bygge Plex deep-link URLs.
  2. add_to_watchlist() og _add_to_watchlist_sync(): nye funktioner til
     Plex Watchlist-integration via myPlexAccount().

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ÆNDRING 1 — I _check_sync(), find denne return-linje ved STATUS_FOUND:

    return {"status": STATUS_FOUND, "title": item_title, "year": item_year}

Udskift den med:

    return {
        "status":            STATUS_FOUND,
        "title":             item_title,
        "year":              item_year,
        "ratingKey":         getattr(item, "ratingKey", None),
        "machineIdentifier": getattr(plex, "machineIdentifier", None),
    }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ÆNDRING 2 — Tilføj disse to funktioner i bunden af plex_service.py:
"""

from functools import partial
import asyncio
import logging

logger = logging.getLogger(__name__)


async def add_to_watchlist(
    title: str,
    plex_username: str | None = None,
) -> dict:
    """
    Tilføj en titel til Plex Watchlist via myPlexAccount.searchDiscover().
    Bruger admin-kontoen (PLEX_TOKEN) til at finde og tilføje titlen.
    """
    try:
        return await asyncio.to_thread(
            partial(_add_to_watchlist_sync, title=title)
        )
    except Exception as e:
        logger.error("add_to_watchlist error: %s", e)
        return {"success": False, "message": str(e)}


def _add_to_watchlist_sync(title: str) -> dict:
    """Synkron implementering — kører i thread pool."""
    from plexapi.server import PlexServer
    from config import PLEX_URL, PLEX_TOKEN

    try:
        admin_plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
        account    = admin_plex.myPlexAccount()
    except Exception as e:
        logger.error("Watchlist: kunne ikke forbinde til Plex: %s", e)
        return {"success": False, "message": f"Forbindelsesfejl: {e}"}

    try:
        # Søg i Plex Discover (online katalog)
        results = account.searchDiscover(title, libtype="movie") or []
        if not results:
            results = account.searchDiscover(title, libtype="show") or []

        if not results:
            return {
                "success": False,
                "message": f"Kunne ikke finde '{title}' i Plex Discover.",
            }

        item = results[0]
        account.addToWatchlist(item)
        found_title = getattr(item, "title", title)
        logger.info("Watchlist: '%s' tilføjet.", found_title)
        return {"success": True, "title": found_title}

    except Exception as e:
        logger.error("Watchlist add error for '%s': %s", title, e)
        return {"success": False, "message": str(e)}