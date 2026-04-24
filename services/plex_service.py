"""
services/plex_service.py - Plex Media Server integration via python-plexapi.

Dynamically scans all library sections on the server — no hardcoded names.
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

CHANGES vs previous version:
  - _franchise_plex_check_sync matcher nu mod BÅDE 'title' og 'original_title'
    fra TMDB — løser falske negativer hvor Plex gemmer filmen under engelsk
    originaltitel men TMDB returnerer en oversat titel som 'title'.
  - check_franchise_on_plex + _franchise_plex_check_sync: avanceret franchise-
    søgning via TMDB collection API + lokalt Plex fuzzy-match indeks.
  - _collection_sync: animations-filter returnerer
    {"results": [top 10 ikke-animation], "hidden_animation_count": N}.
  - _clean_title + _titles_match_fuzzy: tre-lags fuzzy matching der fanger
    Plex-titler med regions-tags eller årstal i parentes.
"""

import asyncio
import logging
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

_TV_YEAR_TOLERANCE   = 2
_COLLECTION_MAX_MAIN = 10
_FRANCHISE_MAX_PER_LIST = 20


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
    }


def _is_animation(item) -> bool:
    genres = [g.tag.lower() for g in getattr(item, "genres", [])]
    return "animation" in genres


# ── Title normalisation ───────────────────────────────────────────────────────

def _normalise(title: str) -> str:
    nfkd      = unicodedata.normalize("NFKD", title)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    s = ascii_str.lower()
    s = re.sub(r"[-_]", " ", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(the|a|an|den|det|en|et)\s+", "", s)
    return s


def _clean_title(title: str) -> str:
    """
    Aggressiv titel-rensning: fjerner parenteser, specialtegn og artikler.
    Bruges til fuzzy matching — aldrig til at overskrive titler.

    'Euphoria (US)'     → 'euphoria'
    'Invincible (2021)' → 'invincible'
    'Iron Man 3'        → 'iron man 3'
    """
    s = title.lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(the|a|an|den|det|en|et)\s+", "", s)
    return s


def _titles_match(title_a: str, title_b: str) -> bool:
    """Lag 1 — eksakt normaliseret match."""
    return _normalise(title_a) == _normalise(title_b)


def _titles_match_fuzzy(title_a: str, title_b: str) -> bool:
    """
    Lag 2 + 3 — fuzzy match via _clean_title.
    Lag 2: identiske rensede titler.
    Lag 3: den ene er substring af den anden (min. 4 tegn).
    """
    clean_a = _clean_title(title_a)
    clean_b = _clean_title(title_b)
    if not clean_a or not clean_b:
        return False
    if clean_a == clean_b:
        return True
    min_len = 4
    if len(clean_a) >= min_len and len(clean_b) >= min_len:
        if clean_a in clean_b or clean_b in clean_a:
            return True
    return False


def _year_ok_for_tv(item_year: int | None, query_year: int | None) -> bool:
    if item_year is None or query_year is None:
        return True
    return abs(item_year - query_year) <= _TV_YEAR_TOLERANCE


# ── Plex connection helper ────────────────────────────────────────────────────

def _connect(plex_username: str | None = None) -> PlexServer | dict:
    try:
        admin_plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
    except Unauthorized:
        logger.error("Plex auth failed — check PLEX_TOKEN")
        return {"status": STATUS_ERROR, "message": "Ugyldig Plex-token."}
    except Exception as e:
        logger.error("Plex connection error: %s", e)
        return {"status": STATUS_ERROR, "message": f"Kunne ikke forbinde til Plex: {e}"}

    if not plex_username:
        return admin_plex

    norm = plex_username.strip().lower()

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
                    user_token = account.user(user.username).get_token(
                        admin_plex.machineIdentifier
                    )
                    return PlexServer(PLEX_URL, user_token, timeout=15)
                except Exception as e:
                    logger.warning("get_token() failed for '%s': %s", plex_username, e)

        try:
            home_users = account.homeUsers()
        except Exception:
            home_users = []

        for user in home_users:
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
                    logger.warning("switchHomeUser() failed for '%s': %s", plex_username, e)
                    return admin_plex

        logger.warning("Plex user '%s' not found — falling back to admin", plex_username)
        return admin_plex

    except Exception as e:
        logger.warning("_connect() error for '%s': %s — falling back to admin", plex_username, e)
        return admin_plex


def _sections(plex: PlexServer, plex_type: str) -> list:
    try:
        return [s for s in plex.library.sections() if s.type == plex_type]
    except Exception as e:
        logger.error("Could not fetch sections: %s", e)
        return []


def _safe_search(section, title: str) -> list:
    try:
        first_word = _normalise(title).split()[0] if _normalise(title).split() else title
        return section.search(title=first_word)
    except Exception as e:
        logger.warning("Search error in '%s': %s", section.title, e)
        return []


# ── Technical specs helper ────────────────────────────────────────────────────

def _stream_info(media_item) -> dict:
    info = {
        "resolution": None, "hdr": False, "video_codec": None,
        "audio_codec": None, "audio_channels": None,
        "bitrate_kbps": None, "container": None, "duration_minutes": None,
    }
    try:
        media = media_item.media[0] if media_item.media else None
        if not media:
            return info
        info["resolution"]       = getattr(media, "videoResolution", None)
        info["bitrate_kbps"]     = getattr(media, "bitrate", None)
        info["container"]        = getattr(media, "container", None)
        info["duration_minutes"] = (
            round(getattr(media_item, "duration", 0) / 60000)
            if getattr(media_item, "duration", None) else None
        )
        part = media.parts[0] if media.parts else None
        if part:
            for stream in getattr(part, "streams", []):
                stype = getattr(stream, "streamType", None)
                if stype == 1:
                    info["video_codec"] = getattr(stream, "codec", None)
                    color_trc = getattr(stream, "colorTrc", "") or ""
                    dv = getattr(stream, "DOVIPresent", False)
                    info["hdr"] = bool(
                        dv
                        or "smpte2084" in color_trc.lower()
                        or "arib-std-b67" in color_trc.lower()
                    )
                elif stype == 2 and not info["audio_codec"]:
                    info["audio_codec"]    = getattr(stream, "codec", None)
                    info["audio_channels"] = getattr(stream, "channels", None)
    except Exception as e:
        logger.warning("Stream info error: %s", e)
    return info


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ASYNC FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def check_library(
    title: str,
    year: int | None,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_check_sync, title=title, year=year,
                    media_type=media_type, plex_username=plex_username)
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
    """
    Avanceret franchise-søgning: henter autoritativ film-liste fra TMDB
    og krydstjekker mod Plex via fuzzy matching på BÅDE title og original_title.
    """
    from services.tmdb_service import get_tmdb_collection_movies

    collection = await get_tmdb_collection_movies(keyword)

    if not collection:
        return {
            "status":  "not_found",
            "message": (
                f"Jeg kunne ikke finde en samling med navnet '{keyword}' i databasen. "
                "Prøv et mere specifikt søgeord, f.eks. 'James Bond' eller "
                "'Marvel Cinematic Universe'."
            ),
        }

    collection_name = collection["collection_name"]
    tmdb_movies     = collection["movies"]

    logger.info(
        "check_franchise_on_plex: '%s' → '%s' (%d film) — starter Plex-tjek",
        keyword, collection_name, len(tmdb_movies),
    )

    return await asyncio.to_thread(
        partial(
            _franchise_plex_check_sync,
            collection_name=collection_name,
            tmdb_movies=tmdb_movies,
            plex_username=plex_username,
        )
    )


async def get_on_deck(plex_username: str | None = None) -> dict:
    try:
        return await asyncio.to_thread(partial(_on_deck_sync, plex_username=plex_username))
    except Exception as e:
        logger.error("get_on_deck error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_plex_metadata(
    title: str,
    year: int | None,
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_metadata_sync, title=title, year=year, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_plex_metadata error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


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
            partial(_similar_sync, title=title, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_similar_in_library error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_missing_from_collection(
    collection_name: str,
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_missing_sync, collection_name=collection_name,
                    plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_missing_from_collection error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def search_by_actor(
    actor_name: str,
    media_type: str = "movie",
    plex_username: str | None = None,
) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_actor_sync, actor_name=actor_name,
                    media_type=media_type, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("search_by_actor error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SYNC IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _franchise_plex_check_sync(
    collection_name: str,
    tmdb_movies: list[dict],
    plex_username: str | None = None,
) -> dict:
    """
    Synkron kerne af check_franchise_on_plex — kører i thread pool.

    FIX: Matcher nu mod BÅDE 'title' (oversat, da-DK) og 'original_title'
    per TMDB-film. Løser falske negativer hvor:
      - TMDB title='Jernmand', original_title='Iron Man', men Plex har 'Iron Man'
      - TMDB title='Iron Man', original_title='Iron Man', og Plex har 'Iron Man'

    Match-logik per TMDB-film:
      1. Byg ét Plex-indeks over alle film-sektioner (ingen gentagne API-kald).
      2. Rens Plex-titel, TMDB title og TMDB original_title via _clean_title().
      3. Godkend som fundet hvis Plex-titel matcher NOGEN af de to TMDB-titler
         via _titles_match() (Lag 1) ELLER _titles_match_fuzzy() (Lag 2/3).
      4. original_title-tjek kortslutter hvis original_title == title
         (undgår dobbelt-arbejde for engelske film).
    """
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    sections = _sections(plex, _MOVIE_TYPE)
    if not sections:
        return {
            "status":  STATUS_ERROR,
            "message": "Ingen film-sektioner fundet i Plex.",
        }

    # Byg lokalt Plex-indeks: {ratingKey: (plex_title, plex_year)}
    plex_index: dict[int, tuple[str, int | None]] = {}
    for section in sections:
        try:
            for item in section.search():
                key = getattr(item, "ratingKey", None)
                if key and key not in plex_index:
                    plex_index[key] = (
                        getattr(item, "title", "") or "",
                        getattr(item, "year", None),
                    )
        except Exception as e:
            logger.warning("Franchise index error in '%s': %s", section.title, e)

    logger.info(
        "check_franchise_on_plex: Plex-indeks bygget (%d film) mod %d TMDB-film",
        len(plex_index), len(tmdb_movies),
    )

    found_on_plex:     list[dict] = []
    missing_from_plex: list[dict] = []

    for tmdb_movie in tmdb_movies:
        tmdb_title     = tmdb_movie.get("title", "")
        original_title = tmdb_movie.get("original_title", "") or tmdb_title
        release        = tmdb_movie.get("release_date", "")
        tmdb_year      = int(release[:4]) if release and release[:4].isdigit() else None

        # titles er identiske for engelske film — undgår dobbelt-arbejde
        titles_differ = original_title != tmdb_title

        matched = False
        for plex_title, plex_year in plex_index.values():
            # Årstals-tjek: max 1 år afvigelse eller manglende år
            if tmdb_year and plex_year and abs(plex_year - tmdb_year) > 1:
                continue

            # Lag 1 + 2/3 mod oversat TMDB-titel
            match_translated = (
                _titles_match(plex_title, tmdb_title) or
                _titles_match_fuzzy(plex_title, tmdb_title)
            )

            # Lag 1 + 2/3 mod original TMDB-titel (kun hvis den adskiller sig)
            match_original = titles_differ and (
                _titles_match(plex_title, original_title) or
                _titles_match_fuzzy(plex_title, original_title)
            )

            if match_translated or match_original:
                matched = True
                logger.debug(
                    "Franchise HIT: plex='%s' ← tmdb='%s' / orig='%s' "
                    "(år: plex=%s tmdb=%s, via=%s)",
                    plex_title, tmdb_title, original_title, plex_year, tmdb_year,
                    "translated" if match_translated else "original",
                )
                break

        if matched:
            found_on_plex.append({"title": tmdb_title, "year": tmdb_year})
        else:
            missing_from_plex.append({
                "title":        tmdb_title,
                "release_date": release or "Ukendt",
            })

    found_count   = len(found_on_plex)
    missing_count = len(missing_from_plex)

    logger.info(
        "check_franchise_on_plex '%s': %d/%d fundet, %d mangler",
        collection_name, found_count, len(tmdb_movies), missing_count,
    )

    return {
        "status":             "ok",
        "collection_name":    collection_name,
        "total_in_franchise": len(tmdb_movies),
        "found_count":        found_count,
        "missing_count":      missing_count,
        "found_on_plex":      found_on_plex[:_FRANCHISE_MAX_PER_LIST],
        "missing_from_plex":  missing_from_plex[:_FRANCHISE_MAX_PER_LIST],
    }


def _check_sync(
    title: str,
    year: int | None,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    """
    Tre-lags fuzzy titel-matching.
    Lag 1: eksakt normaliseret. Lag 2: renset (_clean_title). Lag 3: substring.
    TV: løst årstal (+/- 2 år). Film: strengt (max 1 år).
    Alle sektioner af den givne type gennemsøges.
    """
    is_tv     = (media_type == "tv")
    plex_type = _TV_TYPE if is_tv else _MOVIE_TYPE

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    sections = _sections(plex, plex_type)
    if not sections:
        logger.warning("check_library: ingen sektioner af type '%s'", plex_type)

    for section in sections:
        for item in _safe_search(section, title):
            item_title = getattr(item, "title", "") or ""
            item_year  = getattr(item, "year", None)

            if not (_titles_match(item_title, title) or
                    _titles_match_fuzzy(item_title, title)):
                continue

            match_lag = 1 if _titles_match(item_title, title) else "2/3"

            if is_tv:
                if not _year_ok_for_tv(item_year, year):
                    continue
            else:
                if year and item_year and abs(item_year - year) > 1:
                    continue

            logger.info(
                "Plex HIT (lag %s): '%s' (%s) i '%s' — søgt på '%s' (%s)",
                match_lag, item_title, item_year, section.title, title, year,
            )
            return {"status": STATUS_FOUND, "title": item_title, "year": item_year}

    logger.info("Plex MISS: '%s' (%s) ikke fundet", title, year)
    return {"status": STATUS_MISSING}


def _collection_sync(
    keyword: str,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    """
    Simpel Plex-tekstsøgning med animations-filter.
    Returnerer {"results": [top 10 ikke-animation], "hidden_animation_count": N}.
    """
    plex_type = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    norm_keyword     = _normalise(keyword)
    main_results     = []
    animated_results = []

    for section in _sections(plex, plex_type):
        for item in _safe_search(section, keyword):
            item_title = getattr(item, "title", "") or ""
            if norm_keyword.split()[0] not in _normalise(item_title):
                continue
            if _is_animation(item):
                animated_results.append(item)
            else:
                main_results.append(item)

    main_results.sort(
        key=lambda x: (
            getattr(x, "year", 0) or 0,
            getattr(x, "audienceRating", 0) or 0,
        ),
        reverse=True,
    )

    top_main = [_slim(i) for i in main_results[:_COLLECTION_MAX_MAIN]]

    logger.info(
        "get_plex_collection '%s': %d ikke-animerede (viser %d), %d animerede (skjult)",
        keyword, len(main_results), len(top_main), len(animated_results),
    )

    return {
        "status":                 "ok",
        "keyword":                keyword,
        "results":                top_main,
        "hidden_animation_count": len(animated_results),
    }


def _on_deck_sync(plex_username: str | None = None) -> dict:
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex
    try:
        on_deck = plex.library.onDeck()[:8]
    except Exception as e:
        logger.error("onDeck error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}

    items = []
    for item in on_deck:
        entry = _slim(item)
        entry["type"] = getattr(item, "type", "unknown")
        if item.type == "episode":
            entry["show"]          = getattr(item, "grandparentTitle", None)
            entry["season"]        = getattr(item, "parentIndex", None)
            entry["episode"]       = getattr(item, "index", None)
            entry["episode_title"] = getattr(item, "title", None)
        items.append(entry)
    return {"status": "ok", "on_deck": items, "count": len(items)}


def _metadata_sync(
    title: str,
    year: int | None,
    plex_username: str | None = None,
) -> dict:
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    for plex_type in (_MOVIE_TYPE, _TV_TYPE):
        for section in _sections(plex, plex_type):
            for item in _safe_search(section, title):
                item_title = getattr(item, "title", "") or ""
                item_year  = getattr(item, "year", None)
                if not (_titles_match(item_title, title) or
                        _titles_match_fuzzy(item_title, title)):
                    continue
                if year and item_year and abs(item_year - year) > 1:
                    continue
                return {
                    "status": STATUS_FOUND,
                    "title":  item_title,
                    "year":   item_year,
                    "type":   plex_type,
                    "specs":  _stream_info(item),
                }
    return {"status": STATUS_MISSING, "message": f"'{title}' ikke fundet i Plex."}


def _unwatched_sync(
    media_type: str,
    genre: str | None,
    plex_username: str | None = None,
) -> dict:
    plex_type  = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE
    plex       = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    unwatched  = []
    norm_genre = _normalise(genre) if genre else None

    for section in _sections(plex, plex_type):
        try:
            results = section.search(unwatched=True)
        except Exception:
            try:
                results = section.search()
            except Exception as e:
                logger.warning("Unwatched search error in '%s': %s", section.title, e)
                continue
        for item in results:
            if getattr(item, "viewCount", 0) > 0:
                continue
            if norm_genre:
                item_genres = [_normalise(g.tag) for g in getattr(item, "genres", [])]
                if not any(norm_genre in g for g in item_genres):
                    continue
            unwatched.append(_slim(item))

    random.shuffle(unwatched)
    return {"status": "ok", "suggestions": unwatched[:6], "total_unwatched": len(unwatched)}


def _similar_sync(title: str, plex_username: str | None = None) -> dict:
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    source_item = source_section = None

    for plex_type in (_MOVIE_TYPE, _TV_TYPE):
        for section in _sections(plex, plex_type):
            for item in _safe_search(section, title):
                if (_titles_match(getattr(item, "title", ""), title) or
                        _titles_match_fuzzy(getattr(item, "title", ""), title)):
                    source_item    = item
                    source_section = section
                    break
            if source_item:
                break
        if source_item:
            break

    if not source_item:
        return {"status": STATUS_MISSING, "message": f"'{title}' ikke fundet i Plex."}

    similar = []
    try:
        related = source_item.related()
        for hub in related:
            for item in getattr(hub, "items", [])[:10]:
                if _normalise(getattr(item, "title", "")) == _normalise(title):
                    continue
                similar.append(_slim(item))
        if similar:
            return {
                "status": "ok",
                "source": getattr(source_item, "title", title),
                "similar": similar[:_MAX_RESULTS],
            }
    except Exception as e:
        logger.warning("Plex related() failed, falling back to genre match: %s", e)

    source_genres = {_normalise(g.tag) for g in getattr(source_item, "genres", [])}
    if not source_genres:
        return {"status": "ok", "source": getattr(source_item, "title", title), "similar": []}

    candidates = []
    try:
        for item in source_section.search():
            if _normalise(getattr(item, "title", "")) == _normalise(title):
                continue
            item_genres = {_normalise(g.tag) for g in getattr(item, "genres", [])}
            overlap = len(source_genres & item_genres)
            if overlap > 0:
                candidates.append((overlap, _slim(item)))
        candidates.sort(key=lambda x: x[0], reverse=True)
        similar = [c[1] for c in candidates[:_MAX_RESULTS]]
    except Exception as e:
        logger.warning("Genre fallback error: %s", e)

    return {"status": "ok", "source": getattr(source_item, "title", title), "similar": similar}


def _missing_sync(collection_name: str, plex_username: str | None = None) -> dict:
    import httpx as _httpx

    try:
        resp = _httpx.get(
            f"{_TMDB_BASE}/search/movie",
            params={"api_key": TMDB_API_KEY, "language": _TMDB_LANG, "query": collection_name},
            timeout=10,
        )
        resp.raise_for_status()
        tmdb_results = resp.json().get("results", [])[:30]
    except Exception as e:
        logger.error("TMDB search error in _missing_sync: %s", e)
        return {"status": STATUS_ERROR, "message": f"Kunne ikke søge på titler: {e}"}

    if not tmdb_results:
        return {"status": "ok", "found_in_plex": [], "missing_from_plex": [], "total": 0}

    norm_kw  = _normalise(collection_name).split()[0]
    relevant = [
        r for r in tmdb_results
        if norm_kw in _normalise(r.get("title") or r.get("original_title") or "")
    ]
    if not relevant:
        relevant = tmdb_results[:15]

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    found_in_plex = []
    missing_from_plex = []

    for movie in relevant:
        tmdb_title = movie.get("title") or movie.get("original_title") or ""
        tmdb_year  = int((movie.get("release_date") or "0")[:4] or 0) or None
        in_plex    = False

        for section in _sections(plex, _MOVIE_TYPE):
            for item in _safe_search(section, tmdb_title):
                item_title = getattr(item, "title", "") or ""
                item_year  = getattr(item, "year", None)
                if not (_titles_match(item_title, tmdb_title) or
                        _titles_match_fuzzy(item_title, tmdb_title)):
                    continue
                if tmdb_year and item_year and abs(item_year - tmdb_year) > 1:
                    continue
                in_plex = True
                break
            if in_plex:
                break

        entry = {"title": tmdb_title, "year": tmdb_year, "tmdb_id": movie.get("id")}
        if in_plex:
            found_in_plex.append(entry)
        else:
            missing_from_plex.append(entry)

    return {
        "status":            "ok",
        "collection":        collection_name,
        "found_in_plex":     found_in_plex[:_MAX_RESULTS],
        "missing_from_plex": missing_from_plex[:_MAX_RESULTS],
        "total_checked":     len(relevant),
    }


def _actor_sync(
    actor_name: str,
    media_type: str = "movie",
    plex_username: str | None = None,
) -> dict:
    plex_type  = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE
    plex       = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    matches    = []
    norm_actor = _normalise(actor_name)

    for section in _sections(plex, plex_type):
        try:
            for item in section.search(actor=actor_name):
                matches.append(_slim(item))
        except Exception:
            try:
                for item in section.search():
                    roles = getattr(item, "roles", []) or []
                    for role in roles:
                        role_name = _normalise(getattr(role, "tag", "") or "")
                        if norm_actor in role_name or role_name in norm_actor:
                            matches.append(_slim(item))
                            break
            except Exception as e:
                logger.warning("Actor fallback error in '%s': %s", section.title, e)

    matches.sort(key=lambda x: x.get("year") or 0, reverse=True)
    capped = matches[:_MAX_RESULTS]

    if not capped:
        return {
            "status":  "not_found",
            "message": f"Ingen titler med '{actor_name}' fundet i Plex-biblioteket.",
            "actor":   actor_name,
        }
    return {"status": "ok", "actor": actor_name, "found": capped, "count": len(capped)}


# ── Plex user validation ──────────────────────────────────────────────────────

async def validate_plex_user(plex_username: str) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_validate_user_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("validate_plex_user error: %s", e)
        return {"valid": False, "message": str(e)}


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

    return {
        "valid":   False,
        "message": f"Brugernavnet '{plex_username}' blev ikke fundet.",
    }


async def get_plex_for_user(plex_username: str):
    try:
        return await asyncio.to_thread(
            partial(_get_user_server_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_plex_for_user error: %s", e)
        return None


def _get_user_server_sync(plex_username: str):
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return None
    try:
        account    = plex.myPlexAccount()
        norm_input = plex_username.strip().lower()
        owner_names = {
            (account.username or "").lower(),
            (account.email or "").lower(),
            (account.title or "").lower(),
        }
        if norm_input in owner_names:
            return plex
        for user in account.users():
            user_names = {
                (getattr(user, "username", "") or "").lower(),
                (getattr(user, "email", "") or "").lower(),
                (getattr(user, "title", "") or "").lower(),
            }
            if norm_input in user_names:
                user_token = account.switchHomeUser(user).authToken
                return PlexServer(PLEX_URL, user_token, timeout=15)
    except Exception as e:
        logger.error("User server switch error: %s", e)
    return None