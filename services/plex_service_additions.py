# Dette er TILFØJELSERNE til plex_service.py — ikke en komplet fil
# De skal flettes ind i den eksisterende kode på GitHub

# ── Ændring 1: I _check_sync(), udskift return-linjen ved STATUS_FOUND med: ──
#
#            return {
#                "status":            STATUS_FOUND,
#                "title":             item_title,
#                "year":              item_year,
#                "ratingKey":         getattr(item, "ratingKey", None),
#                "machineIdentifier": getattr(plex, "machineIdentifier", None),
#            }
#
# ── Ændring 2: Ny funktion add_to_watchlist ───────────────────────────────────

async def add_to_watchlist(
    tmdb_id: int,
    title: str,
    plex_username: str | None = None,
) -> dict:
    """
    Tilføj en titel til brugerens Plex Watchlist via myPlexAccount.

    Bruger searchDiscover() til at finde titlen og addToWatchlist() til at tilføje.
    Kræver at admin_plex har adgang til brugerens konto.
    """
    try:
        return await asyncio.to_thread(
            partial(_add_to_watchlist_sync, tmdb_id=tmdb_id, title=title)
        )
    except Exception as e:
        logger.error("add_to_watchlist error: %s", e)
        return {"success": False, "message": str(e)}


def _add_to_watchlist_sync(tmdb_id: int, title: str) -> dict:
    """Synkron implementering af watchlist-tilføjelse via PlexAPI."""
    try:
        admin_plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
        account    = admin_plex.myPlexAccount()
    except Exception as e:
        logger.error("Watchlist: kunne ikke forbinde til Plex: %s", e)
        return {"success": False, "message": f"Forbindelsesfejl: {e}"}

    try:
        # Søg via Discover (Plex's online katalog)
        results = account.searchDiscover(title, libtype="movie") or []
        if not results:
            results = account.searchDiscover(title, libtype="show") or []

        if not results:
            return {
                "success": False,
                "message": f"Kunne ikke finde '{title}' i Plex Discover.",
            }

        # Brug første resultat
        item = results[0]
        account.addToWatchlist(item)
        logger.info("Watchlist: '%s' tilføjet for admin-konto", title)
        return {"success": True, "title": getattr(item, "title", title)}

    except Exception as e:
        logger.error("Watchlist add error for '%s': %s", title, e)
        return {"success": False, "message": str(e)}