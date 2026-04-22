"""
services/seerr_service.py - Overseerr/Jellyseerr API integration.

Handles media requests routed to the correct Radarr/Sonarr root folder
based on content category (animation, dansk, standard, tv_program).

Per the Seerr API spec:
- Movies: seasons key can be omitted or set to "all"
- TV Shows: seasons MUST be an array of integers (actual season numbers)
  Missing this key causes a 500 Internal Server Error.
Both 201 Created and 202 Accepted are treated as success.
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

            # Both 201 Created (movies) and 202 Accepted (TV) are success.
            if resp.status_code in (201, 202):
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

            if resp.status_code == 403:
                return {
                    "success": False,
                    "status": "forbidden",
                    "message": "Adgang nægtet af Seerr. Tjek API-nøglen.",
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


async def request_tv(
    tmdb_id: int,
    season_numbers: list[int],
    category: str = "standard",
) -> dict:
    """
    Send a TV series request to Seerr routed to the correct Sonarr root folder.

    Per the Seerr API spec, 'seasons' MUST be an array of integers representing
    the actual season numbers. Omitting it causes a 500 error.

    Args:
        tmdb_id:        The TMDB ID of the series.
        season_numbers: List of actual season numbers, e.g. [1, 2, 3].
                        Must be fetched from TMDB's seasons list
                        (use season_number field, skip season 0/specials).
        category:       "tv_program" or "standard".
    """
    root_folder = _TV_ROOTS.get(category, ROOT_TV_STANDARD)

    logger.info("Requesting TV tmdb_id=%s seasons=%s category=%s rootFolder=%s",
                tmdb_id, season_numbers, category, root_folder)

    result = await _post_request({
        "mediaType": "tv",
        "mediaId": tmdb_id,
        "seasons": season_numbers,   # REQUIRED — array of ints per Seerr API spec
        "rootFolder": root_folder,
        "is4k": False,
    })

    if result.get("status") == "requested":
        result["message"] = (
            f"Serien er tilføjet til køen! "
            f"({len(season_numbers)} sæson{'er' if len(season_numbers) != 1 else ''})"
        )
        result["root_folder"] = root_folder
        result["seasons_requested"] = season_numbers

    return result