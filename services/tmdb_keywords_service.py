"""
services/tmdb_keywords_service.py - TMDB keywords + extended metadata fetcher.

CHANGES vs previous version (v0.2.0 — batch fetcher til Step 2):
  - NY: fetch_metadata_batch() — henter N items parallelt fra TMDB.
    * Bruges af /fetch_metadata admin-kommandoen
    * Throttling via semaphore (max 10 concurrent requests for at respektere TMDB rate-limit)
    * Returnerer struktureret resultat med success/error counts
    * Hver item er enten {"status": "ok", ...} eller {"status": "error/not_found", ...}
  - Eksisterende fetch_movie_metadata, fetch_tv_metadata, search_tmdb_by_title — uændret.

UNCHANGED (v0.1.0):
  - fetch_movie_metadata(tmdb_id) - henter genrer + keywords for én film
  - fetch_tv_metadata(tmdb_id)    - samme for TV-serie
  - search_tmdb_by_title(query)   - titel-søgning til /test_metadata

DESIGN-PRINCIPPER:
  - Engelsk (en-US) for konsistens med subgenre-formler
  - Robust fejlhåndtering: returnerer struktureret fejl-info, kaster ALDRIG
  - To separate API-kald per item (details + keywords), parallelt via asyncio.gather
  - Batch-fetch begrænser concurrency for ikke at hammre TMDB
"""

import asyncio
import logging

import httpx

from config import TMDB_API_KEY

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.themoviedb.org/3"
_TIMEOUT  = 15

# Max parallelle TMDB-requests per batch (respekterer ~50 req/sec rate-limit)
# 10 concurrent × ~1 sek per request = ~10 req/sec — masse safety margin
_MAX_CONCURRENT = 10


# ══════════════════════════════════════════════════════════════════════════════
# Single-item fetch (uændret fra v0.1.0)
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_movie_metadata(tmdb_id: int) -> dict:
    """
    Hent komplet TMDB-metadata for en film: titel, år, genrer (engelsk) og keywords.

    Returnerer ALTID en dict med 'status'-felt:
      - status='ok'        → alle felter populeret
      - status='not_found' → film findes ikke på TMDB
      - status='error'     → API-fejl (se 'error_message')
    """
    return await _fetch_metadata(tmdb_id, "movie")


async def fetch_tv_metadata(tmdb_id: int) -> dict:
    """Samme som fetch_movie_metadata, men for TV-serier."""
    return await _fetch_metadata(tmdb_id, "tv")


async def _fetch_metadata(tmdb_id: int, media_type: str, client: httpx.AsyncClient | None = None) -> dict:
    """
    Fælles implementation. Kører to parallelle TMDB-kald per item.

    Hvis 'client' er None, oprettes en ny midlertidig client (til single-item brug).
    Hvis 'client' er given, genbruges connection (til batch-fetch — meget hurtigere).
    """
    if media_type not in ("movie", "tv"):
        return {
            "status":        "error",
            "error_message": f"Ugyldig media_type: '{media_type}'",
            "tmdb_id":       tmdb_id,
            "media_type":    media_type,
        }

    details_url   = f"{_BASE_URL}/{media_type}/{tmdb_id}"
    keywords_url  = f"{_BASE_URL}/{media_type}/{tmdb_id}/keywords"
    common_params = {"api_key": TMDB_API_KEY, "language": "en-US"}

    # Genbruge eksisterende client hvis givet (batch-mode), ellers opret ny
    if client is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as new_client:
            return await _do_fetch(new_client, tmdb_id, media_type,
                                   details_url, keywords_url, common_params)
    return await _do_fetch(client, tmdb_id, media_type,
                           details_url, keywords_url, common_params)


async def _do_fetch(
    client:        httpx.AsyncClient,
    tmdb_id:       int,
    media_type:    str,
    details_url:   str,
    keywords_url:  str,
    common_params: dict,
) -> dict:
    """Selve fetch-logikken — ekstraheret så vi kan genbruge connection."""
    try:
        details_resp, keywords_resp = await asyncio.gather(
            client.get(details_url,  params=common_params),
            client.get(keywords_url, params={"api_key": TMDB_API_KEY}),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error("TMDB metadata fetch fejl (id=%s): %s", tmdb_id, e)
        return {
            "status":        "error",
            "error_message": str(e),
            "tmdb_id":       tmdb_id,
            "media_type":    media_type,
        }

    # ── Parse details ─────────────────────────────────────────────────────────
    if isinstance(details_resp, Exception):
        return {
            "status":        "error",
            "error_message": f"Details API exception: {details_resp}",
            "tmdb_id":       tmdb_id,
            "media_type":    media_type,
        }

    if details_resp.status_code == 404:
        return {
            "status":     "not_found",
            "tmdb_id":    tmdb_id,
            "media_type": media_type,
            "message":    f"{media_type} med tmdb_id={tmdb_id} findes ikke på TMDB",
        }

    if details_resp.status_code != 200:
        return {
            "status":        "error",
            "error_message": f"Details HTTP {details_resp.status_code}",
            "tmdb_id":       tmdb_id,
            "media_type":    media_type,
        }

    try:
        details_data = details_resp.json()
    except Exception as e:
        return {
            "status":        "error",
            "error_message": f"Details JSON-parse fejl: {e}",
            "tmdb_id":       tmdb_id,
            "media_type":    media_type,
        }

    # Title + year (TV bruger 'name' + 'first_air_date')
    if media_type == "movie":
        title    = details_data.get("title") or details_data.get("original_title") or "Ukendt"
        date_str = details_data.get("release_date") or ""
    else:
        title    = details_data.get("name") or details_data.get("original_name") or "Ukendt"
        date_str = details_data.get("first_air_date") or ""

    year = None
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        year = int(date_str[:4])

    genres = [g.get("name") for g in details_data.get("genres", []) if g.get("name")]

    # ── Parse keywords ────────────────────────────────────────────────────────
    keywords: list[str] = []
    keyword_error: str | None = None

    if isinstance(keywords_resp, Exception):
        keyword_error = f"Keywords API exception: {keywords_resp}"
    elif keywords_resp.status_code == 200:
        try:
            kw_data = keywords_resp.json()
            # /movie/{id}/keywords returnerer {"keywords": [...]}
            # /tv/{id}/keywords returnerer {"results": [...]}
            kw_list = kw_data.get("keywords") or kw_data.get("results") or []
            keywords = [k.get("name") for k in kw_list if k.get("name")]
        except Exception as e:
            keyword_error = f"Keywords JSON-parse fejl: {e}"
    elif keywords_resp.status_code == 404:
        keyword_error = "Ingen keywords tilgængelige"
    else:
        keyword_error = f"Keywords HTTP {keywords_resp.status_code}"

    # ── Byg succes-respons ────────────────────────────────────────────────────
    result = {
        "status":        "ok",
        "tmdb_id":       tmdb_id,
        "media_type":    media_type,
        "title":         title,
        "year":          year,
        "genres":        genres,
        "keywords":      keywords,
        "genre_count":   len(genres),
        "keyword_count": len(keywords),
    }

    if keyword_error:
        result["keyword_warning"] = keyword_error

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Batch fetcher (NY v0.2.0)
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_metadata_batch(items: list[dict]) -> dict:
    """
    Hent metadata for en batch af items parallelt.
    Bruges af /fetch_metadata admin-kommandoen.

    Args:
      items: liste af dicts med format:
        [{"tmdb_id": 27205, "media_type": "movie"}, ...]

    Returns:
      {
        "results": [
          {"status": "ok", "tmdb_id": 27205, "media_type": "movie", ...},
          {"status": "not_found", ...},
          {"status": "error", ...},
        ],
        "summary": {
          "total":     100,
          "ok":        87,
          "not_found": 8,
          "error":     5,
        },
        "duration_seconds": 12.34,
      }

    Throttling: max _MAX_CONCURRENT samtidige TMDB-kald via semaphore.
    Genbruger HTTP connection pool for 5-10× speedup.
    """
    import time
    start_time = time.monotonic()

    if not items:
        return {
            "results":          [],
            "summary":          {"total": 0, "ok": 0, "not_found": 0, "error": 0},
            "duration_seconds": 0.0,
        }

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    results: list[dict] = []

    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        limits=httpx.Limits(max_connections=_MAX_CONCURRENT * 2),
    ) as client:

        async def _fetch_one(item: dict) -> dict:
            async with semaphore:
                tmdb_id    = item.get("tmdb_id")
                media_type = item.get("media_type")
                if not tmdb_id or media_type not in ("movie", "tv"):
                    return {
                        "status":        "error",
                        "error_message": "Ugyldig item (manglende tmdb_id eller media_type)",
                        "tmdb_id":       tmdb_id,
                        "media_type":    media_type,
                    }
                try:
                    return await _fetch_metadata(tmdb_id, media_type, client=client)
                except Exception as e:
                    logger.warning(
                        "fetch_metadata_batch: uventet fejl for %s/%s: %s",
                        media_type, tmdb_id, e,
                    )
                    return {
                        "status":        "error",
                        "error_message": f"Uventet fejl: {e}",
                        "tmdb_id":       tmdb_id,
                        "media_type":    media_type,
                    }

        results = await asyncio.gather(*[_fetch_one(item) for item in items])

    # Optælling
    summary = {"total": len(results), "ok": 0, "not_found": 0, "error": 0}
    for r in results:
        status = r.get("status", "error")
        if status in summary:
            summary[status] += 1

    duration = time.monotonic() - start_time

    logger.info(
        "fetch_metadata_batch: %d items processed in %.1fs — "
        "ok=%d, not_found=%d, error=%d",
        summary["total"], duration, summary["ok"],
        summary["not_found"], summary["error"],
    )

    return {
        "results":          results,
        "summary":          summary,
        "duration_seconds": round(duration, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Title search (uændret fra v0.1.0)
# ══════════════════════════════════════════════════════════════════════════════

async def search_tmdb_by_title(query: str, media_type: str = "both") -> list[dict]:
    """
    Søg TMDB efter titel. Returnerer max 5 resultater.
    Bruges af /test_metadata når brugeren skriver en titel i stedet for et ID.
    """
    results: list[dict] = []
    common_params = {"api_key": TMDB_API_KEY, "language": "en-US", "query": query}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        if media_type in ("movie", "both"):
            try:
                resp = await client.get(f"{_BASE_URL}/search/movie", params=common_params)
                if resp.status_code == 200:
                    for item in resp.json().get("results", [])[:5]:
                        date_str = item.get("release_date") or ""
                        year = int(date_str[:4]) if date_str[:4].isdigit() else None
                        results.append({
                            "tmdb_id":    item.get("id"),
                            "title":      item.get("title") or item.get("original_title"),
                            "year":       year,
                            "media_type": "movie",
                            "popularity": item.get("popularity", 0),
                        })
            except Exception as e:
                logger.warning("TMDB title search (movie) fejl: %s", e)

        if media_type in ("tv", "both"):
            try:
                resp = await client.get(f"{_BASE_URL}/search/tv", params=common_params)
                if resp.status_code == 200:
                    for item in resp.json().get("results", [])[:5]:
                        date_str = item.get("first_air_date") or ""
                        year = int(date_str[:4]) if date_str[:4].isdigit() else None
                        results.append({
                            "tmdb_id":    item.get("id"),
                            "title":      item.get("name") or item.get("original_name"),
                            "year":       year,
                            "media_type": "tv",
                            "popularity": item.get("popularity", 0),
                        })
            except Exception as e:
                logger.warning("TMDB title search (tv) fejl: %s", e)

    results.sort(key=lambda r: r.get("popularity", 0), reverse=True)
    return results[:5]