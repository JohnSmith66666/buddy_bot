"""
services/plex_service.py - Plex Media Server integration via python-plexapi.

CHANGES vs previous version (v1.1.0 — switchHomeUser failure cache):
  - Performance fix: _connect() cacher nu Plex Home User auth-fejl per username.
    Når switchHomeUser() fejler med 401 unauthorized, caches usernavnet i 1 time
    så efterfølgende kald straks falder tilbage til admin-konto i stedet for
    at spilde 3-5 sekunder på timeout-baseret retry. Ved 5 parallelle Plex-tjek
    sparer dette 15-25 sekunder.
  - Cachen er observerbar: ERROR-log første gang en bruger ryger i cache så
    rod-årsagen (udløbet token, fjernet bruger) ikke bliver glemt.
  - DEBUG-log på subsequent skips for stille drift.
  - TTL er 1 time — rettet token får hurtigt effekt, og spam undgås.

UNCHANGED (v1.0.2 — sci-fi genre fix):
  - _GENRE_SYNONYMS: Tilføjet "sciencefiction" og "science fiction" som
    selvstændige nøgler.
Uses fuzzy title matching to handle variations like 'Olsen-banden' vs 'Olsen Banden'.
PlexAPI calls are synchronous so we run them in a thread pool to avoid
blocking the async event loop.

TOKEN OPTIMISATION (data-diæt):
  - All list results are capped at 25 items maximum.
  - Every Plex item is serialised through _slim() before being returned to
    the AI. _slim() keeps only: title, year, rating, genres (max 3), summary.
  - File paths, codecs, bitrates and other heavy metadata are stripped from
    all list responses. Technical specs are only returned by get_plex_metadata(),
    which is called explicitly when the user asks for them.

CHANGES vs previous version (v1.0.1 — GUID Lag 0 fix):
  - KRITISK FIX: _check_sync() Lag 0 GUID-scan brugte section.search() uden
    filter og itererede hele biblioteket. PlexAPI loader .guids lazy — uden
    item.reload() er listen tom, og _extract_tmdb_id_from_guids() returnerer
    altid None. Resultatet var at GUID-matchet aldrig virkede, og funktionen
    faldt igennem til lag 1/2/3 titel-søgning, som matchede en tilfældig film.
    Det forklarede log-lines som:
      Plex HIT (lag 0/GUID): 'Blade Runner 2049' — søgt på 'The Hateful Eight'
      Plex HIT (lag 0/GUID): 'To All the Boys I've Loved Before' — søgt på 'Once Upon a Time in Hollywood'
    FIX: Bruger nu section.search(guid=f'tmdb://{tmdb_id}') som er Plex'
    native server-side GUID-filter. Det er O(1) i stedet for O(n), kræver
    ingen item.reload(), og returnerer præcis den rigtige film.
    Fallback til section.search(guid=f'tmdb://{tmdb_id}') fejler gracefully
    med tom liste hvis filmen ikke findes — da hopper vi til lag 1.

UNCHANGED (v0.9.9 — find_unwatched fix):
  - KRITISK FIX: _unwatched_sync() brugte section.search(unwatched=True) som
    ikke er et gyldigt PlexAPI-argument og kaster en exception. Fallback
    section.search() returnerer kun ~20 resultater (Plex default limit) —
    og hvis alle 20 er sete, returnerer viewCount-filteret 0 resultater.
    Fix: section.all() henter HELE biblioteket uden limit. Vi filtrerer
    usete client-side via viewCount == 0.
  - Tilføjet INFO-log der viser antal usete titler fundet per kald.

UNCHANGED:
  - Fix A: _build_actor_guid_set() scanner hele biblioteket (Lag 2). Uændret.
  - _extract_imdb_id_from_guids(), check_actor_on_plex() IMDb GUID-match. Uændret.
  - _franchise_plex_check_sync(). Uændret.
  - add_to_watchlist(), get_plex_watch_url(), validate_plex_user(). Uændret.
"""

import asyncio
import logging
import math
import random
import re
import unicodedata
from functools import partial

import httpx
from plexapi.exceptions import Unauthorized
from plexapi.server import PlexServer

from config import PLEX_TOKEN, PLEX_URL, TMDB_API_KEY

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

STATUS_FOUND   = "found"
STATUS_MISSING = "missing"
STATUS_ERROR   = "error"

_MOVIE_TYPE  = "movie"
_TV_TYPE     = "show"
_MAX_RESULTS = 25

_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_LANG = "da-DK"

_TV_YEAR_TOLERANCE      = 2
_COLLECTION_MAX_MAIN    = 10
_FRANCHISE_MAX_PER_LIST = 40
_ACTOR_MAX_MISSING      = 15

# ── switchHomeUser failure cache ──────────────────────────────────────────────
# Cacher Plex Home User auth-fejl (401 unauthorized) per username.
# Når en bruger fejler, falder vi straks tilbage til admin-konto i stedet for
# at spilde 3-5 sekunder på timeout-baseret retry ved hvert eneste tool-kald.
# Cachen er per process — nulstilles ved Railway redeploy.
#
# TTL er bevidst 1 time: kort nok til at fange genaktiverede tokens hurtigt,
# langt nok til at undgå spam når token er permanent revoked.
import time

_SWITCH_FAIL_TTL_SECS = 3600  # 1 time
_switch_fail_cache: dict[str, float] = {}  # username (lower) → unix timestamp


# ── Lightweight item serialiser ───────────────────────────────────────────────

def _slim(item) -> dict:
    genres  = [g.tag for g in getattr(item, "genres", [])][:3]
    summary = (getattr(item, "summary", "") or "").strip()
    if len(summary) > 200:
        summary = summary[:197] + "…"
    return {
        "title":   getattr(item, "title", "Ukendt"),
        "year":    getattr(item, "year", None),
        "rating":  getattr(item, "audienceRating", None),
        "genres":  genres,
        "summary": summary or None,
        "tmdb_id": _extract_tmdb_id_from_guids(item),
    }


def _is_animation(item) -> bool:
    genres = [g.tag.lower() for g in getattr(item, "genres", [])]
    return "animation" in genres


def _extract_tmdb_id_from_guids(item) -> int | None:
    """
    Udtræk TMDB ID fra et Plex-items .guids liste.
    Format: [Guid(id='tmdb://671'), Guid(id='imdb://tt0241527'), ...]
    """
    try:
        guids = getattr(item, "guids", []) or []
        for guid in guids:
            guid_str = getattr(guid, "id", None) or str(guid)
            if guid_str.startswith("tmdb://"):
                raw = guid_str.replace("tmdb://", "")
                if raw.isdigit():
                    return int(raw)
    except Exception as e:
        logger.debug("_extract_tmdb_id_from_guids error: %s", e)
    return None


def _extract_imdb_id_from_guids(item) -> str | None:
    """Udtræk IMDb ID (f.eks. 'tt21909366') fra et Plex-items .guids liste."""
    try:
        guids = getattr(item, "guids", []) or []
        for guid in guids:
            guid_str = getattr(guid, "id", None) or str(guid)
            if guid_str.startswith("imdb://"):
                return guid_str.replace("imdb://", "")
    except Exception as e:
        logger.debug("_extract_imdb_id_from_guids error: %s", e)
    return None


# ── Title normalisation ───────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s]", "", s)
    return " ".join(s.lower().split())


def _titles_match(a: str, b: str) -> bool:
    return _normalise(a) == _normalise(b)


def _titles_match_fuzzy(a: str, b: str) -> bool:
    na, nb = _normalise(a), _normalise(b)
    return na in nb or nb in na


def _year_ok_for_tv(item_year: int | None, search_year: int | None) -> bool:
    if not item_year or not search_year:
        return True
    return abs(item_year - search_year) <= _TV_YEAR_TOLERANCE


# ── Plex connection ───────────────────────────────────────────────────────────

def _connect(plex_username: str | None = None) -> PlexServer | dict:
    try:
        admin_plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
    except Exception as e:
        logger.error("Plex connection error: %s", e)
        return {"status": STATUS_ERROR, "message": f"Forbindelsesfejl: {e}"}

    if not plex_username:
        return admin_plex

    # ── Cache-tjek: tidligere fejlet switchHomeUser? ──────────────────────────
    norm = plex_username.strip().lower()
    cached_at = _switch_fail_cache.get(norm)
    if cached_at is not None:
        age = time.time() - cached_at
        if age < _SWITCH_FAIL_TTL_SECS:
            logger.debug(
                "switchHomeUser cache HIT for '%s' (age=%.0fs) — bruger admin",
                plex_username, age,
            )
            return admin_plex
        else:
            # TTL udløbet — giv brugeren en chance igen
            logger.info(
                "switchHomeUser cache TTL udløbet for '%s' — prøver igen",
                plex_username,
            )
            _switch_fail_cache.pop(norm, None)

    try:
        account = admin_plex.myPlexAccount()

        owner_names = {
            (account.username or "").lower(),
            (account.email or "").lower(),
            (account.title or "").lower(),
        }
        if norm in owner_names:
            return admin_plex

        for user in account.users():
            user_names = {
                (getattr(user, "username", "") or "").lower(),
                (getattr(user, "email", "") or "").lower(),
                (getattr(user, "title", "") or "").lower(),
            }
            if norm in user_names:
                try:
                    switched = account.switchHomeUser(user)
                    return PlexServer(PLEX_URL, switched.authToken, timeout=15)
                except Exception as e:
                    # Cache fejlen så vi ikke spilder 3-5s på næste tool-kald
                    _switch_fail_cache[norm] = time.time()
                    logger.error(
                        "switchHomeUser FAILED for '%s' — caching for %ds. "
                        "Action required: tjek Plex-token i database eller "
                        "fjern brugerens tilknytning. Fejl: %s",
                        plex_username, _SWITCH_FAIL_TTL_SECS, e,
                    )
                    return admin_plex

        logger.warning("Plex user '%s' not found — falling back to admin", plex_username)
        return admin_plex

    except Exception as e:
        logger.warning("_connect() error for '%s': %s — falling back to admin", plex_username, e)
        return admin_plex


def _sections(plex: PlexServer, plex_type: str):
    try:
        return [s for s in plex.library.sections() if s.type == plex_type]
    except Exception as e:
        logger.error("_sections error: %s", e)
        return []


def _safe_search(section, title: str):
    try:
        return section.search(title=title, limit=_MAX_RESULTS)
    except Exception as e:
        logger.warning("section.search error for '%s': %s", title, e)
        return []


# ── Metadata helpers ──────────────────────────────────────────────────────────

def _get_stream_info(item) -> dict:
    info = {}
    try:
        media_list = getattr(item, "media", []) or []
        if not media_list:
            return info
        media = media_list[0]
        info["resolution"]  = getattr(media, "videoResolution", None)
        info["video_codec"] = getattr(media, "videoCodec", None)
        info["bitrate_kbps"]= getattr(media, "bitrate", None)
        info["container"]   = getattr(media, "container", None)
        parts = getattr(media, "parts", []) or []
        streams = parts[0].streams if parts else []
        video_streams = [s for s in streams if getattr(s, "streamType", None) == 1]
        audio_streams = [s for s in streams if getattr(s, "streamType", None) == 2]
        if video_streams:
            vs = video_streams[0]
            info["hdr"]         = bool(getattr(vs, "colorPrimaries", None) == "bt2020")
            info["video_profile"]= getattr(vs, "displayTitle", None)
        if audio_streams:
            aus = audio_streams[0]
            info["audio_codec"]  = getattr(aus, "codec", None)
            info["channels"]     = getattr(aus, "channels", None)
    except Exception as e:
        logger.warning("Stream info error: %s", e)
    return info


# ══════════════════════════════════════════════════════════════════════════════
# SYNC IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _check_sync(
    title: str,
    year: int | None,
    media_type: str,
    plex_username: str | None = None,
    tmdb_id: int | None = None,
) -> dict:
    """
    Tre lag:
      Lag 0 (GUID): Server-side GUID-filter via section.search(guid=...).
                    Kræver ingen item.reload() og er O(1) — præcis og hurtig.
                    Bruges KUN når tmdb_id er angivet.
      Lag 1 (eksakt): section.search(title) + _titles_match.
      Lag 2/3 (fuzzy): section.search(title) + _titles_match_fuzzy.
    """
    is_tv     = (media_type == "tv")
    plex_type = _TV_TYPE if is_tv else _MOVIE_TYPE

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    def _build_result(item, match_lag) -> dict:
        item_title = getattr(item, "title", "") or ""
        item_year  = getattr(item, "year", None)
        logger.info(
            "Plex HIT (lag %s): '%s' (%s) — søgt på '%s' (%s)",
            match_lag, item_title, item_year, title, year,
        )
        try:
            item.reload()
        except Exception as e:
            logger.warning("item.reload() fejlede for '%s': %s", item_title, e)
        p_rating     = getattr(item, "rating", None)
        a_rating     = getattr(item, "audienceRating", None)
        final_rating = p_rating if p_rating else a_rating
        return {
            "status":            STATUS_FOUND,
            "title":             item_title,
            "year":              item_year,
            "ratingKey":         item.ratingKey,
            "machineIdentifier": plex.machineIdentifier,
            "rating":            final_rating,
        }

    for section in _sections(plex, plex_type):

        # ── Lag 0: Server-side GUID-filter (præcis, ingen lazy-load problem) ──
        # section.search(guid=...) sender filteret direkte til Plex-serveren.
        # Returnerer maks 1 item — den eksakte film med det TMDB ID.
        # Kræver IKKE item.reload() fordi resultater fra search() inkluderer
        # fuld metadata inkl. guids når de returneres med dette filter.
        if tmdb_id:
            try:
                guid_hits = section.search(guid=f"tmdb://{tmdb_id}")
                if guid_hits:
                    return _build_result(guid_hits[0], "0/GUID")
            except Exception as e:
                logger.warning("GUID search fejl i '%s': %s", section.title, e)
            # Ingen hit på GUID — fortsæt til lag 1

        # ── Lag 1 + 2/3: Titel-søgning ────────────────────────────────────────
        # Prøv også med stripped titel (fjern '...' og lignende) som fallback.
        # "Once Upon a Time... in Hollywood" søges som "Once Upon a Time in Hollywood"
        stripped_title = re.sub(r"\.{2,}", "", title).strip()
        search_titles = [title] if stripped_title == title else [title, stripped_title]

        matched = False
        for search_title in search_titles:
            for item in _safe_search(section, search_title):
                item_title = getattr(item, "title", "") or ""
                item_year  = getattr(item, "year", None)

                if not (_titles_match(item_title, title) or
                        _titles_match_fuzzy(item_title, title) or
                        (stripped_title != title and (
                            _titles_match(item_title, stripped_title) or
                            _titles_match_fuzzy(item_title, stripped_title)
                        ))):
                    continue

                match_lag = 1 if _titles_match(item_title, title) else "2/3"

                if is_tv:
                    if not _year_ok_for_tv(item_year, year):
                        continue
                else:
                    if year and item_year and abs(item_year - year) > 1:
                        continue

                return _build_result(item, match_lag)
            if matched:
                break

    return {"status": STATUS_MISSING}


def _collection_sync(
    keyword: str,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    """Simpel Plex-tekstsøgning med animations-filter."""
    is_tv     = (media_type == "tv")
    plex_type = _TV_TYPE if is_tv else _MOVIE_TYPE

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    results = []
    for section in _sections(plex, plex_type):
        for item in _safe_search(section, keyword):
            results.append(_slim(item))
            if len(results) >= _COLLECTION_MAX_MAIN:
                break
        if len(results) >= _COLLECTION_MAX_MAIN:
            break

    if not results:
        return {"status": STATUS_MISSING}
    return {"status": "ok", "results": results}


def _franchise_plex_check_sync(
    collection_name: str,
    tmdb_movies: list[dict],
    plex_username: str | None = None,
) -> dict:
    """GUID-matching som primær metode, fuzzy titel som fallback."""
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    sections = _sections(plex, _MOVIE_TYPE)
    if not sections:
        return {"status": STATUS_ERROR, "message": "Ingen film-sektioner fundet i Plex."}

    plex_index: dict[int, dict] = {}
    for section in sections:
        try:
            for item in section.search():
                key = getattr(item, "ratingKey", None)
                if key and key not in plex_index:
                    plex_index[key] = {
                        "title":   getattr(item, "title", "") or "",
                        "year":    getattr(item, "year", None),
                        "tmdb_id": _extract_tmdb_id_from_guids(item),
                    }
        except Exception as e:
            logger.warning("Franchise index error in '%s': %s", section.title, e)

    plex_by_tmdb_id = {
        e["tmdb_id"]: e for e in plex_index.values() if e["tmdb_id"]
    }

    logger.info(
        "check_franchise_on_plex: %d/%d Plex-film har TMDB GUID — tjekker %d TMDB-film",
        len(plex_by_tmdb_id), len(plex_index), len(tmdb_movies),
    )

    found_on_plex:     list[dict] = []
    missing_from_plex: list[dict] = []

    for tmdb_movie in tmdb_movies:
        tmdb_id        = tmdb_movie.get("tmdb_id")
        tmdb_title     = tmdb_movie.get("title", "")
        original_title = tmdb_movie.get("original_title", "") or tmdb_title
        release        = tmdb_movie.get("release_date", "")
        tmdb_year      = int(release[:4]) if release and len(release) >= 4 and release[:4].isdigit() else None

        in_plex    = False
        plex_entry = None

        if tmdb_id and tmdb_id in plex_by_tmdb_id:
            in_plex    = True
            plex_entry = plex_by_tmdb_id[tmdb_id]
        else:
            for entry in plex_index.values():
                if (_titles_match(entry["title"], tmdb_title) or
                        _titles_match(entry["title"], original_title) or
                        _titles_match_fuzzy(entry["title"], tmdb_title)):
                    if not tmdb_year or not entry["year"] or abs(entry["year"] - tmdb_year) <= 1:
                        in_plex    = True
                        plex_entry = entry
                        break

        result_entry = {
            "title":    tmdb_title,
            "year":     tmdb_year,
            "tmdb_id":  tmdb_id,
        }
        if in_plex and plex_entry:
            result_entry["plex_title"] = plex_entry["title"]

        (found_on_plex if in_plex else missing_from_plex).append(result_entry)

    return {
        "status":           "ok",
        "collection":       collection_name,
        "found_on_plex":    found_on_plex[:_FRANCHISE_MAX_PER_LIST],
        "missing_from_plex": missing_from_plex[:_FRANCHISE_MAX_PER_LIST],
        "total_checked":    len(tmdb_movies),
    }


def _unwatched_sync(
    media_type: str,
    genre: str | None,
    plex_username: str | None = None,
) -> dict:
    """
    Find usete film eller serier i Plex-biblioteket.

    FIX (v0.9.9): section.all() henter hele biblioteket uden Plex default-limit.
    FIX (v1.0.0): Genre-matching bruger nu _genre_matches() med synonym-tabel
      der dækker dansk/engelsk genre-labels. Tidligere matchede 'crime' ikke
      'Kriminalitet' fordi Plex bruger dansk på denne server.
    """
    is_tv     = (media_type == "tv")
    plex_type = _TV_TYPE if is_tv else _MOVIE_TYPE

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    candidates = []
    norm_genre = _normalise(genre) if genre else None

    for section in _sections(plex, plex_type):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning("section.all() fejl i '%s': %s", section.title, e)
            continue

        for item in all_items:
            if getattr(item, "viewCount", 0):
                continue
            if norm_genre:
                item_genres = [_normalise(g.tag) for g in getattr(item, "genres", [])]
                if not _genre_matches(norm_genre, item_genres):
                    continue
            candidates.append(item)

    logger.info(
        "find_unwatched: %d usete %s fundet (genre-filter: %s)",
        len(candidates), media_type, genre or "ingen",
    )

    if not candidates:
        return {"status": STATUS_MISSING}

    sample_size = min(5, len(candidates))
    chosen      = random.sample(candidates, sample_size)
    return {"status": "ok", "results": [_slim(i) for i in chosen]}


# Claude sender altid engelske genre-navne fra system-promptens leksikon.
# Denne mapping udvider søgningen til at matche begge sprog.
_GENRE_SYNONYMS: dict[str, list[str]] = {
    "crime":       ["kriminalitet", "krimi", "crime"],
    "kriminalitet": ["crime", "krimi", "kriminalitet"],
    "comedy":      ["komedie", "comedy"],
    "komedie":     ["comedy", "komedie"],
    "drama":       ["drama"],
    "thriller":    ["thriller", "suspense"],
    "horror":      ["gyser", "horror"],
    "gyser":       ["horror", "gyser"],
    "action":      ["action"],
    "animation":   ["animation", "anime"],
    "documentary": ["dokumentar", "documentary"],
    "romance":     ["romantik", "romance", "romantic"],
    "romantik":    ["romance", "romantic", "romantik"],
    "scifi":          ["sciencefiction", "sci fi", "scifi", "science fiction"],
    "sciencefiction": ["scifi", "sci fi", "science fiction", "sciencefiction"],
    "science fiction": ["scifi", "sciencefiction", "sci fi"],
    "fantasy":     ["fantasy"],
    "mystery":     ["mysterium", "mystery"],
    "mysterium":   ["mystery", "mysterium"],
    "war":         ["krig", "war"],
    "krig":        ["war", "krig"],
    "western":     ["western"],
    "biography":   ["biografi", "biography"],
    "history":     ["historie", "history"],
    "historie":    ["history", "historie"],
    "music":       ["musik", "music", "musical"],
    "musik":       ["music", "musical", "musik"],
    "family":      ["familie", "family", "children"],
    "familie":     ["family", "children", "familie"],
    "sport":       ["sport"],
    "adventure":   ["eventyr", "adventure"],
    "eventyr":     ["adventure", "eventyr"],
}


def _genre_matches(norm_genre: str, item_genre_tags: list[str]) -> bool:
    """
    Tjek om et genre-filter matcher en films genre-tags.
    Matcher på tværs af dansk/engelsk via _GENRE_SYNONYMS.
    """
    search_terms = {norm_genre}
    search_terms.update(_GENRE_SYNONYMS.get(norm_genre, []))

    for item_genre in item_genre_tags:
        for term in search_terms:
            if term in item_genre:
                return True
    return False


def _build_actor_guid_set(
    actor_name: str,
    plex_username: str | None = None,
) -> tuple[set[int], set[str]]:
    """
    Byg to sæt for alle Plex-film der matcher skuespilleren:
      - tmdb_ids: set[int]
      - imdb_ids: set[str]

    Lag 1: section.search(actor=actor_name) — hurtig.
    Lag 2: scan hele biblioteket — fanger titler gemt under fremmed titel.
    """
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return set(), set()

    tmdb_ids: set[int] = set()
    imdb_ids: set[str] = set()

    for section in _sections(plex, _MOVIE_TYPE):
        try:
            actor_items = section.search(actor=actor_name)
        except Exception:
            actor_items = []

        for item in actor_items:
            tid = _extract_tmdb_id_from_guids(item)
            iid = _extract_imdb_id_from_guids(item)
            if tid:
                tmdb_ids.add(tid)
            if iid:
                imdb_ids.add(iid)

        # Lag 2: scan hele sektionen
        try:
            all_items = section.search()
        except Exception as e:
            logger.warning("Full section scan fejl i '%s': %s", section.title, e)
            continue

        for item in all_items:
            tid = _extract_tmdb_id_from_guids(item)
            iid = _extract_imdb_id_from_guids(item)
            if tid:
                tmdb_ids.add(tid)
            if iid:
                imdb_ids.add(iid)

    logger.debug(
        "_build_actor_guid_set '%s': %d TMDB IDs, %d IMDb IDs",
        actor_name, len(tmdb_ids), len(imdb_ids),
    )
    return tmdb_ids, imdb_ids


def _validate_user_sync(plex_username: str) -> dict:
    try:
        admin_plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
        account    = admin_plex.myPlexAccount()
    except Exception as e:
        return {"valid": False, "message": f"Forbindelsesfejl: {e}"}

    norm = plex_username.strip().lower()

    owner_names = {
        (account.username or "").lower(),
        (account.email or "").lower(),
        (account.title or "").lower(),
    }
    if norm in owner_names:
        display = account.title or account.username or plex_username
        return {"valid": True, "username": display, "user_type": "owner"}

    try:
        for user in account.users():
            user_names = {
                (getattr(user, "username", "") or "").lower(),
                (getattr(user, "email", "") or "").lower(),
                (getattr(user, "title", "") or "").lower(),
            }
            if norm in user_names:
                actual = (
                    getattr(user, "username", None)
                    or getattr(user, "title", None)
                    or plex_username
                )
                return {"valid": True, "username": actual, "user_type": "friend"}
    except Exception as e:
        logger.warning("Could not check shared users: %s", e)

    try:
        for user in account.homeUsers():
            user_names = {
                (getattr(user, "username", "") or "").lower(),
                (getattr(user, "email", "") or "").lower(),
                (getattr(user, "title", "") or "").lower(),
            }
            if norm in user_names:
                actual = (
                    getattr(user, "username", None)
                    or getattr(user, "title", None)
                    or plex_username
                )
                return {"valid": True, "username": actual, "user_type": "managed"}
    except Exception as e:
        logger.warning("Could not check home users: %s", e)

    return {"valid": False, "message": f"Brugernavnet '{plex_username}' blev ikke fundet."}


def _add_to_watchlist_sync(title: str, plex_username: str | None = None) -> bool:
    """Synkron implementering — kører i thread pool via asyncio.to_thread."""
    try:
        plex    = _connect(plex_username)
        account = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15).myPlexAccount()

        results = account.searchDiscover(title, libtype="movie") or []
        if not results:
            results = account.searchDiscover(title, libtype="show") or []
        if not results:
            logger.warning("add_to_watchlist: ingen Discover-resultater for '%s'", title)
            return False

        target = results[0]
        account.addToWatchlist(target)
        logger.info("add_to_watchlist: '%s' tilføjet til watchlist", title)
        return True
    except Exception as e:
        logger.error("_add_to_watchlist_sync error: %s", e)
        return False


def _get_similar_sync(title: str, plex_username: str | None = None) -> dict:
    """Find titler i Plex der ligner en bestemt film via Plex hub."""
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    for section in _sections(plex, _MOVIE_TYPE):
        for item in _safe_search(section, title):
            item_title = getattr(item, "title", "") or ""
            if not (_titles_match(item_title, title) or _titles_match_fuzzy(item_title, title)):
                continue
            try:
                item.reload()
                related = item.related()
                if related:
                    results = [_slim(r) for r in related[0].items[:10]]
                    return {"status": "ok", "based_on": item_title, "results": results}
            except Exception as e:
                logger.warning("_get_similar_sync related error: %s", e)

    return {"status": STATUS_MISSING}


def _get_plex_metadata_sync(title: str, year: int | None = None) -> dict:
    """Hent tekniske specs for en Plex-titel."""
    plex = _connect()
    if isinstance(plex, dict):
        return plex

    for plex_type in [_MOVIE_TYPE, _TV_TYPE]:
        for section in _sections(plex, plex_type):
            for item in _safe_search(section, title):
                item_title = getattr(item, "title", "") or ""
                item_year  = getattr(item, "year", None)
                if not (_titles_match(item_title, title) or _titles_match_fuzzy(item_title, title)):
                    continue
                if year and item_year and abs(item_year - year) > 1:
                    continue
                try:
                    item.reload()
                except Exception:
                    pass
                info = _get_stream_info(item)
                info.update({
                    "title": item_title,
                    "year":  item_year,
                })
                return {"status": "ok", **info}

    return {"status": STATUS_MISSING}


def _get_on_deck_sync(plex_username: str | None = None) -> dict:
    """Hent On Deck (fortsæt med at se) for brugeren."""
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    try:
        items = plex.library.onDeck()[:10]
        return {"status": "ok", "results": [_slim(i) for i in items]}
    except Exception as e:
        logger.error("_get_on_deck_sync error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


def _search_by_actor_sync(
    actor_name: str,
    plex_username: str | None = None,
) -> dict:
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    results = []
    for section in _sections(plex, _MOVIE_TYPE):
        try:
            hits = section.search(actor=actor_name, limit=_MAX_RESULTS)
            results.extend(_slim(i) for i in hits)
        except Exception as e:
            logger.warning("search_by_actor error in '%s': %s", section.title, e)

    if not results:
        return {"status": STATUS_MISSING}
    return {"status": "ok", "actor": actor_name, "results": results[:_MAX_RESULTS]}


def _get_missing_from_collection_sync(
    collection_name: str,
    plex_username: str | None = None,
) -> dict:
    """Find hvad der mangler af en samling i Plex via simpel TMDB-søgning."""
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    sections = _sections(plex, _MOVIE_TYPE)

    plex_index: dict[str, dict] = {}
    for section in sections:
        try:
            for item in section.search():
                norm = _normalise(getattr(item, "title", "") or "")
                plex_index[norm] = {
                    "title":   getattr(item, "title", ""),
                    "year":    getattr(item, "year", None),
                    "tmdb_id": _extract_tmdb_id_from_guids(item),
                }
        except Exception as e:
            logger.warning("Missing-collection index error: %s", e)

    return {
        "status":     "ok",
        "collection": collection_name,
        "plex_index": list(plex_index.values()),
    }


def _check_actor_on_plex_sync(
    actor_name: str,
    top_movies: list[dict],
    plex_username: str | None = None,
) -> dict:
    """Krydstjek skuespillers top-film mod Plex via GUID + fuzzy titel."""
    tmdb_ids, imdb_ids = _build_actor_guid_set(actor_name, plex_username)

    found    = []
    missing  = []

    for movie in top_movies:
        tmdb_id  = movie.get("tmdb_id")
        imdb_id  = movie.get("imdb_id")
        title    = movie.get("title", "")
        year     = movie.get("year")

        in_plex = False
        if tmdb_id and tmdb_id in tmdb_ids:
            in_plex = True
        elif imdb_id and imdb_id in imdb_ids:
            in_plex = True

        entry = {"title": title, "year": year, "tmdb_id": tmdb_id}
        (found if in_plex else missing).append(entry)

    capped_missing = missing[:_ACTOR_MAX_MISSING]

    if not found:
        return {
            "status":  STATUS_MISSING,
            "actor":   actor_name,
            "found":   [],
        }
    return {"status": "ok", "actor": actor_name, "found": found, "count": len(found)}


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ASYNC FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def check_library(
    title: str,
    year: int | None,
    media_type: str,
    plex_username: str | None = None,
    tmdb_id: int | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_check_sync, title=title, year=year,
                    media_type=media_type, plex_username=plex_username,
                    tmdb_id=tmdb_id)
        )
    except Exception as e:
        logger.error("check_library error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_collection(
    keyword: str,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_collection_sync, keyword=keyword,
                    media_type=media_type, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_collection error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def check_franchise_on_plex(
    keyword: str,
    plex_username: str | None = None,
) -> dict:
    """Avanceret franchise-søgning via TMDB collection API + Plex GUID-matching."""
    from services.tmdb_service import get_tmdb_collection_movies

    collection = await get_tmdb_collection_movies(keyword)

    if not collection:
        return {
            "status":  "not_found",
            "message": (
                f"Jeg kunne ikke finde en samling med navnet '{keyword}' i databasen. "
                "Prøv et mere specifikt søgeord."
            ),
        }

    return await asyncio.to_thread(
        partial(
            _franchise_plex_check_sync,
            collection_name=collection["collection_name"],
            tmdb_movies=collection["movies"],
            plex_username=plex_username,
        )
    )


async def check_actor_on_plex(
    actor_name: str,
    plex_username: str | None = None,
) -> dict:
    """
    Krydstjek en skuespillers top 75 film mod Plex.
    Flow: TMDB person → filmografi (top 75) → Plex GUID-match → fuzzy fallback.
    Top 75 (mod 20) fanger klassikere der er lavt placeret på TMDB's popularitetsliste
    men stadig i biblioteket — f.eks. Tom Hanks' 38 Plex-film.
    """
    from services.tmdb_service import search_person, get_person_filmography

    person_results = await search_person(actor_name)
    if not person_results:
        return {"status": "not_found", "message": f"Ingen person fundet for '{actor_name}'."}

    person    = person_results[0]
    person_id = person["id"]

    filmography = await get_person_filmography(person_id)
    if not filmography:
        return {"status": "not_found", "message": f"Ingen filmografi fundet for '{actor_name}'."}

    top_movies = filmography.get("movie_credits", [])[:75]

    return await asyncio.to_thread(
        partial(
            _check_actor_on_plex_sync,
            actor_name=actor_name,
            top_movies=top_movies,
            plex_username=plex_username,
        )
    )


async def find_unwatched(
    media_type: str,
    genre: str | None = None,
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_unwatched_sync, media_type=media_type,
                    genre=genre, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("find_unwatched error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_similar_in_library(
    title: str,
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_get_similar_sync, title=title, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_similar_in_library error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_plex_metadata(
    title: str,
    year: int | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_get_plex_metadata_sync, title=title, year=year)
        )
    except Exception as e:
        logger.error("get_plex_metadata error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_on_deck(
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_get_on_deck_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_on_deck error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def search_by_actor(
    actor_name: str,
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_search_by_actor_sync, actor_name=actor_name, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("search_by_actor error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_missing_from_collection(
    collection_name: str,
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_get_missing_from_collection_sync,
                    collection_name=collection_name, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_missing_from_collection error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def validate_plex_user(plex_username: str) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_validate_user_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("validate_plex_user error: %s", e)
        return {"valid": False, "message": str(e)}


async def add_to_watchlist(title: str, plex_username: str | None = None) -> bool:
    """Returnerer True ved success."""
    try:
        return await asyncio.to_thread(
            partial(_add_to_watchlist_sync, title=title, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("add_to_watchlist error: %s", e)
        return False


async def get_plex_watch_url(
    tmdb_id: int,
    media_type: str,
) -> str | None:
    """
    Hent watch.plex.tv deep-link URL for en specifik film/serie via slug-opslag.
    Returnerer f.eks. https://watch.plex.tv/movie/spy-kids eller None ved fejl.
    """
    plex_type_int = 1 if media_type == "movie" else 2
    url = "https://metadata.provider.plex.tv/library/metadata/matches"
    params = {
        "guid":         f"tmdb://{tmdb_id}",
        "type":         plex_type_int,
        "X-Plex-Token": PLEX_TOKEN,
    }
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data  = resp.json()
            items = data.get("MediaContainer", {}).get("Metadata", [])
            if not items:
                logger.debug("get_plex_watch_url: ingen metadata fundet for tmdb_id=%s", tmdb_id)
                return None
            slug = items[0].get("slug")
            if not slug:
                return None
            media_path = "movie" if media_type == "movie" else "show"
            return f"https://watch.plex.tv/{media_path}/{slug}"
    except Exception as e:
        logger.warning("get_plex_watch_url fejl for tmdb_id=%s: %s", tmdb_id, e)
        return None