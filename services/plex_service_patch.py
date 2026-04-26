"""
plex_service_patch.py — To præcise ændringer til services/plex_service.py på GitHub.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ÆNDRING 1 — _check_sync(): returnér ratingKey + machineIdentifier
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Find denne linje i _check_sync() (ca. linje 220):

    return {"status": STATUS_FOUND, "title": item_title, "year": item_year}

Udskift med:

    return {
        "status":            STATUS_FOUND,
        "title":             item_title,
        "year":              item_year,
        "ratingKey":         getattr(item, "ratingKey", None),
        "machineIdentifier": getattr(plex, "machineIdentifier", None),
    }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ÆNDRING 2 — Tilføj add_to_watchlist i bunden af plex_service.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Kopier disse to funktioner og indsæt dem i bunden af plex_service.py:
"""

import asyncio
import logging
from functools import partial

logger = logging.getLogger(__name__)


async def add_to_watchlist(
    title: str,
    plex_username: str | None = None,
) -> bool:
    """
    Tilføj en titel til Plex Watchlist.
    Returnerer True ved success, False hvis titlen ikke kunne findes.
    Kører synkron PlexAPI i thread pool for at undgå blocking.
    """
    try:
        return await asyncio.to_thread(
            partial(_add_to_watchlist_sync, title=title, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("add_to_watchlist error: %s", e)
        return False


def _add_to_watchlist_sync(title: str, plex_username: str | None = None) -> bool:
    """
    Synkron implementering — kører i thread pool via asyncio.to_thread.
    Bruger _connect() for at få brugerens server og derefter myPlexAccount().
    """
    # Brug _connect for at respektere plex_username-logikken
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        # _connect returnerer dict ved fejl
        logger.error("add_to_watchlist: kunne ikke forbinde: %s", plex)
        return False

    try:
        account = plex.myPlexAccount()
        results = account.searchDiscover(title)
        if not results:
            logger.warning("Watchlist: ingen resultater for '%s' i Discover", title)
            return False

        item = results[0]
        account.addToWatchlist(item)
        logger.info("Watchlist: '%s' tilføjet ('%s')", title, getattr(item, "title", title))
        return True

    except Exception as e:
        logger.error("Watchlist add error for '%s': %s", title, e)
        return False