"""
services/tmdb_service.py - TMDB API integration.

All requests use language=da-DK for Danish titles and overviews.
Images are returned as full URLs ready to send in Telegram.
"""

import logging

import httpx

from config import TMDB_API_KEY

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_BASE_URL = "https://api.themoviedb.org/3"
_POSTER_BASE = "https://image.tmdb.org/t/p/w500"
_BACKDROP_BASE = "https://image.tmdb.org/t/p/original"
_PROFILE_BASE = "https://image.tmdb.org/t/p/w185"
_LANGUAGE = "da-DK"

# Genre ID → Danish label (film)
_MOVIE_GENRES: dict[int, str] = {
    28: "Action", 12: "Eventyr", 16: "Animation", 35: "Komedie",
    80: "Krimi", 99: "Dokumentar", 18: "Drama", 10751: "Familie",
    14: "Fantasy", 36: "Historie", 27: "Horror", 10402: "Musik",
    9648: "Mysterium", 10749: "Romantik", 878: "Science Fiction",
    10770: "TV-film", 53: "Thriller", 10752: "Krig", 37: "Western",
}

# Genre ID → Danish label (serier)
_TV_GENRES: dict[int, str] = {
    10759: "Action & Eventyr", 16: "Animation", 35: "Komedie",
    80: "Krimi", 99: "Dokumentar", 18: "Drama", 10751: "Familie",
    10762: "Børn", 9648: "Mysterium", 10763: "Nyheder",
    10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Sæbeopera",
    10767: "Talkshow", 10768: "Krig & Politik", 37: "Western",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _params(**kwargs) -> dict:
    """Build a parameter dict with auth and language pre-filled."""
    return {"api_key": TMDB_API_KEY, "language": _LANGUAGE, **kwargs}


def _poster_url(path: str | None) -> str | None:
    return f"{_POSTER_BASE}{path}" if path else None


def _backdrop_url(path: str | None) -> str | None:
    return f"{_BACKDROP_BASE}{path}" if path else None


def _profile_url(path: str | None) -> str | None:
    return f"{_PROFILE_BASE}{path}" if path else None


def _genre_names(genre_ids: list[int], is_tv: bool = False) -> list[str]:
    table = _TV_GENRES if is_tv else _MOVIE_GENRES
    return [table.get(gid, str(gid)) for gid in genre_ids]


def _format_movie_result(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title") or item.get("original_title"),
        "overview": item.get("overview") or "Ingen beskrivelse tilgængelig.",
        "release_date": item.get("release_date", "Ukendt"),
        "vote_average": round(item.get("vote_average", 0), 1),
        "genres": _genre_names(item.get("genre_ids", []), is_tv=False),
        "poster_url": _poster_url(item.get("poster_path")),
        "backdrop_url": _backdrop_url(item.get("backdrop_path")),
        "media_type": "movie",
    }


def _format_tv_result(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("name") or item.get("original_name"),
        "overview": item.get("overview") or "Ingen beskrivelse tilgængelig.",
        "release_date": item.get("first_air_date", "Ukendt"),
        "vote_average": round(item.get("vote_average", 0), 1),
        "genres": _genre_names(item.get("genre_ids", []), is_tv=True),
        "poster_url": _poster_url(item.get("poster_path")),
        "backdrop_url": _backdrop_url(item.get("backdrop_path")),
        "media_type": "tv",
    }


def _format_provider_list(providers: list[dict]) -> list[str]:
    """Extract provider names from a list of provider dicts."""
    return [p["provider_name"] for p in providers if "provider_name" in p]


# ── Public functions ──────────────────────────────────────────────────────────

async def search_media(query: str, media_type: str = "both") -> list[dict]:
    """Search TMDB for films and/or TV shows matching the query."""
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=10) as client:
        if media_type in ("movie", "both"):
            try:
                resp = await client.get(
                    f"{_BASE_URL}/search/movie",
                    params=_params(query=query),
                )
                resp.raise_for_status()
                for item in resp.json().get("results", [])[:5]:
                    results.append(_format_movie_result(item))
            except httpx.HTTPError as e:
                logger.error("TMDB movie search error: %s", e)

        if media_type in ("tv", "both"):
            try:
                resp = await client.get(
                    f"{_BASE_URL}/search/tv",
                    params=_params(query=query),
                )
                resp.raise_for_status()
                for item in resp.json().get("results", [])[:5]:
                    results.append(_format_tv_result(item))
            except httpx.HTTPError as e:
                logger.error("TMDB TV search error: %s", e)

    return results


async def get_media_details(tmdb_id: int, media_type: str) -> dict | None:
    """Fetch full details for a specific film or TV show."""
    endpoint = "movie" if media_type == "movie" else "tv"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/{endpoint}/{tmdb_id}",
                params=_params(),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB details error (id=%s type=%s): %s", tmdb_id, media_type, e)
            return None

    if media_type == "movie":
        return {
            "id": data.get("id"),
            "title": data.get("title") or data.get("original_title"),
            "tagline": data.get("tagline"),
            "overview": data.get("overview") or "Ingen beskrivelse tilgængelig.",
            "release_date": data.get("release_date", "Ukendt"),
            "runtime_minutes": data.get("runtime"),
            "vote_average": round(data.get("vote_average", 0), 1),
            "genres": [g["name"] for g in data.get("genres", [])],
            "poster_url": _poster_url(data.get("poster_path")),
            "backdrop_url": _backdrop_url(data.get("backdrop_path")),
            "imdb_id": data.get("imdb_id"),
            "media_type": "movie",
        }
    else:
        return {
            "id": data.get("id"),
            "title": data.get("name") or data.get("original_name"),
            "tagline": data.get("tagline"),
            "overview": data.get("overview") or "Ingen beskrivelse tilgængelig.",
            "first_air_date": data.get("first_air_date", "Ukendt"),
            "number_of_seasons": data.get("number_of_seasons"),
            "number_of_episodes": data.get("number_of_episodes"),
            "vote_average": round(data.get("vote_average", 0), 1),
            "genres": [g["name"] for g in data.get("genres", [])],
            "poster_url": _poster_url(data.get("poster_path")),
            "backdrop_url": _backdrop_url(data.get("backdrop_path")),
            "status": data.get("status"),
            "media_type": "tv",
        }


async def search_person(query: str) -> list[dict]:
    """Search TMDB for actors, directors, and other crew members."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/search/person",
                params=_params(query=query),
            )
            resp.raise_for_status()
            raw_results = resp.json().get("results", [])[:5]
        except httpx.HTTPError as e:
            logger.error("TMDB person search error: %s", e)
            return []

    results = []
    for person in raw_results:
        known_for = []
        for item in person.get("known_for", []):
            title = item.get("title") or item.get("name") or item.get("original_title")
            year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
            media = "film" if item.get("media_type") == "movie" else "serie"
            if title:
                known_for.append(f"{title} ({year}) [{media}]" if year else f"{title} [{media}]")

        results.append({
            "id": person.get("id"),
            "name": person.get("name"),
            "known_for_department": person.get("known_for_department", "Ukendt"),
            "popularity": round(person.get("popularity", 0), 1),
            "profile_url": _profile_url(person.get("profile_path")),
            "known_for": known_for,
        })

    return results


async def get_person_filmography(person_id: int) -> dict | None:
    """Fetch the full filmography (movie + TV credits) for a person."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            bio_resp = await client.get(
                f"{_BASE_URL}/person/{person_id}",
                params=_params(),
            )
            bio_resp.raise_for_status()
            bio = bio_resp.json()

            credits_resp = await client.get(
                f"{_BASE_URL}/person/{person_id}/combined_credits",
                params=_params(),
            )
            credits_resp.raise_for_status()
            credits = credits_resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB filmography error (id=%s): %s", person_id, e)
            return None

    movie_credits = sorted(
        [
            {
                "id": item.get("id"),
                "title": item.get("title") or item.get("original_title"),
                "release_date": item.get("release_date", "")[:4] or "Ukendt",
                "character": item.get("character"),
                "vote_average": round(item.get("vote_average", 0), 1),
            }
            for item in credits.get("cast", [])
            if item.get("media_type") == "movie"
        ],
        key=lambda x: x["release_date"],
        reverse=True,
    )

    tv_credits = sorted(
        [
            {
                "id": item.get("id"),
                "title": item.get("name") or item.get("original_name"),
                "first_air_date": item.get("first_air_date", "")[:4] or "Ukendt",
                "character": item.get("character"),
                "vote_average": round(item.get("vote_average", 0), 1),
            }
            for item in credits.get("cast", [])
            if item.get("media_type") == "tv"
        ],
        key=lambda x: x["first_air_date"],
        reverse=True,
    )

    crew_credits = sorted(
        [
            {
                "id": item.get("id"),
                "title": item.get("title") or item.get("original_title"),
                "release_date": item.get("release_date", "")[:4] or "Ukendt",
                "job": item.get("job"),
            }
            for item in credits.get("crew", [])
            if item.get("media_type") == "movie"
        ],
        key=lambda x: x["release_date"],
        reverse=True,
    )

    return {
        "id": bio.get("id"),
        "name": bio.get("name"),
        "biography": (bio.get("biography") or "Ingen biografi tilgængelig.")[:500],
        "birthday": bio.get("birthday"),
        "place_of_birth": bio.get("place_of_birth"),
        "known_for_department": bio.get("known_for_department", "Ukendt"),
        "profile_url": _profile_url(bio.get("profile_path")),
        "movie_credits": movie_credits[:10],
        "tv_credits": tv_credits[:10],
        "crew_credits": crew_credits[:10],
    }


async def get_trending() -> list[dict]:
    """
    Fetch the trending films and TV shows in Denmark this week.

    Returns:
        A list of up to 10 trending titles (mixed film + TV).
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/trending/all/week",
                params=_params(),
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])[:10]
        except httpx.HTTPError as e:
            logger.error("TMDB trending error: %s", e)
            return []

    formatted = []
    for item in results:
        media_type = item.get("media_type")
        if media_type == "movie":
            formatted.append(_format_movie_result(item))
        elif media_type == "tv":
            formatted.append(_format_tv_result(item))
        # Skip "person" entries that occasionally appear in trending

    return formatted


async def get_recommendations(tmdb_id: int, media_type: str) -> list[dict]:
    """
    Fetch recommendations similar to a specific film or TV show.

    Args:
        tmdb_id:    The TMDB ID of the title.
        media_type: "movie" or "tv".

    Returns:
        A list of up to 8 recommended titles.
    """
    endpoint = "movie" if media_type == "movie" else "tv"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/{endpoint}/{tmdb_id}/recommendations",
                params=_params(),
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])[:8]
        except httpx.HTTPError as e:
            logger.error("TMDB recommendations error (id=%s type=%s): %s", tmdb_id, media_type, e)
            return []

    if media_type == "movie":
        return [_format_movie_result(item) for item in results]
    return [_format_tv_result(item) for item in results]


async def get_watch_providers(tmdb_id: int, media_type: str) -> dict:
    """
    Fetch Danish streaming providers (DK) for a specific film or TV show.

    Args:
        tmdb_id:    The TMDB ID of the title.
        media_type: "movie" or "tv".

    Returns:
        A dict with lists of providers split by type:
        flatrate (subscription), rent, buy — all filtered for Denmark only.
    """
    endpoint = "movie" if media_type == "movie" else "tv"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/{endpoint}/{tmdb_id}/watch/providers",
                params={"api_key": TMDB_API_KEY},  # No language param for this endpoint
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB watch providers error (id=%s type=%s): %s", tmdb_id, media_type, e)
            return {"available_in_dk": False}

    # Filter for Denmark only.
    dk_data = data.get("results", {}).get("DK")
    if not dk_data:
        return {"available_in_dk": False}

    return {
        "available_in_dk": True,
        "link": dk_data.get("link"),
        "flatrate": _format_provider_list(dk_data.get("flatrate", [])),
        "rent": _format_provider_list(dk_data.get("rent", [])),
        "buy": _format_provider_list(dk_data.get("buy", [])),
    }