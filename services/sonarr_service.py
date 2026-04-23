"""
services/sonarr_service.py - Direkte Sonarr API integration.

Erstatter Seerr for serie-anmodninger.

Sorteringslogik (rodmapper):
  - original_language == 'da' → /mnt/unionfs/Media/TV/TV   (dansk indhold)
  - alt andet                 → /mnt/unionfs/Media/TV/Serier

Tags:
  - Hver anmodning tildeles automatisk et tag med brugerens Plex-navn.
  - Tagget oprettes i Sonarr hvis det ikke findes.
"""

import logging

import httpx

from config import (
    SONARR_API_KEY,
    SONARR_QUALITY_PROFILE_ID,
    SONARR_URL,
    ROOT_TV_DANSK,
    ROOT_TV_STANDARD,
)

logger = logging.getLogger(__name__)


def _base() -> str:
    return SONARR_URL.rstrip("/")


def _headers() -> dict:
    return {"X-Api-Key": SONARR_API_KEY, "Content-Type": "application/json"}


# ── Tag helpers ───────────────────────────────────────────────────────────────

async def _get_or_create_tag(label: str) -> int | None:
    """
    Find Sonarr tag ID by label, or create it if it doesn't exist.
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
            logger.info("Sonarr: created tag '%s' with id=%s", label, tag_id)
            return tag_id
        except httpx.HTTPError as e:
            logger.error("Sonarr tag error: %s", e)
            return None


# ── Library check ─────────────────────────────────────────────────────────────

async def check_sonarr_library(tvdb_id: int) -> dict:
    """
    Check if a series is already in Sonarr.
    Returns: {"status": "found"|"missing"|"monitored_only", "title": ...}
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_base()}/api/v3/series",
                headers=_headers(),
                params={"tvdbId": tvdb_id},
            )
            resp.raise_for_status()
            series_list = resp.json()
        except httpx.HTTPError as e:
            logger.error("Sonarr library check error: %s", e)
            return {"status": "error", "message": str(e)}

    if not series_list:
        return {"status": "missing"}

    series = series_list[0]
    stats = series.get("statistics", {})
    if stats.get("episodeFileCount", 0) > 0:
        return {"status": "found", "title": series.get("title"), "year": series.get("year")}
    return {"status": "monitored_only", "title": series.get("title"), "year": series.get("year")}


# ── TVDB ID lookup ────────────────────────────────────────────────────────────

async def lookup_series(tvdb_id: int) -> dict | None:
    """Look up a series in Sonarr's lookup endpoint by TVDB ID."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_base()}/api/v3/series/lookup",
                headers=_headers(),
                params={"term": f"tvdb:{tvdb_id}"},
            )
            resp.raise_for_status()
            results = resp.json()
            return results[0] if results else None
        except httpx.HTTPError as e:
            logger.error("Sonarr lookup error: %s", e)
            return None


# ── Add series ────────────────────────────────────────────────────────────────

async def add_series(
    tvdb_id: int,
    title: str,
    year: int,
    original_language: str,
    season_numbers: list[int],
    plex_username: str | None = None,
) -> dict:
    """
    Add a series to Sonarr.

    Rodmappe-logik:
      - original_language == 'da' → ROOT_TV_DANSK    (/mnt/unionfs/Media/TV/TV)
      - else                      → ROOT_TV_STANDARD (/mnt/unionfs/Media/TV/Serier)
    """
    # Determine root folder
    if original_language == "da":
        root_folder = ROOT_TV_DANSK
    else:
        root_folder = ROOT_TV_STANDARD

    # Resolve tag
    tag_ids = []
    if plex_username:
        tag_id = await _get_or_create_tag(plex_username)
        if tag_id is not None:
            tag_ids = [tag_id]

    logger.info(
        "Sonarr: adding series tvdb_id=%s title='%s' lang=%s rootFolder=%s seasons=%s tags=%s",
        tvdb_id, title, original_language, root_folder, season_numbers, tag_ids,
    )

    # Build season list — all seasons monitored
    seasons = [
        {"seasonNumber": s, "monitored": True}
        for s in season_numbers
    ]

    # Fetch full series data from Sonarr lookup for required fields
    lookup = await lookup_series(tvdb_id)
    if not lookup:
        return {
            "success": False,
            "status": "lookup_failed",
            "message": f"Kunne ikke finde serien i Sonarr's database (tvdb_id={tvdb_id}).",
        }

    # Merge lookup data with our overrides
    payload = {
        **lookup,
        "qualityProfileId": SONARR_QUALITY_PROFILE_ID,
        "rootFolderPath": root_folder,
        "monitored": True,
        "seasonFolder": True,
        "tags": tag_ids,
        "seasons": seasons,
        "addOptions": {
            "searchForMissingEpisodes": True,
            "monitor": "all",
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                f"{_base()}/api/v3/series",
                headers=_headers(),
                json=payload,
            )

            if resp.status_code == 201:
                body = resp.json()
                logger.info("Sonarr: series added successfully id=%s", body.get("id"))
                season_str = ", ".join(str(s) for s in season_numbers)
                return {
                    "success": True,
                    "status": "added",
                    "sonarr_id": body.get("id"),
                    "title": body.get("title"),
                    "root_folder": root_folder,
                    "seasons_requested": season_numbers,
                    "message": (
                        f"Serien er tilføjet og søges nu! "
                        f"(Sæson {season_str}) 📺"
                    ),
                }

            if resp.status_code == 400:
                body = resp.json()
                logger.warning("Sonarr 400 response: %s", body)
                return {
                    "success": False,
                    "status": "already_exists",
                    "message": "Serien er allerede i Sonarr.",
                }

            resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            logger.error("Sonarr HTTP error: %s — body: %s", e, e.response.text)
            return {
                "success": False,
                "status": "error",
                "message": f"Sonarr fejl: HTTP {e.response.status_code}",
            }
        except httpx.HTTPError as e:
            logger.error("Sonarr connection error: %s", e)
            return {
                "success": False,
                "status": "connection_error",
                "message": "Kunne ikke forbinde til Sonarr.",
            }

    return {"success": False, "status": "unknown_error", "message": "Ukendt fejl."}