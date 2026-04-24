"""
plex_service_patch.py — Præcise ændringer til services/plex_service.py på GitHub.

ÆNDRING 1: _check_sync() — tilføj ratingKey + machineIdentifier ved STATUS_FOUND
═══════════════════════════════════════════════════════════════════════════════════
Find denne linje i _check_sync() (ca. linje 220 i din fil):

    return {"status": STATUS_FOUND, "title": item_title, "year": item_year}

Udskift den med:

    return {
        "status":            STATUS_FOUND,
        "title":             item_title,
        "year":              item_year,
        "ratingKey":         getattr(item, "ratingKey", None),
        "machineIdentifier": getattr(plex, "machineIdentifier", None),
    }


ÆNDRING 2: Tilføj add_to_watchlist og _add_to_watchlist_sync i bunden af filen
═══════════════════════════════════════════════════════════════════════════════════
Kopier disse to funktioner og sæt dem ind i bunden af plex_service.py:
"""

import asyncio
import logging
from functools import partial

logger = logging.getLogger(__name__)


async def add_to_watchlist(
    title: str,
    plex_username: str | None = None,
) -> dict:
    """
    Tilføj en titel til Plex Watchlist via myPlexAccount.searchDiscover().
    Kører synkron PlexAPI i thread pool for at undgå blocking.
    """
    try:
        return await asyncio.to_thread(
            partial(_add_to_watchlist_sync, title=title)
        )
    except Exception as e:
        logger.error("add_to_watchlist error: %s", e)
        return {"success": False, "message": str(e)}


def _add_to_watchlist_sync(title: str) -> dict:
    """Synkron implementering — kører i thread pool via asyncio.to_thread."""
    from plexapi.server import PlexServer
    from config import PLEX_URL, PLEX_TOKEN

    try:
        admin_plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
        account    = admin_plex.myPlexAccount()
    except Exception as e:
        logger.error("Watchlist: forbindelsesfejl: %s", e)
        return {"success": False, "message": f"Forbindelsesfejl: {e}"}

    try:
        # Søg i Plex Discover (online katalog) — prøv film først, derefter TV
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