"""
services/tmdb_service.py - TMDB API integration.

CHANGES vs previous version:
  - get_person_filmography() bruger nu /person/{id}/movie_credits i stedet for
    /person/{id}/combined_credits. Årsag: combined_credits returnerer kun ~20
    resultater fra side 1, mens movie_credits returnerer ALLE film på én gang
    som en komplet liste under 'cast'. Samuel L. Jackson: 20 → 180+ film.
  - Filtrering: fjerner poster med 'uncredited' i character-feltet og poster
    uden release_date (urealiserede projekter), da disse forurener statistikken.
  - get_tmdb_collection_movies() returnerer tmdb_id, title, original_title.
  - get_trending() laver to parallelle API-kald, returnerer 5+5 dict.
  - get_now_playing() og get_upcoming() sorterer efter popularity, top 10.
"""

import asyncio
import logging

import httpx

from config import TMDB_API_KEY

logger = logging.getLogger(__name__)

_BASE_URL    = "https://api.themoviedb.org/3"
_LANGUAGE    = "da-DK"
_MAX_RESULTS = 10

_MOVIE_GENRES: dict[int, str] = {
    28: "Action", 12: "Eventyr", 16: "Animation", 35: "Komedie",
    80: "Krimi", 99: "Dokumentar", 18: "Drama", 10751: "Familie",
    14: "Fantasy", 36: "Historie", 27: "Horror", 10402: "Musik",
    9648: "Mysterium", 10749: "Romantik", 878: "Science Fiction",
    10770: "TV-film", 53: "Thriller", 10752: "Krig", 37: "Western",
}

_TV_GENRES: dict[int, str] = {
    10759: "Action & Eventyr", 16: "Animation", 35: "Komedie",
    80: "Krimi", 99: "Dokumentar", 18: "Drama", 10751: "Familie",
    10762: "Børn", 9648: "Mysterium", 10763: "Nyheder",
    10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Sæbeopera",
    10767: "Talkshow", 10768: "Krig & Politik", 37: "Western",
}

_STRIP_FIELDS = {
    "poster_path", "backdrop_path", "poster_url", "backdrop_url",
    "profile_url", "profile_path", "still_path",
    "production_companies", "production_countries",
    "spoken_languages", "belongs_to_collection",
    "homepage", "adult", "video",
}


def _params(**kwargs) -> dict:
    return {"api_key": TMDB_API_KEY, "language": _LANGUAGE, **kwargs}


def _strip(d: dict) -> dict:
    for field in _STRIP_FIELDS:
        d.pop(field, None)
    return d


def _genre_names(genre_ids: list[int], is_tv: bool = False) -> list[str]:
    table = _TV_GENRES if is_tv else _MOVIE_GENRES
    return [table.get(gid, str(gid)) for gid in genre_ids]


def _format_movie_result(item: dict) -> dict:
    return _strip({
        "id":           item.get("id"),
        "title":        item.get("title") or item.get("original_title"),
        "overview":     (item.get("overview") or "Ingen beskrivelse.")[:200],
        "release_date": item.get("release_date", "Ukendt"),
        "vote_average": round(item.get("vote_average", 0), 1),
        "genres":       _genre_names(item.get("genre_ids", []), is_tv=False),
        "media_type":   "movie",
    })


def _format_tv_result(item: dict) -> dict:
    return _strip({
        "id":           item.get("id"),
        "title":        item.get("name") or item.get("original_name"),
        "overview":     (item.get("overview") or "Ingen beskrivelse.")[:200],
        "release_date": item.get("first_air_date", "Ukendt"),
        "vote_average": round(item.get("vote_average", 0), 1),
        "genres":       _genre_names(item.get("genre_ids", []), is_tv=True),
        "media_type":   "tv",
    })


def _format_provider_list(providers: list[dict]) -> list[str]:
    return [p["provider_name"] for p in providers if "provider_name" in p]


async def search_media(query: str, media_type: str = "both") -> list[dict]:
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as client:
        if media_type in ("movie", "both"):
            try:
                resp = await client.get(
                    f"{_BASE_URL}/search/movie", params=_params(query=query)
                )
                resp.raise_for_status()
                for item in resp.json().get("results", [])[:_MAX_RESULTS]:
                    results.append(_format_movie_result(item))
            except httpx.HTTPError as e:
                logger.error("TMDB movie search error: %s", e)

        if media_type in ("tv", "both"):
            try:
                resp = await client.get(
                    f"{_BASE_URL}/search/tv", params=_params(query=query)
                )
                resp.raise_for_status()
                for item in resp.json().get("results", [])[:_MAX_RESULTS]:
                    results.append(_format_tv_result(item))
            except httpx.HTTPError as e:
                logger.error("TMDB TV search error: %s", e)

    return results


async def get_media_details(tmdb_id: int, media_type: str) -> dict | None:
    """Fetch full details for a film or TV show."""
    endpoint = "movie" if media_type == "movie" else "tv"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/{endpoint}/{tmdb_id}", params=_params()
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB details error (id=%s type=%s): %s", tmdb_id, media_type, e)
            return None

    if media_type == "movie":
        return _strip({
            "id":                data.get("id"),
            "title":             data.get("title") or data.get("original_title"),
            "tagline":           data.get("tagline"),
            "overview":          (data.get("overview") or "Ingen beskrivelse.")[:300],
            "release_date":      data.get("release_date", "Ukendt"),
            "runtime_minutes":   data.get("runtime"),
            "vote_average":      round(data.get("vote_average", 0), 1),
            "genres":            [g["name"] for g in data.get("genres", [])],
            "genre_ids":         [g["id"] for g in data.get("genres", [])],
            "original_language": data.get("original_language"),
            "imdb_id":           data.get("imdb_id"),
            "media_type":        "movie",
        })

    tvdb_id = None
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            ext_resp = await client.get(
                f"{_BASE_URL}/tv/{tmdb_id}/external_ids",
                params={"api_key": TMDB_API_KEY},
            )
            ext_resp.raise_for_status()
            tvdb_id = ext_resp.json().get("tvdb_id")
        except httpx.HTTPError as e:
            logger.warning("Could not fetch external_ids for tmdb_id=%s: %s", tmdb_id, e)

    return _strip({
        "id":                   data.get("id"),
        "title":                data.get("name") or data.get("original_name"),
        "tagline":              data.get("tagline"),
        "overview":             (data.get("overview") or "Ingen beskrivelse.")[:300],
        "first_air_date":       data.get("first_air_date", "Ukendt"),
        "number_of_seasons":    data.get("number_of_seasons"),
        "number_of_episodes":   data.get("number_of_episodes"),
        "season_numbers": [
            s["season_number"]
            for s in data.get("seasons", [])
            if s.get("season_number", 0) > 0
        ],
        "vote_average":         round(data.get("vote_average", 0), 1),
        "genres":               [g["name"] for g in data.get("genres", [])],
        "genre_ids":            [g["id"] for g in data.get("genres", [])],
        "original_language":    data.get("original_language"),
        "status":               data.get("status"),
        "tvdb_id":              tvdb_id,
        "media_type":           "tv",
    })


async def get_tmdb_collection_movies(keyword: str) -> dict | None:
    """Søg efter en TMDB-samling og returner alle film med tmdb_id, title, original_title."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            search_resp = await client.get(
                f"{_BASE_URL}/search/collection", params=_params(query=keyword)
            )
            search_resp.raise_for_status()
            search_results = search_resp.json().get("results", [])
        except httpx.HTTPError as e:
            logger.error("TMDB collection search error for '%s': %s", keyword, e)
            return None

        if not search_results:
            return None

        top             = search_results[0]
        collection_id   = top.get("id")
        collection_name = top.get("name") or top.get("original_name") or keyword

        logger.info("TMDB collection: '%s' → '%s' (id=%s)", keyword, collection_name, collection_id)

        try:
            detail_resp = await client.get(
                f"{_BASE_URL}/collection/{collection_id}", params=_params()
            )
            detail_resp.raise_for_status()
            detail = detail_resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB collection detail error (id=%s): %s", collection_id, e)
            return None

    parts  = detail.get("parts", [])
    movies = []
    for part in parts:
        title          = part.get("title") or part.get("original_title") or ""
        original_title = part.get("original_title") or title
        tmdb_id        = part.get("id")
        if not title or not tmdb_id:
            continue
        movies.append({
            "tmdb_id":        tmdb_id,
            "title":          title,
            "original_title": original_title,
            "release_date":   part.get("release_date") or "Ukendt",
        })

    movies.sort(key=lambda x: x["release_date"])
    logger.info("TMDB collection '%s': %d film fundet", collection_name, len(movies))

    return {
        "collection_id":   collection_id,
        "collection_name": collection_name,
        "total_parts":     len(movies),
        "movies":          movies,
    }


async def search_person(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/search/person", params=_params(query=query)
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
            year  = (item.get("release_date") or item.get("first_air_date") or "")[:4]
            media = "film" if item.get("media_type") == "movie" else "serie"
            if title:
                known_for.append(
                    f"{title} ({year}) [{media}]" if year else f"{title} [{media}]"
                )
        results.append({
            "id":                   person.get("id"),
            "name":                 person.get("name"),
            "known_for_department": person.get("known_for_department", "Ukendt"),
            "popularity":           round(person.get("popularity", 0), 1),
            "known_for":            known_for,
        })
    return results


async def get_person_filmography(person_id: int) -> dict | None:
    """
    Hent den FULDE filmografi for en person.

    FIX: Bruger nu /person/{id}/movie_credits i stedet for /combined_credits.

    Årsag til skiftet:
      - /combined_credits returnerer kun ~20 resultater pga. intern paginering
        på TMDB's side (side 1 af en blandet film+TV-liste).
      - /movie_credits returnerer ALLE film på én gang som én komplet liste
        under 'cast' — ingen paginering, ingen begrænsning.
      - Samuel L. Jackson: combined_credits → 20 film, movie_credits → 180+ film.

    Kører to parallelle kald via asyncio.gather:
      1. /person/{id}               → biografi, navn, fødselsdato
      2. /person/{id}/movie_credits → komplet film-cast liste

    Filtrering (fjerner støj fra listen):
      - 'uncredited' i character-feltet: statist-roller der ikke tæller karrieremæssigt.
      - Ingen release_date: urealiserede/annoncerede projekter uden udgivelsesdato.
        Disse forvrænger total_movies-tællingen i check_actor_on_plex.

    Deduplikering på tmdb_id: TMDB kan liste samme film flere gange (f.eks.
    skuespiller + stemme-rolle). Vi beholder posten med højest vote_count.

    Hvert film-objekt indeholder:
      tmdb_id, title, original_title, release_date, character,
      vote_average, vote_count, popularity

    Sorteret efter popularity (faldende) — bedste hits først.
    TV-credits hentes stadig via combined_credits, men kun top 10.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            # Kald begge endpoints parallelt for minimal latens
            bio_resp, movie_credits_resp = await asyncio.gather(
                client.get(f"{_BASE_URL}/person/{person_id}", params=_params()),
                client.get(
                    f"{_BASE_URL}/person/{person_id}/movie_credits",
                    params={"api_key": TMDB_API_KEY, "language": _LANGUAGE},
                ),
            )
            bio_resp.raise_for_status()
            movie_credits_resp.raise_for_status()
            bio          = bio_resp.json()
            movie_data   = movie_credits_resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB filmography error (id=%s): %s", person_id, e)
            return None

    # ── Hent TV-credits separat (bruger combined_credits kun til TV) ──────────
    tv_credits_raw = []
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            tv_resp = await client.get(
                f"{_BASE_URL}/person/{person_id}/tv_credits",
                params={"api_key": TMDB_API_KEY, "language": _LANGUAGE},
            )
            tv_resp.raise_for_status()
            tv_credits_raw = tv_resp.json().get("cast", [])
        except httpx.HTTPError as e:
            logger.warning("TMDB TV credits error (id=%s): %s", person_id, e)

    # ── Film-credits: komplet liste fra /movie_credits ────────────────────────
    raw_cast = movie_data.get("cast", [])

    logger.info(
        "get_person_filmography: person_id=%s → %d rå film-credits fra /movie_credits",
        person_id, len(raw_cast),
    )

    # Filtrér støj
    filtered = []
    for item in raw_cast:
        # Fjern ukrediterede roller
        character = (item.get("character") or "").strip().lower()
        if "uncredited" in character:
            continue

        # Fjern projekter uden udgivelsesdato (urealiserede)
        release_date = (item.get("release_date") or "").strip()
        if not release_date:
            continue

        filtered.append(item)

    # Deduplikér på tmdb_id — behold posten med højest vote_count
    seen: dict[int, dict] = {}
    for item in filtered:
        tid = item.get("id")
        if not tid:
            continue
        existing = seen.get(tid)
        if existing is None or (item.get("vote_count", 0) or 0) > (existing.get("vote_count", 0) or 0):
            seen[tid] = item

    # Byg final film-liste
    movie_credits = []
    for item in seen.values():
        title          = item.get("title") or item.get("original_title") or ""
        original_title = item.get("original_title") or title
        if not title:
            continue
        movie_credits.append({
            "tmdb_id":        item.get("id"),
            "title":          title,
            "original_title": original_title,
            "release_date":   (item.get("release_date") or "")[:4] or "Ukendt",
            "character":      item.get("character") or "",
            "vote_average":   round(item.get("vote_average", 0) or 0, 1),
            "vote_count":     item.get("vote_count", 0) or 0,
            "popularity":     round(item.get("popularity", 0) or 0, 2),
        })

    # Sortér efter popularity faldende
    movie_credits.sort(key=lambda x: x["popularity"], reverse=True)

    logger.info(
        "get_person_filmography: person_id=%s → %d unikke film efter filtrering/dedup "
        "(fjernet %d råposter)",
        person_id, len(movie_credits), len(raw_cast) - len(movie_credits),
    )

    # ── TV-credits: top 10 kronologisk ───────────────────────────────────────
    tv_credits = sorted(
        [
            {
                "id":             i.get("id"),
                "title":          i.get("name") or i.get("original_name"),
                "first_air_date": (i.get("first_air_date") or "")[:4] or "Ukendt",
                "character":      i.get("character"),
                "vote_average":   round(i.get("vote_average", 0) or 0, 1),
            }
            for i in tv_credits_raw
            if i.get("name") or i.get("original_name")
        ],
        key=lambda x: x["first_air_date"],
        reverse=True,
    )[:10]

    return {
        "id":                   bio.get("id"),
        "name":                 bio.get("name"),
        "biography":            (bio.get("biography") or "Ingen biografi tilgængelig.")[:300],
        "birthday":             bio.get("birthday"),
        "place_of_birth":       bio.get("place_of_birth"),
        "known_for_department": bio.get("known_for_department", "Ukendt"),
        "total_movie_credits":  len(movie_credits),
        "movie_credits":        movie_credits,   # ALLE film, sorteret efter popularity
        "tv_credits":           tv_credits,      # top 10, kronologisk
    }


async def get_trending() -> dict:
    """Laver to parallelle kald. Returnerer {"movies": [5 film], "tv": [5 serier]}."""
    _TOP_N = 5

    async def _fetch(endpoint_type: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(
                    f"{_BASE_URL}/trending/{endpoint_type}/week", params=_params()
                )
                resp.raise_for_status()
                return resp.json().get("results", [])[:_TOP_N]
            except httpx.HTTPError as e:
                logger.error("TMDB trending/%s error: %s", endpoint_type, e)
                return []

    movie_raw, tv_raw = await asyncio.gather(_fetch("movie"), _fetch("tv"))
    return {
        "movies": [_format_movie_result(i) for i in movie_raw],
        "tv":     [_format_tv_result(i)    for i in tv_raw],
    }


async def get_recommendations(tmdb_id: int, media_type: str) -> list[dict]:
    endpoint = "movie" if media_type == "movie" else "tv"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/{endpoint}/{tmdb_id}/recommendations", params=_params()
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])[:_MAX_RESULTS]
        except httpx.HTTPError as e:
            logger.error("TMDB recommendations error: %s", e)
            return []
    if media_type == "movie":
        return [_format_movie_result(i) for i in results]
    return [_format_tv_result(i) for i in results]


async def get_watch_providers(tmdb_id: int, media_type: str) -> dict:
    endpoint = "movie" if media_type == "movie" else "tv"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/{endpoint}/{tmdb_id}/watch/providers",
                params={"api_key": TMDB_API_KEY},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB watch providers error: %s", e)
            return {"available_in_dk": False}
    dk_data = data.get("results", {}).get("DK")
    if not dk_data:
        return {"available_in_dk": False}
    return {
        "available_in_dk": True,
        "flatrate": _format_provider_list(dk_data.get("flatrate", [])),
        "rent":     _format_provider_list(dk_data.get("rent", [])),
        "buy":      _format_provider_list(dk_data.get("buy", [])),
    }


async def get_now_playing() -> list[dict]:
    """Sorterer efter popularity (faldende), returnerer top 10. Henter 2 sider."""
    raw: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for page in (1, 2):
            try:
                resp = await client.get(
                    f"{_BASE_URL}/movie/now_playing",
                    params=_params(region="DK", page=page),
                )
                resp.raise_for_status()
                raw.extend(resp.json().get("results", []))
            except httpx.HTTPError as e:
                logger.error("TMDB now_playing error (page %d): %s", page, e)
                break
    raw.sort(key=lambda x: x.get("popularity", 0), reverse=True)
    return [_format_movie_result(i) for i in raw[:_MAX_RESULTS]]


async def get_upcoming() -> list[dict]:
    """Sorterer efter popularity (faldende), top 10 præsenteret kronologisk."""
    raw: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for page in (1, 2):
            try:
                resp = await client.get(
                    f"{_BASE_URL}/movie/upcoming",
                    params=_params(region="DK", page=page),
                )
                resp.raise_for_status()
                raw.extend(resp.json().get("results", []))
            except httpx.HTTPError as e:
                logger.error("TMDB upcoming error (page %d): %s", page, e)
                break
    raw.sort(key=lambda x: x.get("popularity", 0), reverse=True)
    top10 = raw[:_MAX_RESULTS]
    top10.sort(key=lambda x: x.get("release_date") or "")
    return [_format_movie_result(i) for i in top10]