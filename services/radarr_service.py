"""
services/radarr_service.py - Direkte Radarr API integration.

CHANGES vs previous version (v0.9.6 — tag-robusthed):
  - _get_or_create_tag(): Håndterer nu 400 Bad Request fra POST /tag mere
    robust. Radarr returnerer 400 hvis et tag med samme label allerede
    eksisterer — men vores GET-opslag finder det ikke pga. API-quirks.
    Fix: ved 400 på POST, lav et nyt GET og find tagget. Hvis det stadig
    ikke kan findes, log en warning og returner None (film tilføjes uden tag).
  - Ingen ændringer i add_movie() eller check_radarr_library() logik.

UNCHANGED:
  - Tag-strategi: tg_<telegram_id>-labels — uændret.
  - Rodmappe-logik: Animation vs Film — uændret.
  - get_all_tags(), check_radarr_library() — uændrede.
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

    Robust mod 400-fejl fra POST /tag:
      Radarr returnerer 400 når et tag med samme label allerede eksisterer
      men ikke findes via GET (API-quirk, f.eks. pga. case-mismatch eller
      timing). Ved 400: lav nyt GET og forsøg at finde det igen.
      Hvis stadig ikke fundet: log warning og returner None — filmen
      tilføjes uden tag i stedet for at crashe.

    Returns the tag ID (int), or None on unrecoverable error.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            # Trin 1: Søg efter eksisterende tag
            resp = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
            resp.raise_for_status()
            existing_tags = resp.json()

            label_lower = label.lower().strip()
            for tag in existing_tags:
                if tag.get("label", "").lower().strip() == label_lower:
                    logger.debug("Radarr: fandt eksisterende tag '%s' id=%s", label, tag["id"])
                    return tag["id"]

            # Trin 2: Tag ikke fundet — opret det
            resp = await client.post(
                f"{_base()}/api/v3/tag",
                headers=_headers(),
                json={"label": label},
            )

            if resp.status_code == 400:
                # Radarr siger 400 — tagget eksisterer sandsynligvis allerede
                # Lav et nyt GET og forsøg at finde det
                logger.warning(
                    "Radarr: POST /tag returnerede 400 for '%s' — forsøger GET igen", label
                )
                resp2 = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
                resp2.raise_for_status()
                for tag in resp2.json():
                    if tag.get("label", "").lower().strip() == label_lower:
                        logger.info(
                            "Radarr: fandt tag '%s' efter 400-fallback, id=%s", label, tag["id"]
                        )
                        return tag["id"]
                # Stadig ikke fundet — giv op, film tilføjes uden tag
                logger.warning(
                    "Radarr: kunne ikke finde eller oprette tag '%s' — film tilføjes uden tag",
                    label,
                )
                return None

            resp.raise_for_status()
            tag_id = resp.json().get("id")
            logger.info("Radarr: oprettede tag '%s' med id=%s", label, tag_id)
            return tag_id

        except httpx.HTTPError as e:
            logger.error("Radarr tag error for '%s': %s", label, e)
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

    Returns:
      {"status": "found"}          — filmen er downloadet og klar
      {"status": "monitored_only"} — filmen er anmodet men ikke downloadet endnu
      {"status": "missing"}        — filmen er ikke i Radarr
      {"status": "error", ...}     — API-fejl
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
    genre_names_lower = [g.lower() if isinstance(g, str) else (g.get("name") or "").lower()
                         for g in genres]
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
        else:
            logger.warning(
                "Radarr: tag '%s' ikke oprettet — film '%s' tilføjes uden notifikations-tag",
                tag_label, title,
            )

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
                errors = body if isinstance(body, list) else [body]
                if any("already" in str(e).lower() or "exists" in str(e).lower()
                       for e in errors):
                    return {
                        "success": False,
                        "status": "already_exists",
                        "message": f"'{title}' er allerede i Radarr.",
                    }
                logger.error("Radarr 400 (ukendt): %s", body)
                return {
                    "success": False,
                    "status": "error",
                    "message": f"Radarr afviste bestillingen (400): {body}",
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