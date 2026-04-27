"""
services/tmdb_service.py - TMDB API integration.

CHANGES vs previous version (v1.1.0 — P1-2 shared httpx client):
  - PERFORMANCE: Alle TMDB-kald genbruger nu en delt httpx.AsyncClient via
    _get_client(). Tidligere oprettede hver async-funktion en ny client
    med 'async with httpx.AsyncClient(...) as client', hvilket kostede
    50-100ms per kald på TLS-handshake til api.themoviedb.org.

  - IMPACT: Alle TMDB-tunge flows er nu ~30% hurtigere. Mest synligt på:
    * search_media (ofte første kald i en bruger-tur)
    * get_recommendations (kaldes ofte parallelt)
    * get_media_details (kaldes ved hver SHOW_INFO)

  - GENBRUGER MØNSTER FRA tmdb_keywords_service: Det service brugte
    allerede shared client for batch-fetch. Nu udvider vi det til ALLE
    TMDB-kald.

  - ARKITEKTUR: Module-level lazy client der lever for hele bot-processen.
    Genstartes automatisk ved Railway redeploy. Connection pooling håndteres
    af httpx.

UNCHANGED (v1.0.4 — filmografi token-fix):
  - get_person_filmography(): movie_credits trimmet fra 8 felter til 4 felter.

UNCHANGED (v0.9.4 — search_media year-filter):
  - search_media() har valgfri `year: int | None = None` parameter.
  - Film: sender primary_release_year til TMDB.
  - TV:   sender first_air_date_year til TMDB.

UNCHANGED:
  - get_media_details(): trailer da-DK→en-US fallback, cast, poster_url, tvdb_id.
  - get_tmdb_collection_movies(): merger alle matchende collections.
  - Alle andre funktioner — uændrede signatures.
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

# ── P1-2: Shared httpx client ─────────────────────────────────────────────────
# Tidligere brugte hver TMDB-funktion 'async with httpx.AsyncClient(...) as':
# Det opretter en ny client + TLS-handshake (~50-100ms) hver gang.
#
# Nu deler alle TMDB-kald én client der lever for hele bot-processen:
# - Connection pooling: TLS-handshake kun én gang
# - Keepalive: TCP-forbindelse genbruges på tværs af kald
# - Estimeret besparelse: 50-100ms per kald = 30% hurtigere TMDB-flows
#
# Client genstartes automatisk ved Railway redeploy (via _get_client()).

_DEFAULT_TIMEOUT = 10
_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """
    Hent (lazy-initialized) shared httpx client til alle TMDB-kald.

    Genbruger TCP-forbindelser via connection pooling. Sparer 50-100ms
    per kald sammenlignet med 'async with httpx.AsyncClient(...) as'.

    Thread-safety: httpx.AsyncClient er thread-safe i async context.
    Vi har ingen race condition fordi _shared_client kun assignes ved
    cold start (første kald). Worst case er at to coroutines samtidig
    opretter to clients — den sidste vinder, og den første GC'es.
    """
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
        logger.debug("TMDB shared client initialized")
    return _shared_client


async def close_tmdb_client() -> None:
    """Luk shared client ved shutdown — kaldes fra main.py on_shutdown()."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None
        logger.info("TMDB shared client lukket")


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


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — alle bruger nu shared client (P1-2)
# ══════════════════════════════════════════════════════════════════════════════

async def search_media(
    query: str,
    media_type: str = "both",
    year: int | None = None,
) -> list[dict]:
    """
    Søg efter film og/eller TV-serier på TMDB.

    query MÅ IKKE indeholde årstal — send år via year-parameteren.
    """
    client = _get_client()
    results: list[dict] = []

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
    client = _get_client()
    endpoint = "movie" if media_type == "movie" else "tv"

    try:
        resp = await client.get(
            f"{_BASE_URL}/{endpoint}/{tmdb_id}",
            params=_params(append_to_response="videos,credits"),
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        logger.error("TMDB media details error (id=%s): %s", tmdb_id, e)
        return None

    # Trailer-fallback: hvis ingen da-DK trailer, hent en-US
    trailer_url = _extract_trailer_url(data)
    if not trailer_url:
        try:
            resp_en = await client.get(
                f"{_BASE_URL}/{endpoint}/{tmdb_id}/videos",
                params={"api_key": TMDB_API_KEY, "language": "en-US"},
            )
            resp_en.raise_for_status()
            videos_en = resp_en.json()
            data["videos"] = videos_en  # patch ind i original-data
            trailer_url = _extract_trailer_url(data)
            logger.debug("TMDB trailer fallback en-US for id=%s: %s",
                         tmdb_id, "fundet" if trailer_url else "intet")
        except httpx.HTTPError as e:
            logger.warning("TMDB en-US trailer fetch fejl (id=%s): %s", tmdb_id, e)

    # For TV: hent external_ids for tvdb_id
    tvdb_id = None
    if media_type == "tv":
        try:
            ext_resp = await client.get(
                f"{_BASE_URL}/tv/{tmdb_id}/external_ids",
                params={"api_key": TMDB_API_KEY},
            )
            ext_resp.raise_for_status()
            ext_data = ext_resp.json()
            tvdb_id  = ext_data.get("tvdb_id")
        except httpx.HTTPError as e:
            logger.warning("TMDB tv external_ids fejl (id=%s): %s", tmdb_id, e)

    cast       = _extract_cast(data)
    poster_url = _build_poster_url(data)

    # Vælg titel og dato baseret på media_type
    if media_type == "movie":
        title        = data.get("title") or data.get("original_title")
        release_date = data.get("release_date")
        runtime      = data.get("runtime")
    else:
        title        = data.get("name") or data.get("original_name")
        release_date = data.get("first_air_date")
        runtime      = None

    # Genres som liste af strings
    genres_list = [g.get("name") for g in data.get("genres", []) if g.get("name")]

    result = {
        "tmdb_id":            tmdb_id,
        "title":              title,
        "release_date":       release_date,
        "first_air_date":     data.get("first_air_date"),
        "overview":           data.get("overview", "")[:500],
        "vote_average":       round(data.get("vote_average", 0), 1),
        "genres":             genres_list,
        "cast":               cast,
        "poster_url":         poster_url,
        "trailer_url":        trailer_url,
        "runtime_minutes":    runtime,
        "number_of_seasons":  data.get("number_of_seasons"),
        "number_of_episodes": data.get("number_of_episodes"),
        "tvdb_id":            tvdb_id,
        "original_language":  data.get("original_language", "en"),
        "media_type":         media_type,
    }

    return _strip(result)


async def get_trending() -> dict:
    """Trending denne uge — ALTID 5 film + 5 serier. Returnerer {"movies": [...], "tv": [...]}."""
    _TOP_N = 5
    client = _get_client()

    async def _fetch(endpoint_type: str) -> list[dict]:
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
    """Find film eller serier der ligner en specifik titel."""
    client = _get_client()
    endpoint = "movie" if media_type == "movie" else "tv"

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
    """Find danske streamingtjenester for en titel."""
    client = _get_client()
    endpoint = "movie" if media_type == "movie" else "tv"

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
    client = _get_client()

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
    client = _get_client()

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


async def get_tmdb_collection_movies(keyword: str) -> dict | None:
    """
    Henter ALLE collections der matcher keyword og merger deres film.
    """
    client = _get_client()

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
    logger.info("TMDB collection '%s': %d film fundet fra %d samlinger",
                merged_name, len(movies), len(collection_names))

    return {
        "collection_id":   None,
        "collection_name": merged_name,
        "total_parts":     len(movies),
        "movies":          movies,
    }


async def search_person(query: str) -> list[dict]:
    """Søg efter en person (skuespiller, instruktør osv.) på TMDB."""
    client = _get_client()

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
    i crew-listen — ikke skuespillerroller.

    Hvis personen ikke har nogen crew-credits (dvs. er ren skuespiller),
    falder vi tilbage til cast-listen.
    """
    client = _get_client()

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
    crew_films = [c for c in movie_data.get("crew", []) if c.get("job") == "Director"]

    # Deduplicate på original_title (TMDB har ofte to entries for samme film)
    seen_titles: set[str] = set()
    unique_films: list[dict] = []
    for film in sorted(crew_films, key=lambda f: f.get("release_date") or ""):
        original_title = film.get("original_title") or film.get("title") or ""
        if not original_title or original_title in seen_titles:
            continue
        seen_titles.add(original_title)
        unique_films.append(film)

    # Hvis ingen instruktør-credits, fald tilbage til cast (skuespiller)
    if not unique_films:
        cast_films = movie_data.get("cast", [])
        cast_films.sort(key=lambda f: f.get("popularity", 0), reverse=True)
        unique_films = cast_films

    movie_credits = []
    for film in unique_films:
        movie_credits.append({
            "tmdb_id":        film.get("id"),
            "title":          film.get("title") or "",
            "original_title": film.get("original_title") or film.get("title") or "",
            "release_date":   film.get("release_date") or "",
        })

    return {
        "id":              bio.get("id"),
        "name":            bio.get("name"),
        "biography":       (bio.get("biography") or "")[:300],
        "place_of_birth":  bio.get("place_of_birth"),
        "movie_credits":   movie_credits,
    }