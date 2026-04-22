"""
services/seerr_service.py - Overseerr/Jellyseerr API integration.

Handles media requests routed to the correct Radarr/Sonarr root folder
based on content category (animation, dansk, standard, tv_program).
"""

import logging

import httpx

from config import (
    ROOT_MOVIE_ANIMATION,
    ROOT_MOVIE_DANSK,
    ROOT_MOVIE_STANDARD,
    ROOT_TV_PROGRAMMER,
    ROOT_TV_STANDARD,
    SEERR_API_KEY,
    SEERR_URL,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_HEADERS = {
    "X-Api-Key": SEERR_API_KEY,
    "Content-Type": "application/json",
}

# Category → root folder mapping
_MOVIE_ROOTS: dict[str, str] = {
    "animation": ROOT_MOVIE_ANIMATION,
    "dansk":     ROOT_MOVIE_DANSK,
    "standard":  ROOT_MOVIE_STANDARD,
}

_TV_ROOTS: dict[str, str] = {
    "tv_program": ROOT_TV_PROGRAMMER,
    "standard":   ROOT_TV_STANDARD,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_url() -> str:
    """Strip trailing slash from SEERR_URL for consistent endpoint building."""
    return SEERR_URL.rstrip("/")


# ── Public functions ──────────────────────────────────────────────────────────

async def request_movie(tmdb_id: int, category: str = "standard") -> dict:
    """
    Send a movie request to Seerr and route it to the correct Radarr root folder.

    Args:
        tmdb_id:  The TMDB ID of the film.
        category: Routing category — "animation", "dansk", or "standard".

    Returns:
        A dict with keys: success (bool), status, message, and request_id (if created).
    """
    root_folder = _MOVIE_ROOTS.get(category, ROOT_MOVIE_STANDARD)

    payload = {
        "mediaType": "movie",
        "mediaId": tmdb_id,
        "rootFolder": root_folder,
        "is4k": False,
    }

    logger.info(
        "Requesting movie tmdb_id=%s category=%s rootFolder=%s",
        tmdb_id, category, root_folder,
    )

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                f"{_base_url()}/api/v1/request",
                headers=_HEADERS,
                json=payload,
            )

            if resp.status_code == 201:
                data = resp.json()
                return {
                    "success": True,
                    "status": "requested",
                    "message": "Filmen er tilføjet til køen!",
                    "request_id": data.get("id"),
                    "root_folder": root_folder,
                }

            if resp.status_code == 409:
                return {
                    "success": False,
                    "status": "already_requested",
                    "message": "Denne film er allerede anmodet om eller findes allerede i biblioteket.",
                }

            resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            logger.error("Seerr movie request error (tmdb=%s): %s", tmdb_id, e)
            return {
                "success": False,
                "status": "error",
                "message": f"Seerr returnerede en fejl: HTTP {e.response.status_code}",
            }
        except httpx.HTTPError as e:
            logger.error("Seerr connection error: %s", e)
            return {
                "success": False,
                "status": "connection_error",
                "message": "Kunne ikke forbinde til Seerr. Tjek at serveren kører.",
            }

    return {
        "success": False,
        "status": "unknown_error",
        "message": "Ukendt fejl ved anmodning.",
    }


async def request_tv(
    tmdb_id: int,
    season_count: int,
    category: str = "standard",
) -> dict:
    """
    Send a TV series request to Seerr and route it to the correct Sonarr root folder.

    Args:
        tmdb_id:      The TMDB ID of the series.
        season_count: Number of seasons to request (from TMDB number_of_seasons).
                      Builds an explicit seasons list so all seasons are monitored.
        category:     Routing category — "tv_program" or "standard".

    Returns:
        A dict with keys: success (bool), status, message, and request_id (if created).
    """
    root_folder = _TV_ROOTS.get(category, ROOT_TV_STANDARD)

    # Build explicit season list so Sonarr monitors all seasons (fixes unmonitored bug).
    seasons = list(range(1, season_count + 1))

    payload = {
        "mediaType": "tv",
        "mediaId": tmdb_id,
        "rootFolder": root_folder,
        "is4k": False,
        "seasons": seasons,
    }

    logger.info(
        "Requesting TV tmdb_id=%s seasons=%s category=%s rootFolder=%s",
        tmdb_id, seasons, category, root_folder,
    )

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                f"{_base_url()}/api/v1/request",
                headers=_HEADERS,
                json=payload,
            )

            if resp.status_code == 201:
                data = resp.json()
                return {
                    "success": True,
                    "status": "requested",
                    "message": f"Serien er tilføjet til køen! ({season_count} sæson{'er' if season_count != 1 else ''})",
                    "request_id": data.get("id"),
                    "root_folder": root_folder,
                    "seasons_requested": seasons,
                }

            if resp.status_code == 409:
                return {
                    "success": False,
                    "status": "already_requested",
                    "message": "Denne serie er allerede anmodet om eller findes allerede i biblioteket.",
                }

            resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            logger.error("Seerr TV request error (tmdb=%s): %s", tmdb_id, e)
            return {
                "success": False,
                "status": "error",
                "message": f"Seerr returnerede en fejl: HTTP {e.response.status_code}",
            }
        except httpx.HTTPError as e:
            logger.error("Seerr connection error: %s", e)
            return {
                "success": False,
                "status": "connection_error",
                "message": "Kunne ikke forbinde til Seerr. Tjek at serveren kører.",
            }

    return {
        "success": False,
        "status": "unknown_error",
        "message": "Ukendt fejl ved anmodning.",
    }