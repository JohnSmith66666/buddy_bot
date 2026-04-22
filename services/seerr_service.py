"""
services/seerr_service.py - Overseerr/Jellyseerr API integration.

Handles media requests routed to the correct Radarr/Sonarr root folder
based on content category (animation, dansk, standard, tv_program).
Seasons are intentionally omitted from TV payloads — Seerr automatically
requests all seasons when the key is absent.
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
    return SEERR_URL.rstrip("/")


async def _post_request(payload: dict) -> dict:
    """Shared POST logic for all Seerr requests."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                f"{_base_url()}/api/v1/request",
                headers=_HEADERS,
                json=payload,
            )

            if resp.status_code == 201:
                return {
                    "success": True,
                    "status": "requested",
                    "request_id": resp.json().get("id"),
                }

            if resp.status_code == 409:
                return {
                    "success": False,
                    "status": "already_requested",
                    "message": "Allerede anmodet om eller findes i biblioteket.",
                }

            resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            logger.error("Seerr HTTP error: %s", e)
            return {
                "success": False,
                "status": "error",
                "message": f"Seerr fejl: HTTP {e.response.status_code}",
            }
        except httpx.HTTPError as e:
            logger.error("Seerr connection error: %s", e)
            return {
                "success": False,
                "status": "connection_error",
                "message": "Kunne ikke forbinde til Seerr. Tjek at serveren kører.",
            }

    return {"success": False, "status": "unknown_error", "message": "Ukendt fejl."}


# ── Public functions ──────────────────────────────────────────────────────────

async def request_movie(tmdb_id: int, category: str = "standard") -> dict:
    """
    Send a movie request to Seerr routed to the correct Radarr root folder.

    Args:
        tmdb_id:  The TMDB ID of the film.
        category: "animation", "dansk", or "standard".
    """
    root_folder = _MOVIE_ROOTS.get(category, ROOT_MOVIE_STANDARD)

    logger.info("Requesting movie tmdb_id=%s category=%s rootFolder=%s",
                tmdb_id, category, root_folder)

    result = await _post_request({
        "mediaType": "movie",
        "mediaId": tmdb_id,
        "rootFolder": root_folder,
        "is4k": False,
    })

    if result.get("status") == "requested":
        result["message"] = "Filmen er tilføjet til køen!"
        result["root_folder"] = root_folder

    return result


async def request_tv(tmdb_id: int, category: str = "standard") -> dict:
    """
    Send a TV series request to Seerr routed to the correct Sonarr root folder.
    Seasons key is intentionally omitted — Seerr requests all seasons automatically.

    Args:
        tmdb_id:  The TMDB ID of the series.
        category: "tv_program" or "standard".
    """
    root_folder = _TV_ROOTS.get(category, ROOT_TV_STANDARD)

    logger.info("Requesting TV tmdb_id=%s category=%s rootFolder=%s",
                tmdb_id, category, root_folder)

    result = await _post_request({
        "mediaType": "tv",
        "mediaId": tmdb_id,
        "rootFolder": root_folder,
        "is4k": False,
    })

    if result.get("status") == "requested":
        result["message"] = "Serien er tilføjet til køen!"
        result["root_folder"] = root_folder

    return result