"""
services/seerr_service.py - Overseerr/Jellyseerr API integration.

CHANGES vs previous version:
  - _get_seerr_status() now uses GET /api/v1/media?tmdbId=X instead of
    the incorrect externalId/externalIdType parameters that returned 400.

Flow for every request:
  1. GET /api/v1/media?tmdbId=X  → check current Seerr status
  2. If already requested/processing → return early with clear status
  3. Otherwise → POST /api/v1/request
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

# Seerr mediaStatus codes
# 1 = Unknown, 2 = Pending, 3 = Processing, 4 = Partially Available, 5 = Available
_STATUS_QUEUED    = {2, 3}
_STATUS_AVAILABLE = {4, 5}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_url() -> str:
    return SEERR_URL.rstrip("/")


async def _get_seerr_status(tmdb_id: int, media_type: str) -> dict:
    """
    Check status via /api/v1/request — /api/v1/media returnerer 400.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_base_url()}/api/v1/request",
                headers=_HEADERS,
                params={"take": 100, "skip": 0, "filter": "all"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("Seerr status check error (tmdb=%s): %s", tmdb_id, e)
            return {"seerr_status": "not_found"}

    for item in data.get("results", []):
        media = item.get("media", {}) or {}
        if media.get("tmdbId") == tmdb_id:
            status = media.get("status", 1)
            if status in _STATUS_AVAILABLE:
                return {"seerr_status": "available", "media_status": status}
            if status in _STATUS_QUEUED:
                return {"seerr_status": "queued", "media_status": status}

    return {"seerr_status": "not_found"}


async def _post_request(payload: dict) -> dict:
    """Shared POST logic for all Seerr requests."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                f"{_base_url()}/api/v1/request",
                headers=_HEADERS,
                json=payload,
            )

            if resp.status_code in (201, 202):
                body = resp.json()
                logger.info("Seerr POST response body: %s", body)
                return {
                    "success": True,
                    "status": "requested",
                    "request_id": body.get("id"),
                }

            if resp.status_code == 409:
                return {
                    "success": False,
                    "status": "already_queued",
                    "message": "Allerede i køen eller tilgængelig.",
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
    Check Seerr status then send a movie request if needed.

    Args:
        tmdb_id:  The TMDB ID of the film.
        category: "animation", "dansk", or "standard".
    """
    status = await _get_seerr_status(tmdb_id, "movie")

    if status["seerr_status"] == "queued":
        return {
            "success": False,
            "status": "already_queued",
            "message": "Filmen ligger allerede i Seerr-køen og venter på at blive hentet.",
        }

    if status["seerr_status"] == "available":
        return {
            "success": False,
            "status": "already_available",
            "message": "Filmen er allerede tilgængelig via Seerr.",
        }

    root_folder = _MOVIE_ROOTS.get(category, ROOT_MOVIE_STANDARD)

    logger.info("Requesting movie tmdb_id=%s category=%s rootFolder=%s",
                tmdb_id, category, root_folder)

    result = await _post_request({
        "mediaType": "movie",
        "mediaId": tmdb_id,
        "rootFolder": root_folder,
        "serverId": 0,
        "profileId": 0,
        "is4k": False,
        "isDefault": False,
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
    Check Seerr status then send a TV series request if needed.

    Args:
        tmdb_id:        The TMDB ID of the series.
        season_numbers: Exact season numbers from TMDB's seasons list.
        category:       "tv_program" or "standard".

    Rodmappe-logik:
        tv_program → /mnt/unionfs/Media/TV/TV   (reality, talkshow, nyheder, dokumentar)
        standard   → /mnt/unionfs/Media/TV/Serier (fiktion, drama, krimi)
    """
    status = await _get_seerr_status(tmdb_id, "tv")

    if status["seerr_status"] == "queued":
        return {
            "success": False,
            "status": "already_queued",
            "message": "Serien ligger allerede i Seerr-køen og venter på at blive hentet.",
        }

    if status["seerr_status"] == "available":
        return {
            "success": False,
            "status": "already_available",
            "message": "Serien er allerede tilgængelig via Seerr.",
        }

    # Rodmappe bestemmes af category — tv_program går i /TV, alt andet i /Serier
    if category == "tv_program":
        root_folder = ROOT_TV_PROGRAMMER   # /mnt/unionfs/Media/TV/TV
    else:
        root_folder = ROOT_TV_STANDARD     # /mnt/unionfs/Media/TV/Serier

    seasons_payload = [int(s) for s in season_numbers]

    logger.info("Requesting TV tmdb_id=%s seasons=%s category=%s rootFolder=%s",
                tmdb_id, seasons_payload, category, root_folder)

    result = await _post_request({
        "mediaType": "tv",
        "mediaId": tmdb_id,
        "seasons": seasons_payload,
        "rootFolder": root_folder,
        "serverId": 0,
        "profileId": 7,
        "languageProfileId": 1,
        "is4k": False,
    })

    if result.get("status") == "requested":
        result["message"] = (
            f"Serien er tilføjet til køen! "
            f"({len(seasons_payload)} sæson{'er' if len(seasons_payload) != 1 else ''}:"
            f" {seasons_payload})"
        )
        result["root_folder"] = root_folder
        result["seasons_requested"] = seasons_payload

    return result


async def _get_seerr_user_id(plex_username: str) -> int | None:
    """Look up a Seerr user ID by matching their Plex username."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_base_url()}/api/v1/user",
                headers=_HEADERS,
                params={"take": 50, "skip": 0},
            )
            resp.raise_for_status()
            users = resp.json().get("results", [])
        except httpx.HTTPError as e:
            logger.error("Seerr user lookup error: %s", e)
            return None

    norm = plex_username.strip().lower()
    for user in users:
        plex_name = (user.get("plexUsername") or "").lower()
        display   = (user.get("displayName") or "").lower()
        email     = (user.get("email") or "").lower()
        if norm in {plex_name, display, email}:
            return user.get("id")

    logger.warning("No Seerr user found for plex_username=%r", plex_username)
    return None


async def get_all_requests(plex_username: str | None = None) -> dict:
    """Fetch media requests from Seerr, optionally filtered by user."""
    seerr_user_id: int | None = None
    if plex_username:
        seerr_user_id = await _get_seerr_user_id(plex_username)

    params: dict = {"take": 50, "skip": 0, "sort": "added", "filter": "all"}
    if seerr_user_id:
        params["requestedBy"] = seerr_user_id

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{_base_url()}/api/v1/request",
                headers=_HEADERS,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("Seerr get_all_requests error: %s", e)
            return {"success": False, "message": f"Kunne ikke hente bestillinger: {e}"}

    requests_list = []
    for item in data.get("results", []):
        media    = item.get("media", {}) or {}
        req_type = item.get("type", "unknown")
        status   = media.get("status", 1)

        status_label = {
            1: "afventer",
            2: "bestilt",
            3: "på_vej",
            4: "delvist_klar",
            5: "klar",
        }.get(status, "ukendt")

        title = (
            media.get("originalTitle")
            or media.get("title")
            or f"ID {media.get('tmdbId', '?')}"
        )

        requests_list.append({
            "title":     title,
            "type":      req_type,
            "status":    status_label,
            "requested": item.get("createdAt", "")[:10],
            "tmdb_id":   media.get("tmdbId"),
        })

    return {
        "success":  True,
        "requests": requests_list,
        "total":    data.get("pageInfo", {}).get("results", len(requests_list)),
    }


async def get_request_status(title: str, plex_username: str | None = None) -> dict:
    """Look up the Seerr status for a specific title by name."""
    result = await get_all_requests(plex_username=plex_username)
    if not result.get("success"):
        return result

    title_lower = title.lower()
    matches = [
        r for r in result["requests"]
        if title_lower in (r.get("title") or "").lower()
    ]

    if not matches:
        return {"success": True, "found": False, "message": f"Ingen aktiv bestilling fundet for '{title}'."}

    return {"success": True, "found": True, "matches": matches}