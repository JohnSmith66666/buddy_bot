"""
services/radarr_service.py - Direkte Radarr API integration.

CHANGES vs previous version:
  - Tag-strategi ændret: bruger nu telegram_id-baserede tags (tg_<id>)
    i stedet for plex_username. Dette gør det muligt for webhook_service
    at udtrække telegram_id direkte fra tag-labelnavnet uden DB-opslag.
  - add_movie() tager nu telegram_id (int) i stedet for plex_username.
  - _get_or_create_tag() er uændret i logik, men kaldes med "tg_<id>".

Sorteringslogik (rodmapper):
  - genre indeholder 'Animation' → /mnt/unionfs/Media/Movies/Animation
  - alt andet                    → /mnt/unionfs/Media/Movies/Film
"""

import logging

import httpx

from config import (
    RADARR_API_KEY,
    RADARR_QUALITY_PROFILE_ID,
    RADARR_URL,
    ROOT_MOVIE_ANIMATION,
    ROOT_MOVIE_STANDARD,
)

logger = logging.getLogger(__name__)


def _base() -> str:
    return RADARR_URL.rstrip("/")


def _headers() -> dict:
    return {"X-Api-Key": RADARR_API_KEY, "Content-Type": "application/json"}


# ── Tag helpers ───────────────────────────────────────────────────────────────

async def _get_or_create_tag(label: str) -> int | None:
    """
    Find Radarr tag ID by label, or create it if it doesn't exist.
    Returns the tag ID, or None on error.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
            resp.raise_for_status()
            for tag in resp.json():
                if tag.get("label", "").lower() == label.lower():
                    return tag["id"]

            # Tag not found — create it
            resp = await client.post(
                f"{_base()}/api/v3/tag",
                headers=_headers(),
                json={"label": label},
            )
            resp.raise_for_status()
            tag_id = resp.json().get("id")
            logger.info("Radarr: created tag '%s' with id=%s", label, tag_id)
            return tag_id
        except httpx.HTTPError as e:
            logger.error("Radarr tag error: %s", e)
            return None


async def get_all_tags() -> dict[int, str]:
    """
    Return a mapping of {tag_id: label} for all tags in Radarr.
    Used by webhook_service to resolve integer tag IDs to labels.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
            resp.raise_for_status()
            return {t["id"]: t["label"] for t in resp.json()}
        except httpx.HTTPError as e:
            logger.error("Radarr get_all_tags error: %s", e)
            return {}


# ── Library check ─────────────────────────────────────────────────────────────

async def check_radarr_library(tmdb_id: int) -> dict:
    """
    Check if a movie is already in Radarr (downloaded or monitored).
    Returns: {"status": "found"|"missing"|"monitored_only", "title": ...}
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_base()}/api/v3/movie",
                headers=_headers(),
                params={"tmdbId": tmdb_id},
            )
            resp.raise_for_status()
            movies = resp.json()
        except httpx.HTTPError as e:
            logger.error("Radarr library check error: %s", e)
            return {"status": "error", "message": str(e)}

    if not movies:
        return {"status": "missing"}

    movie = movies[0]
    if movie.get("hasFile"):
        return {"status": "found", "title": movie.get("title"), "year": movie.get("year")}
    return {"status": "monitored_only", "title": movie.get("title"), "year": movie.get("year")}


# ── Add movie ─────────────────────────────────────────────────────────────────

async def add_movie(
    tmdb_id: int,
    title: str,
    year: int,
    genres: list[str],
    telegram_id: int | None = None,
) -> dict:
    """
    Add a movie to Radarr.

    Tag-strategi: opretter/henter tagget "tg_<telegram_id>" så webhook_service
    kan udtrække telegram_id direkte fra label-navnet uden DB-opslag.

    Rodmappe-logik:
      - 'Animation' in genres → ROOT_MOVIE_ANIMATION
      - else                  → ROOT_MOVIE_STANDARD
    """
    genre_names_lower = [g.lower() for g in genres]
    if "animation" in genre_names_lower:
        root_folder = ROOT_MOVIE_ANIMATION
    else:
        root_folder = ROOT_MOVIE_STANDARD

    tag_ids = []
    if telegram_id:
        tag_label = f"tg_{telegram_id}"
        tag_id = await _get_or_create_tag(tag_label)
        if tag_id is not None:
            tag_ids = [tag_id]

    logger.info(
        "Radarr: adding movie tmdb_id=%s title='%s' rootFolder=%s tags=%s",
        tmdb_id, title, root_folder, tag_ids,
    )

    payload = {
        "tmdbId": tmdb_id,
        "title": title,
        "year": year,
        "qualityProfileId": RADARR_QUALITY_PROFILE_ID,
        "rootFolderPath": root_folder,
        "monitored": True,
        "addOptions": {"searchForMovie": True},
        "tags": tag_ids,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                f"{_base()}/api/v3/movie",
                headers=_headers(),
                json=payload,
            )

            if resp.status_code == 201:
                body = resp.json()
                logger.info("Radarr: movie added successfully id=%s", body.get("id"))
                return {
                    "success": True,
                    "status": "added",
                    "radarr_id": body.get("id"),
                    "title": body.get("title"),
                    "root_folder": root_folder,
                    "message": "Filmen er tilføjet til køen og søges nu! 🎬",
                }

            if resp.status_code == 400:
                body = resp.json()
                if any("already" in str(e).lower() for e in body):
                    return {
                        "success": False,
                        "status": "already_exists",
                        "message": "Filmen er allerede i Radarr.",
                    }

            resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            logger.error("Radarr HTTP error: %s — body: %s", e, e.response.text)
            return {
                "success": False,
                "status": "error",
                "message": f"Radarr fejl: HTTP {e.response.status_code}",
            }
        except httpx.HTTPError as e:
            logger.error("Radarr connection error: %s", e)
            return {
                "success": False,
                "status": "connection_error",
                "message": "Kunne ikke forbinde til Radarr.",
            }

    return {"success": False, "status": "unknown_error", "message": "Ukendt fejl."}


# ── Get queue ─────────────────────────────────────────────────────────────────

async def get_radarr_queue() -> list:
    """Return current Radarr download queue."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_base()}/api/v3/queue",
                headers=_headers(),
                params={"pageSize": 50},
            )
            resp.raise_for_status()
            return resp.json().get("records", [])
        except httpx.HTTPError as e:
            logger.error("Radarr queue error: %s", e)
            return []