"""
services/sonarr_service.py - Direkte Sonarr API integration.

CHANGES vs previous version (v0.9.9 — tag-format fix):
  - KRITISK FIX: Tag-label format ændret fra "tg_<id>" til "tg-<id>".
    Samme fix som radarr_service.py — bindestreg bruges fordi Sonarr
    (ligesom Radarr) kan afvise labels med underscore i visse versioner.
    webhook_service.py parser nu "tg-" i stedet for "tg_".

UNCHANGED:
  - check_sonarr_library(), lookup_series(), _lookup_series_by_title() — uændrede.
  - add_series(): rodmappe-logik, TVDB ID fallback, seasons-håndtering — uændret.
  - get_all_tags() — uændret.
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

# Tag-præfiks — bindestreg bruges fordi Sonarr/Radarr kan afvise underscore i labels
_TAG_PREFIX = "tg-"


def _base() -> str:
    return SONARR_URL.rstrip("/")


def _headers() -> dict:
    return {"X-Api-Key": SONARR_API_KEY, "Content-Type": "application/json"}


# ── Tag helpers ───────────────────────────────────────────────────────────────

async def _get_or_create_tag(label: str) -> int | None:
    """Find or create a Sonarr tag by label. Returns tag ID or None."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
            resp.raise_for_status()
            label_lower = label.lower().strip()
            for tag in resp.json():
                if tag.get("label", "").lower().strip() == label_lower:
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
                    "Sonarr: POST /tag 400 for '%s' — response: %s — forsøger GET igen",
                    label, body,
                )
                resp2 = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
                resp2.raise_for_status()
                for tag in resp2.json():
                    if tag.get("label", "").lower().strip() == label_lower:
                        logger.info("Sonarr: fandt tag '%s' efter 400-fallback id=%s", label, tag["id"])
                        return tag["id"]
                logger.warning("Sonarr: kunne ikke oprette tag '%s' — bruges uden tag", label)
                return None

            resp.raise_for_status()
            tag_id = resp.json().get("id")
            logger.info("Sonarr: created tag '%s' with id=%s", label, tag_id)
            return tag_id

        except httpx.HTTPError as e:
            logger.error("Sonarr tag error: %s", e)
            return None


async def get_all_tags() -> dict[int, str]:
    """
    Return a mapping of {tag_id: label} for all tags in Sonarr.
    Used by webhook_service to resolve integer tag IDs to labels.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{_base()}/api/v3/tag", headers=_headers())
            resp.raise_for_status()
            return {t["id"]: t["label"] for t in resp.json()}
        except httpx.HTTPError as e:
            logger.error("Sonarr get_all_tags error: %s", e)
            return {}


# ── Library check ─────────────────────────────────────────────────────────────

async def check_sonarr_library(tvdb_id: int) -> dict:
    """Check if a series is already in Sonarr."""
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


# ── Lookup helpers ────────────────────────────────────────────────────────────

async def lookup_series(tvdb_id: int) -> dict | None:
    """Look up a series in Sonarr by TVDB ID."""
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
            logger.error("Sonarr lookup error (tvdb_id=%s): %s", tvdb_id, e)
            return None


async def _lookup_series_by_title(title: str) -> dict | None:
    """
    Fallback: søg Sonarr efter seriens titel og returner første match.
    Sonarr bruger SkyHook som kilde og finder ofte serier som TMDB mangler TVDB ID på.
    """
    logger.info("Sonarr: TVDB ID missing, attempting title search fallback for '%s'", title)
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_base()}/api/v3/series/lookup",
                headers=_headers(),
                params={"term": title},
            )
            resp.raise_for_status()
            results = resp.json()
        except httpx.HTTPError as e:
            logger.error("Sonarr title lookup error for '%s': %s", title, e)
            return None

    if not results:
        logger.warning("Sonarr title search returned 0 results for '%s'", title)
        return None

    title_lower = title.lower()
    for result in results:
        if (result.get("title") or "").lower() == title_lower:
            logger.info("Sonarr title search exact match: '%s' → tvdb_id=%s",
                        result.get("title"), result.get("tvdbId"))
            return result

    first = results[0]
    logger.info("Sonarr title search best match: '%s' → tvdb_id=%s",
                first.get("title"), first.get("tvdbId"))
    return first


# ── Add series ────────────────────────────────────────────────────────────────

async def add_series(
    tvdb_id: int | None,
    title: str,
    year: int,
    original_language: str,
    season_numbers: list[int],
    telegram_id: int | None = None,
) -> dict:
    """
    Add a series to Sonarr.

    Tag-strategi: opretter/henter tagget "tg-<telegram_id>" (bindestreg)
    så webhook_service kan udtrække telegram_id direkte fra label-navnet
    uden DB-opslag. Bindestreg bruges fordi Sonarr/Radarr kan afvise underscore.

    Rodmappe-logik:
      - original_language == 'da' → ROOT_TV_DANSK
      - else                      → ROOT_TV_STANDARD

    TVDB ID fallback:
      1. Brug tvdb_id fra TMDB hvis tilgængeligt.
      2. Hvis mangler: søg Sonarr på titel (SkyHook-kilde).
      3. Hvis stadig ingen match: returner brugervenlig fejl.
    """
    lookup = None

    if tvdb_id:
        lookup = await lookup_series(tvdb_id)
        if lookup:
            logger.info("Sonarr: found series via tvdb_id=%s", tvdb_id)

    if not lookup:
        lookup = await _lookup_series_by_title(title)

    if not lookup:
        return {
            "success": False,
            "status": "not_found",
            "message": (
                f"Jeg kunne desværre ikke finde de nødvendige tekniske data på "
                f"'{title}' lige nu. Prøv igen om et par dage, "
                f"når den er oprettet i de store databaser."
            ),
        }

    resolved_tvdb_id = lookup.get("tvdbId") or tvdb_id
    logger.info("Sonarr: resolved tvdb_id=%s for '%s'", resolved_tvdb_id, title)

    root_folder = ROOT_TV_DANSK if original_language == "da" else ROOT_TV_STANDARD

    tag_ids = []
    if telegram_id:
        tag_label = f"{_TAG_PREFIX}{telegram_id}"   # f.eks. "tg-731397952"
        tag_id = await _get_or_create_tag(tag_label)
        if tag_id is not None:
            tag_ids = [tag_id]
        else:
            logger.warning(
                "Sonarr: tag '%s' ikke oprettet — '%s' tilføjes uden notifikations-tag",
                tag_label, title,
            )

    seasons = [{"seasonNumber": s, "monitored": True} for s in season_numbers]

    logger.info(
        "Sonarr: adding '%s' tvdb_id=%s lang=%s rootFolder=%s seasons=%s tags=%s",
        title, resolved_tvdb_id, original_language, root_folder, season_numbers, tag_ids,
    )

    payload = {
        **lookup,
        "qualityProfileId": SONARR_QUALITY_PROFILE_ID,
        "rootFolderPath":   root_folder,
        "monitored":        True,
        "seasonFolder":     True,
        "tags":             tag_ids,
        "seasons":          seasons,
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
                    "status":  "added",
                    "sonarr_id": body.get("id"),
                    "title":     body.get("title"),
                    "root_folder": root_folder,
                    "seasons_requested": season_numbers,
                    "message": f"Serien er tilføjet og søges nu! (Sæson {season_str}) 📺",
                }

            if resp.status_code == 400:
                body = resp.json()
                errors = body if isinstance(body, list) else [body]
                if any("already" in str(e).lower() or "exists" in str(e).lower() for e in errors):
                    return {
                        "success": False,
                        "status":  "already_exists",
                        "message": f"'{title}' er allerede i Sonarr.",
                    }
                logger.error("Sonarr 400: %s", body)
                return {
                    "success": False,
                    "status": "error",
                    "message": f"Sonarr 400: {body}",
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