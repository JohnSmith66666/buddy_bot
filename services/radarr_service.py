"""
services/radarr_service.py - Direkte Radarr API integration.

CHANGES vs previous version (v0.9.9 — tag-format fix):
  - KRITISK FIX: Tag-label format ændret fra "tg_<id>" til "tg-<id>".
    Radarr's POST /api/v3/tag returnerede 400 Bad Request på labels med
    underscore. Bindestreg accepteres af Radarr.
    webhook_service.py er tilsvarende opdateret til at parse "tg-".
  - _get_or_create_tag(): Logger nu Radarr's response body ved 400.
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

_TAG_PREFIX = "tg-"   # Bindestreg — Radarr afviser underscore i labels


def _base() -> str:
    return RADARR_URL.rstrip("/")


def _headers() -> dict:
    return {"X-Api-Key": RADARR_API_KEY, "Content-Type": "application/json"}


async def _get_or_create_tag(label: str) -> int | None:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
            resp.raise_for_status()
            label_lower = label.lower().strip()
            for tag in resp.json():
                if tag.get("label", "").lower().strip() == label_lower:
                    logger.debug("Radarr: fandt tag '%s' id=%s", label, tag["id"])
                    return tag["id"]

            resp = await client.post(
                f"{_base()}/api/v3/tag",
                headers=_headers(),
                json={"label": label},
            )

            if resp.status_code == 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                logger.warning(
                    "Radarr: POST /tag 400 for '%s' — response: %s — forsøger GET igen",
                    label, body,
                )
                resp2 = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
                resp2.raise_for_status()
                for tag in resp2.json():
                    if tag.get("label", "").lower().strip() == label_lower:
                        logger.info("Radarr: fandt tag '%s' efter 400-fallback id=%s", label, tag["id"])
                        return tag["id"]
                logger.warning("Radarr: kunne ikke oprette tag '%s' — bruges uden tag", label)
                return None

            resp.raise_for_status()
            tag_id = resp.json().get("id")
            logger.info("Radarr: oprettede tag '%s' id=%s", label, tag_id)
            return tag_id

        except httpx.HTTPError as e:
            logger.error("Radarr tag error for '%s': %s", label, e)
            return None


async def get_all_tags() -> dict[int, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
            resp.raise_for_status()
            return {t["id"]: t["label"] for t in resp.json()}
        except httpx.HTTPError as e:
            logger.error("Radarr get_all_tags error: %s", e)
            return {}


async def check_radarr_library(tmdb_id: int) -> dict:
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


async def add_movie(
    tmdb_id: int,
    title: str,
    year: int,
    genres: list[str],
    telegram_id: int | None = None,
) -> dict:
    genre_names_lower = [g.lower() if isinstance(g, str) else (g.get("name") or "").lower()
                         for g in genres]
    root_folder = ROOT_MOVIE_ANIMATION if "animation" in genre_names_lower else ROOT_MOVIE_STANDARD

    tag_ids = []
    if telegram_id:
        tag_label = f"{_TAG_PREFIX}{telegram_id}"
        tag_id = await _get_or_create_tag(tag_label)
        if tag_id is not None:
            tag_ids = [tag_id]
        else:
            logger.warning("Radarr: tag '%s' ikke oprettet — '%s' tilføjes uden tag", tag_label, title)

    logger.info("Radarr: adding movie tmdb_id=%s title='%s' rootFolder=%s tags=%s",
                tmdb_id, title, root_folder, tag_ids)

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
            resp = await client.post(f"{_base()}/api/v3/movie", headers=_headers(), json=payload)

            if resp.status_code == 201:
                body = resp.json()
                logger.info("Radarr: movie added successfully id=%s", body.get("id"))
                return {"success": True, "status": "added", "radarr_id": body.get("id"),
                        "title": body.get("title"), "root_folder": root_folder,
                        "message": "Filmen er tilføjet til køen og søges nu! 🎬"}

            if resp.status_code == 400:
                body = resp.json()
                errors = body if isinstance(body, list) else [body]
                if any("already" in str(e).lower() or "exists" in str(e).lower() for e in errors):
                    return {"success": False, "status": "already_exists",
                            "message": f"'{title}' er allerede i Radarr."}
                logger.error("Radarr 400: %s", body)
                return {"success": False, "status": "error", "message": f"Radarr 400: {body}"}

            resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            logger.error("Radarr HTTP error: %s — %s", e, e.response.text)
            return {"success": False, "status": "error",
                    "message": f"Radarr fejl: HTTP {e.response.status_code}"}
        except httpx.HTTPError as e:
            logger.error("Radarr connection error: %s", e)
            return {"success": False, "status": "connection_error",
                    "message": "Kunne ikke forbinde til Radarr."}

    return {"success": False, "status": "unknown_error", "message": "Ukendt fejl."}