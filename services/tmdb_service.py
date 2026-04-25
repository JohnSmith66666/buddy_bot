"""
services/tmdb_service.py - TMDB API integration.

CHANGES vs previous version:
  - KRITISK BUG RETTET: "poster_url" var i _STRIP_FIELDS og blev slettet
    af _strip() — nu er "poster_path" og "credits" i _STRIP_FIELDS i stedet.
  - cast returneres som list[str] med de 3 øverste skuespillere fra credits.
  - poster_url returneres som fuld https://image.tmdb.org/t/p/w500{path} URL.
  - Overview trunkering fjernet — fuld tekst fra TMDB.
  - Engelsk trailer-fallback og append_to_response=videos,credits — uændret.
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
    "credits",                        # fjernet efter cast-ekstraktion
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
      1. type="Trailer" og site="YouTube"  — officiel trailer
      2. type="Teaser"  og site="YouTube"  — teaser hvis ingen trailer

    Returnerer kort youtu.be/{key} URL — dette format er påkrævet for at
    Telegrams inline video-afspiller aktiveres automatisk i chatten.
    Det lange watch?v= format understøtter ikke denne adfærd.

    Returnerer None hvis ingen YouTube-video fundet.
    """
    videos = data.get("videos", {})
    results = videos.get("results", []) if isinstance(videos, dict) else []

    if not results:
        return None

    # Trin 1: søg efter officiel trailer
    for video in results:
        if video.get("site") == "YouTube" and video.get("type") == "Trailer":
            key = video.get("key")
            if key:
                logger.debug("TMDB trailer fundet: type=Trailer key=%s", key)
                return f"https://youtu.be/{key}"

    # Trin 2: fallback til teaser
    for video in results:
        if video.get("site") == "YouTube" and video.get("type") == "Teaser":
            key = video.get("key")
            if key:
                logger.debug("TMDB trailer fallback: type=Teaser key=%s", key)
                return f"https://youtu.be/{key}"

    logger.debug("TMDB: ingen YouTube-trailer fundet i %d videoer", len(results))
    return None


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

    # Udtræk trailer, cast og poster FØR _strip() fjerner felterne
    trailer_url = _extract_trailer_url(data)
    cast        = _extract_cast(data)
    poster_url  = _build_poster_url(data)

    # ── Engelsk fallback hvis ingen dansk trailer ─────────────────────────────
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
                    logger.info(
                        "TMDB trailer (en-US fallback): tmdb_id=%s → %s", tmdb_id, trailer_url
                    )
            except httpx.HTTPError as e:
                logger.warning("TMDB en-US video fallback fejlede (id=%s): %s", tmdb_id, e)

    # ── Engelsk fallback hvis ingen dansk overview ────────────────────────────
    overview = data.get("overview") or ""
    if not overview:
        logger.debug("TMDB: ingen da-DK overview for tmdb_id=%s — prøver en-US", tmdb_id)
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                en_resp = await client.get(
                    f"{_BASE_URL}/{endpoint}/{tmdb_id}",
                    params={"api_key": TMDB_API_KEY, "language": "en-US"},
                )
                en_resp.raise_for_status()
                en_overview = en_resp.json().get("overview") or ""
                if en_overview:
                    logger.info("TMDB overview (en-US fallback): tmdb_id=%s — oversætter...", tmdb_id)
                    try:
                        import anthropic as _anthropic
                        from config import ANTHROPIC_API_KEY as _ANT_KEY
                        _ant = _anthropic.AsyncAnthropic(api_key=_ANT_KEY)
                        msg = await _ant.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=300,
                            messages=[{
                                "role": "user",
                                "content": (
                                    f"Oversæt dette filmresumé til naturligt dansk. "
                                    f"Returnér KUN den oversatte tekst — ingen forklaring, ingen citationstegn:\n\n{en_overview}"
                                ),
                            }],
                        )
                        overview = msg.content[0].text.strip()
                        logger.info("TMDB overview oversat til dansk for tmdb_id=%s", tmdb_id)
                    except Exception as e:
                        logger.warning("Oversættelse fejlede for tmdb_id=%s: %s — bruger engelsk", tmdb_id, e)
                        overview = en_overview
            except httpx.HTTPError as e:
                logger.warning("TMDB en-US overview fallback fejlede (id=%s): %s", tmdb_id, e)
    overview = overview or "Ingen beskrivelse."

    if trailer_url:
        logger.info("TMDB trailer: tmdb_id=%s → %s", tmdb_id, trailer_url)
    else:
        logger.debug("TMDB: ingen trailer (hverken da-DK eller en-US) for tmdb_id=%s", tmdb_id)

    if media_type == "movie":
        return _strip({
            "id":                data.get("id"),
            "title":             data.get("title") or data.get("original_title"),
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

    # TV-serie: hent external_ids separat for tvdb_id (Sonarr kræver det)
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
        "title":                data.get("name") or data.get("original_name"),
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

    Henter ALLE collections der matcher keyword (ikke kun den første) og merger
    deres film. Dette sikrer at f.eks. både danske og norske Olsen-banden-samlinger
    fanges i én søgning.
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

        # Hent detaljer for ALLE matchende collections og merge filmene
        all_movies:       dict[int, dict] = {}  # tmdb_id → movie dict (dedupliker)
        collection_names: list[str]       = []

        for collection in search_results:
            collection_id   = collection.get("id")
            collection_name = collection.get("name") or collection.get("original_name") or keyword

            logger.info(
                "TMDB collection: '%s' → '%s' (id=%s)",
                keyword, collection_name, collection_id,
            )

            try:
                detail_resp = await client.get(
                    f"{_BASE_URL}/collection/{collection_id}", params=_params()
                )
                detail_resp.raise_for_status()
                detail = detail_resp.json()
            except httpx.HTTPError as e:
                logger.warning(
                    "TMDB collection detail fejl (id=%s): %s — springer over",
                    collection_id, e,
                )
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

    movies          = sorted(all_movies.values(), key=lambda x: x["release_date"])
    merged_name     = " + ".join(collection_names) if len(collection_names) > 1 else collection_names[0]
    logger.info(
        "TMDB collection '%s': %d film fundet fra %d samlinger",
        merged_name, len(movies), len(collection_names),
    )

    return {
        "collection_id":   None,  # Flere collections — intet enkelt ID
        "collection_name": merged_name,
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
    Hent den FULDE filmografi for en person via /movie_credits endpoint.

    Bruger /movie_credits (ikke /combined_credits) for at få ALLE film på én gang.
    Filtrering: fjerner 'uncredited'-roller og poster uden release_date.
    Deduplikering på tmdb_id — beholder posten med højest vote_count.
    Sorteret efter popularity faldende.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            bio_resp, movie_credits_resp = await asyncio.gather(
                client.get(f"{_BASE_URL}/person/{person_id}", params=_params()),
                client.get(
                    f"{_BASE_URL}/person/{person_id}/movie_credits",
                    params={"api_key": TMDB_API_KEY, "language": _LANGUAGE},
                ),
            )
            bio_resp.raise_for_status()
            movie_credits_resp.raise_for_status()
            bio        = bio_resp.json()
            movie_data = movie_credits_resp.json()
        except httpx.HTTPError as e:
            logger.error("TMDB filmography error (id=%s): %s", person_id, e)
            return None

    # TV-credits separat
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

    # Filtrer og dedupliker film
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

    movie_credits = sorted(seen.values(), key=lambda x: x.get("popularity", 0), reverse=True)
    movie_credits = [
        {
            "tmdb_id":        m.get("id"),
            "title":          m.get("title") or m.get("original_title"),
            "original_title": m.get("original_title") or m.get("title"),
            "release_date":   m.get("release_date", "Ukendt"),
            "vote_average":   round(m.get("vote_average", 0), 1),
            "vote_count":     m.get("vote_count", 0),
            "character":      m.get("character", ""),
            "popularity":     round(m.get("popularity", 0), 1),
        }
        for m in movie_credits
    ]

    # Top 10 TV-serier
    tv_credits = sorted(tv_credits_raw, key=lambda x: x.get("popularity", 0), reverse=True)[:10]
    tv_credits = [
        {
            "tmdb_id":    t.get("id"),
            "title":      t.get("name") or t.get("original_name"),
            "air_date":   t.get("first_air_date", "Ukendt"),
            "character":  t.get("character", ""),
            "popularity": round(t.get("popularity", 0), 1),
        }
        for t in tv_credits
    ]

    return {
        "person_id":            person_id,
        "name":                 bio.get("name"),
        "biography":            (bio.get("biography") or "")[:300],
        "birthday":             bio.get("birthday"),
        "place_of_birth":       bio.get("place_of_birth"),
        "known_for_department": bio.get("known_for_department", "Ukendt"),
        "total_movie_credits":  len(movie_credits),
        "movie_credits":        movie_credits,
        "tv_credits":           tv_credits,
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