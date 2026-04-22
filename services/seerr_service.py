"""
services/seerr_service.py - Overseerr/Jellyseerr API integration.

Handles media requests routed to the correct Radarr/Sonarr root folder
based on content category (animation, dansk, standard, tv_program).

Flow for every request:
  1. GET /api/v1/media?tmdbId=X  → check current Seerr status
  2. If already requested/processing → return early with clear status
  3. Otherwise → POST /api/v1/request

Per the Seerr API spec:
- Movies: seasons key can be omitted
- TV Shows: seasons MUST be an array of integers (actual season numbers)
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

# Seerr mediaStatus codes
# 1 = Unknown, 2 = Pending, 3 = Processing, 4 = Partially Available, 5 = Available
_STATUS_QUEUED     = {2, 3}   # Requested / downloading
_STATUS_AVAILABLE  = {4, 5}   # Already on Plex (partially or fully)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_url() -> str:
    return SEERR_URL.rstrip("/")


async def _get_seerr_status(tmdb_id: int, media_type: str) -> dict:
    """
    Check the current status of a title in Seerr.

    Returns a dict with:
      seerr_status  → "queued" | "available" | "not_found" | "error"
      media_status  → raw Seerr mediaStatus integer (if found)
    """
    params = {
        "externalId": tmdb_id,
        "externalIdType": "tmdb",
        "type": media_type,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_base_url()}/api/v1/media",
                headers=_HEADERS,
                params=params,
            )

            if resp.status_code == 404:
                return {"seerr_status": "not_found"}

            resp.raise_for_status()
            data = resp.json()

            # The endpoint returns a paginated list — grab the first result.
            results = data.get("results", [])
            if not results:
                return {"seerr_status": "not_found"}

            media_status = results[0].get("mediaInfo", {}).get("status", 1)

            if media_status in _STATUS_AVAILABLE:
                return {"seerr_status": "available", "media_status": media_status}
            if media_status in _STATUS_QUEUED:
                return {"seerr_status": "queued", "media_status": media_status}

            return {"seerr_status": "not_found", "media_status": media_status}

        except httpx.HTTPError as e:
            logger.error("Seerr status check error (tmdb=%s): %s", tmdb_id, e)
            return {"seerr_status": "error", "message": str(e)}


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
                return {
                    "success": True,
                    "status": "requested",
                    "request_id": resp.json().get("id"),
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
    # Step 1: Check current Seerr status.
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

    # Step 2: Not in queue — send the request.
    root_folder = _MOVIE_ROOTS.get(category, ROOT_MOVIE_STANDARD)

    logger.info("Requesting movie tmdb_id=%s category=%s rootFolder=%s",
                tmdb_id, category, root_folder)

    result = await _post_request({
        "mediaType": "movie",
        "mediaId": tmdb_id,
        "rootFolder": root_folder,
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
    """
    # Step 1: Check current Seerr status.
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

    # Step 2: Not in queue — send the request.
    root_folder = _TV_ROOTS.get(category, ROOT_TV_STANDARD)
    seasons_payload = [int(s) for s in season_numbers]

    logger.info("Requesting TV tmdb_id=%s seasons=%s category=%s rootFolder=%s",
                tmdb_id, seasons_payload, category, root_folder)

    result = await _post_request({
        "mediaType": "tv",
        "mediaId": tmdb_id,
        "seasons": seasons_payload,
        "rootFolder": root_folder,
        "is4k": False,
        "isDefault": False,
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
    """
    Look up a Seerr user ID by matching their Plex username.
    Returns None if not found.
    """
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
        # Seerr stores the Plex username in plexUsername or displayName
        plex_name = (user.get("plexUsername") or "").lower()
        display    = (user.get("displayName") or "").lower()
        email      = (user.get("email") or "").lower()
        if norm in {plex_name, display, email}:
            return user.get("id")

    return None

async def get_all_requests(plex_username: str | None = None) -> dict:
    """
    Fetch media requests from Seerr.

    If plex_username is provided, only returns requests made by that user.
    Otherwise returns all requests (admin view).
    """
    # Resolve Seerr user ID if we have a Plex username
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
        media     = item.get("media", {}) or {}
        req_type  = item.get("type", "unknown")
        status    = media.get("status", 1)

        # Map numeric status to human-friendly label
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
            "title":      title,
            "type":       req_type,
            "status":     status_label,
            "requested":  item.get("createdAt", "")[:10],
            "tmdb_id":    media.get("tmdbId"),
        })

    return {
        "success":  True,
        "requests": requests_list,
        "total":    data.get("pageInfo", {}).get("results", len(requests_list)),
    }


async def get_request_status(title: str, plex_username: str | None = None) -> dict:
    """
    Look up the Seerr status for a specific title by name.

    Searches all requests and returns the best match.
    """
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