"""
services/tmdb_service.py - TMDB API integration.

CHANGES vs previous version (v1.0.4 — filmografi token-fix):
  - get_person_filmography(): movie_credits trimmet fra 8 felter til 4 felter
    per film (fjerner vote_average, vote_count, character, popularity).
    Årsag: 91 film × 150 chars = 13.700 chars → _trim_tool_result kappede til
    10 items → Jackie Brown, Kill Bill, Inglourious Basterds m.fl. nåede
    aldrig frem til Buddy → Buddy gættede forkerte ID'er fra træningsdata.
    Fix: 91 film × 65 chars = 5.915 chars → alle passer inden for 6000-grænsen.
    Buddy bruger ikke vote_average/count/character til Plex-tjek.

UNCHANGED (v0.9.4 — search_media year-filter):
  - search_media() har nu en valgfri `year: int | None = None` parameter.
  - Film: sender primary_release_year til TMDB når year er angivet.
  - TV:   sender first_air_date_year til TMDB når year er angivet.

UNCHANGED:
  - get_media_details(): trailer da-DK→en-US fallback, cast, poster_url, tvdb_id.
  - get_tmdb_collection_movies(): merger alle matchende collections.
  - Alle andre funktioner — uændrede.
"""

import asyncio
import logging

import httpx

from config import TMDB_API_KEY

logger = logging.getLogger(__name__)

_BASE_URL    = "https://api.themoviedb.org/3"
_LANGUAGE    = "da-DK"
_MAX_RESULTS = 10
_POSTER_BASE = "https://image.tmdb.org/t/p/w500"


def _extract_cast(data: dict, max_cast: int = 3) -> list[str]:
    """Returner de øverste max_cast skuespillernavne fra credits.cast."""
    cast = data.get("credits", {}).get("cast", [])
    return [c["name"] for c in cast[:max_cast] if c.get("name")]


def _build_poster_url(data: dict) -> str | None:
    """Byg fuld TMDB poster URL fra poster_path, eller None hvis mangler."""
    path = data.get("poster_path")
    return f"{_POSTER_BASE}{path}" if path else None


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
    "backdrop_path", "poster_path", "backdrop_url",
    "profile_url", "profile_path", "still_path",
    "production_companies", "production_countries",
    "spoken_languages", "belongs_to_collection",
    "homepage", "adult", "video",
    "credits",
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


def _extract_trailer_url(data: dict) -> str | None:
    """
    Find den bedste YouTube-trailer fra TMDB's videos-respons.

    Prioritetsorden:
      1. type="Trailer" og site="YouTube"
      2. type="Teaser"  og site="YouTube"

    Returnerer kort youtu.be/{key} URL.
    """
    videos  = data.get("videos", {})
    results = videos.get("results", []) if isinstance(videos, dict) else []

    if not results:
        return None

    for video in results:
        if video.get("site") == "YouTube" and video.get("type") == "Trailer":
            key = video.get("key")
            if key:
                logger.debug("TMDB trailer fundet: type=Trailer key=%s", key)
                return f"https://youtu.be/{key}"

    for video in results:
        if video.get("site") == "YouTube" and video.get("type") == "Teaser":
            key = video.get("key")
            if key:
                logger.debug("TMDB trailer fallback: type=Teaser key=%s", key)
                return f"https://youtu.be/{key}"

    logger.debug("TMDB: ingen YouTube-trailer fundet i %d videoer", len(results))
    return None


async def search_media(
    query: str,
    media_type: str = "both",
    year: int | None = None,
) -> list[dict]:
    """
    Søg efter film og/eller TV-serier på TMDB.

    query MÅ IKKE indeholde årstal — send år via year-parameteren.
    """
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as client:
        if media_type in ("movie", "both"):
            try:
                movie_params = _params(query=query)
                if year:
                    movie_params["primary_release_year"] = year
                    logger.debug("TMDB movie search: query=%r primary_release_year=%s", query, year)
                resp = await client.get(f"{_BASE_URL}/search/movie", params=movie_params)
                resp.raise_for_status()
                for item in resp.json().get("results", [])[:_MAX_RESULTS]:
                    results.append(_format_movie_result(item))
            except httpx.HTTPError as e:
                logger.error("TMDB movie search error: %s", e)

        if media_type in ("tv", "both"):
            try:
                tv_params = _params(query=query)
                if year:
                    tv_params["first_air_date_year"] = year
                    logger.debug("TMDB tv search: query=%r first_air_date_year=%s", query, year)
                resp = await client.get(f"{_BASE_URL}/search/tv", params=tv_params)
                resp.raise_for_status()
                for item in resp.json().get("results", [])[:_MAX_RESULTS]:
                    results.append(_format_tv_result(item))
            except httpx.HTTPError as e:
                logger.error("TMDB TV search error: %s", e)

    return results


async def get_media_details(tmdb_id: int, media_type: str) -> dict | None:
    """
    Fetch full details for a film or TV show.

    append_to_response=videos,credits henter trailer + cast i ét kald.
    Returnerer cast (top 3 navne) og poster_url til Netflix-look visningen.

    Trailer-strategi (to-trins fallback):
      1. da-DK: traileren hentes i det primære API-kald.
      2. en-US: fallback hvis ingen dansk trailer findes.

    For TV-serier hentes external_ids separat for tvdb_id (Sonarr kræver det).
    """
    endpoint = "movie" if media_type == "movie" else "tv"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/{endpoint}/{tmdb_id}",
                params=_params(append_to_response="videos,credits"),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB details error (id=%s type=%s): %s", tmdb_id, media_type, e)
            return None

    trailer_url = _extract_trailer_url(data)
    cast        = _extract_cast(data)
    poster_url  = _build_poster_url(data)

    # ── Engelsk trailer-fallback ──────────────────────────────────────────────
    if not trailer_url:
        logger.debug("TMDB: ingen da-DK trailer for tmdb_id=%s — prøver en-US", tmdb_id)
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                en_resp = await client.get(
                    f"{_BASE_URL}/{endpoint}/{tmdb_id}/videos",
                    params={"api_key": TMDB_API_KEY, "language": "en-US"},
                )
                en_resp.raise_for_status()
                trailer_url = _extract_trailer_url({"videos": en_resp.json()})
                if trailer_url:
                    logger.info("TMDB trailer (en-US fallback): tmdb_id=%s → %s", tmdb_id, trailer_url)
            except httpx.HTTPError as e:
                logger.warning("TMDB en-US video fallback fejlede (id=%s): %s", tmdb_id, e)

    # ── Engelsk overview-fallback ─────────────────────────────────────────────
    overview = data.get("overview") or ""
    en_title = None
    if not overview:
        logger.debug("TMDB: ingen da-DK overview for tmdb_id=%s — prøver en-US", tmdb_id)
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                en_resp = await client.get(
                    f"{_BASE_URL}/{endpoint}/{tmdb_id}",
                    params={"api_key": TMDB_API_KEY, "language": "en-US"},
                )
                en_resp.raise_for_status()
                en_data  = en_resp.json()
                overview = en_data.get("overview") or ""
                en_title = (
                    en_data.get("title") or en_data.get("name") or
                    en_data.get("original_title") or en_data.get("original_name")
                )
            except httpx.HTTPError as e:
                logger.warning("TMDB en-US overview fallback fejlede (id=%s): %s", tmdb_id, e)

    # ── Titel-fallback for ikke-latinske sprog ────────────────────────────────
    _LATIN_LANGUAGES = {
        "en", "da", "de", "fr", "es", "it", "nl", "sv", "no", "fi",
        "pl", "pt", "ro", "cs", "hu",
    }
    original_language = data.get("original_language") or "en"
    da_title = (
        data.get("title") or data.get("name") or
        data.get("original_title") or data.get("original_name")
    )
    if original_language not in _LATIN_LANGUAGES:
        if not en_title:
            async with httpx.AsyncClient(timeout=5) as client:
                try:
                    en_resp = await client.get(
                        f"{_BASE_URL}/{endpoint}/{tmdb_id}",
                        params={"api_key": TMDB_API_KEY, "language": "en-US"},
                    )
                    en_resp.raise_for_status()
                    en_data  = en_resp.json()
                    en_title = (
                        en_data.get("title") or en_data.get("name") or
                        en_data.get("original_title") or en_data.get("original_name")
                    )
                except Exception:
                    pass
        if en_title and en_title != da_title:
            logger.info(
                "TMDB titel-fallback: '%s' → '%s' (sprog: %s)",
                da_title, en_title, original_language,
            )
            da_title = en_title

    if trailer_url:
        logger.info("TMDB trailer: tmdb_id=%s → %s", tmdb_id, trailer_url)
    else:
        logger.debug("TMDB: ingen trailer (hverken da-DK eller en-US) for tmdb_id=%s", tmdb_id)

    if media_type == "movie":
        return _strip({
            "id":                data.get("id"),
            "title":             da_title,
            "tagline":           data.get("tagline"),
            "overview":          overview,
            "release_date":      data.get("release_date", "Ukendt"),
            "runtime_minutes":   data.get("runtime"),
            "vote_average":      round(data.get("vote_average", 0), 1),
            "genres":            [g["name"] for g in data.get("genres", [])],
            "genre_ids":         [g["id"] for g in data.get("genres", [])],
            "original_language": data.get("original_language"),
            "imdb_id":           data.get("imdb_id"),
            "trailer_url":       trailer_url,
            "cast":              cast,
            "poster_url":        poster_url,
            "media_type":        "movie",
        })

    # TV-serie: hent external_ids separat for tvdb_id
    tvdb_id = None
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            ext_resp = await client.get(
                f"{_BASE_URL}/tv/{tmdb_id}/external_ids",
                params={"api_key": TMDB_API_KEY},
            )
            ext_resp.raise_for_status()
            tvdb_id = ext_resp.json().get("tvdb_id")
            logger.info("TMDB external_ids for tmdb_id=%s: tvdb_id=%s", tmdb_id, tvdb_id)
        except httpx.HTTPError as e:
            logger.warning("Could not fetch external_ids for tmdb_id=%s: %s", tmdb_id, e)

    return _strip({
        "id":                   data.get("id"),
        "title":                da_title,
        "tagline":              data.get("tagline"),
        "overview":             overview,
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
        "trailer_url":          trailer_url,
        "cast":                 cast,
        "poster_url":           poster_url,
        "media_type":           "tv",
    })


async def get_tmdb_collection_movies(keyword: str) -> dict | None:
    """
    Søg efter TMDB-samlinger og returner alle film med tmdb_id, title, original_title.

    Henter ALLE collections der matcher keyword og merger deres film.
    """
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

        all_movies:       dict[int, dict] = {}
        collection_names: list[str]       = []

        for collection in search_results:
            collection_id   = collection.get("id")
            collection_name = collection.get("name") or collection.get("original_name") or keyword

            logger.info("TMDB collection: '%s' → '%s' (id=%s)", keyword, collection_name, collection_id)

            try:
                detail_resp = await client.get(
                    f"{_BASE_URL}/collection/{collection_id}", params=_params()
                )
                detail_resp.raise_for_status()
                detail = detail_resp.json()
            except httpx.HTTPError as e:
                logger.warning("TMDB collection detail fejl (id=%s): %s — springer over", collection_id, e)
                continue

            collection_names.append(collection_name)

            for part in detail.get("parts", []):
                tmdb_id        = part.get("id")
                title          = part.get("title") or part.get("original_title") or ""
                original_title = part.get("original_title") or title
                if not title or not tmdb_id:
                    continue
                if tmdb_id not in all_movies:
                    all_movies[tmdb_id] = {
                        "tmdb_id":        tmdb_id,
                        "title":          title,
                        "original_title": original_title,
                        "release_date":   part.get("release_date") or "Ukendt",
                    }

    if not all_movies:
        return None

    movies      = sorted(all_movies.values(), key=lambda x: x["release_date"])
    merged_name = " + ".join(collection_names) if len(collection_names) > 1 else collection_names[0]
    logger.info("TMDB collection '%s': %d film fundet fra %d samlinger", merged_name, len(movies), len(collection_names))

    return {
        "collection_id":   None,
        "collection_name": merged_name,
        "total_parts":     len(movies),
        "movies":          movies,
    }


async def search_person(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{_BASE_URL}/search/person", params=_params(query=query))
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
                known_for.append(f"{title} ({year}) [{media}]" if year else f"{title} [{media}]")
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
    Hent filmografi for en person via /movie_credits endpoint.

    Returnerer KUN film hvor personen er registreret som INSTRUKTØR (job="Director")
    i crew-listen — ikke skuespillerroller. Dette reducerer Tarantinos 91 cast-film
    til ~10 instruktørfilm, som passer komfortabelt inden for 6.000-chars-grænsen.

    Hvis personen ikke har nogen crew-credits (dvs. er ren skuespiller),
    falder vi tilbage til cast-listen for at stadig returnere noget brugbart.

    Sorteret efter release_date (ældste først) — kronologisk rækkefølge er
    mere intuitiv for instruktør-filmografier end popularity.

    Token-optimering: kun 3 felter per film (tmdb_id, title, original_title,
    release_date). biography, tv_credits og skuespillerroller fjernet.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            bio_resp, credits_resp = await asyncio.gather(
                client.get(f"{_BASE_URL}/person/{person_id}", params=_params()),
                client.get(
                    f"{_BASE_URL}/person/{person_id}/movie_credits",
                    params={"api_key": TMDB_API_KEY, "language": _LANGUAGE},
                ),
            )
            bio_resp.raise_for_status()
            credits_resp.raise_for_status()
            bio        = bio_resp.json()
            movie_data = credits_resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB filmography error (id=%s): %s", person_id, e)
            return None

    # ── Hent kun instruktørfilm fra crew-listen ───────────────────────────────
    # Deduplicerer på original_title (ikke id) — TMDB har ofte to entries for
    # samme film: én tidlig festival-version og den rigtige release.
    # Crew-entries har IKKE vote_count, så vi bruger seneste release_date
    # som proxy: festival-screenings har altid en tidligere dato end den
    # officielle release.
    # Eksempel Reservoir Dogs: id=443129 (1991-06-01 festival) vs id=500 (1992-09-02 release)
    # Eksempel Inglourious Basterds: id=16869 (tidlig) vs id=44008 (2009-08-21 release)
    crew_raw = movie_data.get("crew", [])
    directed: dict[str, dict] = {}   # original_title.lower() → item med seneste release_date
    for item in crew_raw:
        if item.get("job") != "Director":
            continue
        if not item.get("release_date"):
            continue
        if item.get("id") is None:
            continue
        key = (item.get("original_title") or item.get("title") or "").lower().strip()
        if not key:
            continue
        existing = directed.get(key)
        # Behold entry med senest release_date — festival-versioner kommer altid før
        if existing is None or item.get("release_date", "") > existing.get("release_date", ""):
            directed[key] = item

    # Fallback til cast-liste for rene skuespillere (known_for_department != "Directing")
    # OG hvis ingen director-credits overhovedet.
    # Tom Hanks har 3 instruktørfilm — men er skuespiller — skal have cast-kreditter.
    is_primarily_director = bio.get("known_for_department", "") == "Directing"

    if not directed or not is_primarily_director:
        if directed and not is_primarily_director:
            logger.info(
                "get_person_filmography: person_id=%s '%s' er skuespiller (known_for=%s) — bruger cast i stedet for Director-credits",
                person_id, bio.get("name"), bio.get("known_for_department"),
            )
        elif not directed:
            logger.info(
                "get_person_filmography: ingen Director-credits for person_id=%s — falder tilbage til cast",
                person_id,
            )
        cast_raw = movie_data.get("cast", [])
        seen: dict[int, dict] = {}
        for item in cast_raw:
            if not item.get("release_date"):
                continue
            if "uncredited" in (item.get("character") or "").lower():
                continue
            mid = item.get("id")
            if mid is None:
                continue
            if mid not in seen or item.get("vote_count", 0) > seen[mid].get("vote_count", 0):
                seen[mid] = item
        directed = seen

    # Sortér kronologisk (ældste film først)
    movie_credits = sorted(directed.values(), key=lambda x: x.get("release_date", ""))
    movie_credits = [
        {
            "tmdb_id":        m.get("id"),
            "title":          m.get("title") or m.get("original_title"),
            "original_title": m.get("original_title") or m.get("title"),
            "release_date":   m.get("release_date", "Ukendt"),
        }
        for m in movie_credits
    ]

    logger.info(
        "get_person_filmography: person_id=%s '%s' → %d film (Director-credits)",
        person_id, bio.get("name"), len(movie_credits),
    )

    return {
        "person_id":            person_id,
        "name":                 bio.get("name"),
        "known_for_department": bio.get("known_for_department", "Ukendt"),
        "total_movie_credits":  len(movie_credits),
        "movie_credits":        movie_credits,
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
    """Sorterer efter popularity (faldende), returnerer top 10."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/movie/now_playing",
                params=_params(region="DK"),
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except httpx.HTTPError as e:
            logger.error("TMDB now_playing error: %s", e)
            return []
    results.sort(key=lambda x: x.get("popularity", 0), reverse=True)
    return [_format_movie_result(i) for i in results[:_MAX_RESULTS]]


async def get_upcoming() -> list[dict]:
    """Sorterer efter popularity (faldende), returnerer top 10."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{_BASE_URL}/movie/upcoming",
                params=_params(region="DK"),
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except httpx.HTTPError as e:
            logger.error("TMDB upcoming error: %s", e)
            return []
    results.sort(key=lambda x: x.get("popularity", 0), reverse=True)
    return [_format_movie_result(i) for i in results[:_MAX_RESULTS]]