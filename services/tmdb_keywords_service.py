"""
services/tmdb_keywords_service.py - TMDB keywords + extended metadata fetcher.

CHANGES (v0.1.0 — initial implementation, Step 1 af subgenre-projekt):
  - NY SERVICE: Henter komplet TMDB-metadata (genrer + keywords) for et givet
    TMDB ID. Bygges som første komponent i vores subgenre-detektion.
  - Bruges initialt af /test_metadata admin-kommandoen til at vurdere
    keyword-kvaliteten manuelt FØR vi designer database-schemaet.
  - I Step 2/3 vil samme service blive brugt af det resumable
    "støvsuger-script" til at populere PostgreSQL-cachen.

DESIGN-PRINCIPPER:
  - Engelsk (en-US) for keywords — dansk er ikke understøttet konsistent
  - Engelsk (en-US) for genrer — for at matche subgenre-formler
  - Robust fejlhåndtering: returnerer struktureret fejl-info, kaster ALDRIG
  - To separate API-kald, kørt i parallel via asyncio.gather for hurtighed
  - Resolver titel + år samtidigt (smart UX i /test_metadata output)
"""

import asyncio
import logging

import httpx

from config import TMDB_API_KEY

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.themoviedb.org/3"
_TIMEOUT  = 15


async def fetch_movie_metadata(tmdb_id: int) -> dict:
    """
    Hent komplet TMDB-metadata for en film: titel, år, genrer (engelsk) og keywords.

    Returnerer ALTID en dict med 'status'-felt:
      - status='ok'        → alle felter populeret
      - status='not_found' → film findes ikke på TMDB
      - status='error'     → API-fejl (se 'error_message')

    Eksempel på success-respons:
      {
        "status":      "ok",
        "tmdb_id":     27205,
        "title":       "Inception",
        "year":        2010,
        "genres":      ["Action", "Adventure", "Mystery", "Science Fiction", "Thriller"],
        "keywords":    ["dream", "subconscious", "heist", "mind-bending", ...],
        "genre_count": 5,
        "keyword_count": 12,
      }
    """
    return await _fetch_metadata(tmdb_id, "movie")


async def fetch_tv_metadata(tmdb_id: int) -> dict:
    """Samme som fetch_movie_metadata, men for TV-serier."""
    return await _fetch_metadata(tmdb_id, "tv")


async def _fetch_metadata(tmdb_id: int, media_type: str) -> dict:
    """
    Fælles implementation. Kører to parallelle TMDB-kald:
      1. /movie/{id}  eller /tv/{id}        → titel, år, genrer
      2. /movie/{id}/keywords eller /tv/{id}/keywords → keywords
    """
    if media_type not in ("movie", "tv"):
        return {
            "status":        "error",
            "error_message": f"Ugyldig media_type: '{media_type}'",
            "tmdb_id":       tmdb_id,
        }

    details_url  = f"{_BASE_URL}/{media_type}/{tmdb_id}"
    keywords_url = f"{_BASE_URL}/{media_type}/{tmdb_id}/keywords"
    common_params = {"api_key": TMDB_API_KEY, "language": "en-US"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
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
            }

    # ── Parse details ─────────────────────────────────────────────────────────
    if isinstance(details_resp, Exception):
        return {
            "status":        "error",
            "error_message": f"Details API exception: {details_resp}",
            "tmdb_id":       tmdb_id,
        }

    if details_resp.status_code == 404:
        return {
            "status":  "not_found",
            "tmdb_id": tmdb_id,
            "message": f"Film/serie med tmdb_id={tmdb_id} findes ikke på TMDB",
        }

    if details_resp.status_code != 200:
        return {
            "status":        "error",
            "error_message": f"Details HTTP {details_resp.status_code}",
            "tmdb_id":       tmdb_id,
        }

    try:
        details_data = details_resp.json()
    except Exception as e:
        return {
            "status":        "error",
            "error_message": f"Details JSON-parse fejl: {e}",
            "tmdb_id":       tmdb_id,
        }

    # Title + year (TV bruger 'name' + 'first_air_date')
    if media_type == "movie":
        title       = details_data.get("title") or details_data.get("original_title") or "Ukendt"
        date_str    = details_data.get("release_date") or ""
    else:
        title       = details_data.get("name") or details_data.get("original_name") or "Ukendt"
        date_str    = details_data.get("first_air_date") or ""

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

    logger.info(
        "TMDB metadata: '%s' (%s) — %d genrer, %d keywords",
        title, year or "?", len(genres), len(keywords),
    )
    return result


async def search_tmdb_by_title(query: str, media_type: str = "both") -> list[dict]:
    """
    Søg TMDB efter titel. Returnerer max 5 resultater.
    Bruges af /test_metadata når brugeren skriver en titel i stedet for et ID.

    Returnerer:
      [
        {"tmdb_id": 27205, "title": "Inception", "year": 2010, "media_type": "movie"},
        ...
      ]
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

    # Sorter efter popularity (mest relevant først)
    results.sort(key=lambda r: r.get("popularity", 0), reverse=True)
    return results[:5]