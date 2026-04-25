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
  - check_actor_on_plex(): tmdb_id tilføjet til hvert element i
    found_on_plex og missing_top_movies. Buddy modtager nu ID'erne
    direkte fra tool-output og behøver ikke gætte eller udelade links.
  - _check_sync(): item.reload() + ratingKey + machineIdentifier — uændret.
  - add_to_watchlist() — uændret.
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
_FRANCHISE_MAX_PER_LIST = 20
_ACTOR_MAX_MISSING      = 15   # Maks manglende film i check_actor_on_plex output


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


def _extract_tmdb_id_from_guids(item) -> int | None:
    """
    Udtræk TMDB ID fra et Plex-items .guids liste.
    Format: [Guid(id='tmdb://671'), Guid(id='imdb://tt0241527'), ...]
    Returnerer numerisk TMDB ID eller None.
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
    Bruges kun til sammenligning — aldrig til at overskrive titler.
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
    """Lag 2 + 3 — fuzzy via _clean_title (identiske eller substring, min 4 tegn)."""
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
    Bridge-funktion: krydstjek en skuespillers top 20 film mod Plex.

    Flow:
      1. search_person(actor_name) → find TMDB person_id og bekræftet navn.
      2. get_person_filmography(person_id) → hent top 20 film (sorteret efter
         popularitets-score: vote_average × log(vote_count + 1)).
      3. _actor_sync() → hent alle film skuespilleren har på Plex lokalt.
      4. Byg Plex GUID-indeks fra actor-resultater for hurtig primær matching.
      5. Kryds-tjek top-20 mod Plex: GUID-match (primær) → fuzzy (fallback).

    Returnerer:
      {
        "actor":              "Robert Downey Jr.",
        "tmdb_person_id":     3223,
        "found_on_plex":      [{"title": ..., "year": ..., "character": ...}],
        "missing_top_movies": [{"title": ..., "release_date": ..., "vote_average": ...}],
        "found_count":        N,
        "missing_count":      M,
        "checked_top_n":      20,
      }
    """
    from services.tmdb_service import get_person_filmography, search_person

    # Trin 1: find personen på TMDB
    persons = await search_person(actor_name)
    if not persons:
        return {
            "status":  "not_found",
            "message": f"Kunne ikke finde '{actor_name}' i filmdatabasen.",
        }

    person     = persons[0]
    person_id  = person["id"]
    actor_display = person["name"]

    logger.info(
        "check_actor_on_plex: '%s' → '%s' (person_id=%s)",
        actor_name, actor_display, person_id,
    )

    # Trin 2: hent top-20 filmografi fra TMDB
    filmography = await get_person_filmography(person_id)
    if not filmography or not filmography.get("movie_credits"):
        return {
            "status":  "no_credits",
            "message": f"Ingen filmografi fundet for '{actor_display}'.",
            "actor":   actor_display,
        }

    top_movies = filmography["movie_credits"]  # max 20, sorteret efter popularitet

    # Trin 3: hent Plex-film med skuespilleren (synkront i thread)
    plex_result = await asyncio.to_thread(
        partial(_actor_sync, actor_name=actor_display,
                media_type="movie", plex_username=plex_username)
    )

    # Byg Plex-indeks fra actor-søgning
    # plex_result["found"] er en liste af _slim()-dicts — ingen guids her.
    # Vi laver et supplerende fuldt Plex-indeks med GUIDs for GUID-matching.
    plex_actor_titles: set[str] = set()   # rensede titler fra _actor_sync
    plex_tmdb_ids:     set[int] = set()   # TMDB IDs fra GUIDs

    if plex_result.get("status") in ("ok", "not_found"):
        # Byg titel-sæt fra _actor_sync-resultater (hurtig fuzzy-fallback)
        for entry in plex_result.get("found", []):
            t = entry.get("title", "")
            if t:
                plex_actor_titles.add(_clean_title(t))

        # Byg GUID-sæt: søg Plex bredt med skuespillerens navn for at hente .guids
        plex_tmdb_ids = await asyncio.to_thread(
            partial(_build_actor_guid_set, actor_name=actor_display,
                    plex_username=plex_username)
        )

    logger.info(
        "check_actor_on_plex: Plex har %d titler med '%s' (%d via GUID, %d via titel)",
        len(plex_result.get("found", [])), actor_display,
        len(plex_tmdb_ids), len(plex_actor_titles),
    )

    # Trin 4: kryds-tjek top-20 mod Plex
    found_on_plex:      list[dict] = []
    missing_top_movies: list[dict] = []

    for movie in top_movies:
        tmdb_id        = movie.get("tmdb_id")
        title          = movie.get("title", "")
        original_title = movie.get("original_title", "") or title
        release        = movie.get("release_date", "")
        year           = int(release[:4]) if release and len(release) >= 4 and release[:4].isdigit() else None

        # Lag 1: GUID-match
        if tmdb_id and tmdb_id in plex_tmdb_ids:
            found_on_plex.append({
                "title":     title,
                "year":      year,
                "character": movie.get("character"),
                "tmdb_id":   tmdb_id,
            })
            continue

        # Lag 2/3: fuzzy titel-match mod Plex actor-titler
        clean_tmdb          = _clean_title(title)
        clean_tmdb_original = _clean_title(original_title)
        titles_differ       = original_title != title

        fuzzy_hit = (
            clean_tmdb in plex_actor_titles or
            (titles_differ and clean_tmdb_original in plex_actor_titles)
        )

        if not fuzzy_hit:
            # Bredere fuzzy: substring-check mod alle Plex-titler
            for plex_clean in plex_actor_titles:
                if (len(clean_tmdb) >= 4 and len(plex_clean) >= 4 and
                        (clean_tmdb in plex_clean or plex_clean in clean_tmdb)):
                    fuzzy_hit = True
                    break
                if titles_differ and (
                        len(clean_tmdb_original) >= 4 and len(plex_clean) >= 4 and
                        (clean_tmdb_original in plex_clean or
                         plex_clean in clean_tmdb_original)):
                    fuzzy_hit = True
                    break

        if fuzzy_hit:
            found_on_plex.append({
                "title":     title,
                "year":      year,
                "character": movie.get("character"),
                "tmdb_id":   tmdb_id,
            })
        else:
            missing_top_movies.append({
                "title":        title,
                "release_date": release or "Ukendt",
                "vote_average": movie.get("vote_average", 0),
                "tmdb_id":      tmdb_id,
            })

    found_count   = len(found_on_plex)
    missing_count = len(missing_top_movies)

    logger.info(
        "check_actor_on_plex '%s': %d/%d fundet på Plex, %d mangler",
        actor_display, found_count, len(top_movies), missing_count,
    )

    return {
        "status":           "ok",
        "actor":            actor_display,
        "tmdb_person_id":   person_id,
        "found_on_plex":    found_on_plex,
        "missing_top_movies": missing_top_movies[:_ACTOR_MAX_MISSING],
        "found_count":      found_count,
        "missing_count":    missing_count,
        "checked_top_n":    len(top_movies),
    }


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

def _build_actor_guid_set(
    actor_name: str,
    plex_username: str | None = None,
) -> set[int]:
    """
    Byg et sæt af TMDB IDs for alle Plex-film der indeholder skuespilleren.
    Bruger PlexAPI's actor-filter og udtrækker .guids fra hvert item.
    Kører i thread pool — returnerer set[int].
    """
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return set()

    tmdb_ids: set[int] = set()

    for section in _sections(plex, _MOVIE_TYPE):
        try:
            items = section.search(actor=actor_name)
        except Exception:
            try:
                # Fallback: scan hele sektionen
                items = section.search()
            except Exception as e:
                logger.warning("GUID set build error in '%s': %s", section.title, e)
                continue

        for item in items:
            tid = _extract_tmdb_id_from_guids(item)
            if tid:
                tmdb_ids.add(tid)

    return tmdb_ids


def _franchise_plex_check_sync(
    collection_name: str,
    tmdb_movies: list[dict],
    plex_username: str | None = None,
) -> dict:
    """
    GUID-matching som primær metode, fuzzy titel som fallback.
    Se fuld dokumentation i forrige version.
    """
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    sections = _sections(plex, _MOVIE_TYPE)
    if not sections:
        return {"status": STATUS_ERROR, "message": "Ingen film-sektioner fundet i Plex."}

    # Byg indeks: {ratingKey: {"title", "year", "tmdb_id"}}
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
        tmdb_year      = int(release[:4]) if release and release[:4].isdigit() else None

        matched      = False
        match_method = None

        # Lag 1: GUID
        if tmdb_id and tmdb_id in plex_by_tmdb_id:
            matched      = True
            match_method = "guid"

        # Lag 2/3: fuzzy fallback
        if not matched:
            titles_differ = original_title != tmdb_title
            for entry in plex_index.values():
                plex_title = entry["title"]
                plex_year  = entry["year"]
                if tmdb_year and plex_year and abs(plex_year - tmdb_year) > 1:
                    continue
                if (_titles_match(plex_title, tmdb_title) or
                        _titles_match_fuzzy(plex_title, tmdb_title) or
                        (titles_differ and (
                            _titles_match(plex_title, original_title) or
                            _titles_match_fuzzy(plex_title, original_title)))):
                    matched      = True
                    match_method = "fuzzy"
                    break

        if matched:
            found_on_plex.append({"title": tmdb_title, "year": tmdb_year,
                                   "match_method": match_method})
        else:
            missing_from_plex.append({"title": tmdb_title, "release_date": release or "Ukendt"})

    found_count   = len(found_on_plex)
    missing_count = len(missing_from_plex)
    guid_hits     = sum(1 for f in found_on_plex if f.get("match_method") == "guid")

    logger.info(
        "check_franchise_on_plex '%s': %d/%d fundet (%d GUID, %d fuzzy), %d mangler",
        collection_name, found_count, len(tmdb_movies),
        guid_hits, found_count - guid_hits, missing_count,
    )

    clean_found = [{"title": f["title"], "year": f["year"]} for f in found_on_plex]

    return {
        "status":             "ok",
        "collection_name":    collection_name,
        "total_in_franchise": len(tmdb_movies),
        "found_count":        found_count,
        "missing_count":      missing_count,
        "found_on_plex":      clean_found[:_FRANCHISE_MAX_PER_LIST],
        "missing_from_plex":  missing_from_plex[:_FRANCHISE_MAX_PER_LIST],
    }


def _check_sync(
    title: str,
    year: int | None,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    is_tv     = (media_type == "tv")
    plex_type = _TV_TYPE if is_tv else _MOVIE_TYPE

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    for section in _sections(plex, plex_type):
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

            # reload() henter det fulde metadata-objekt inkl. ratings
            # (søgeresultater er "lette" objekter uden ratings)
            try:
                item.reload()
            except Exception as e:
                logger.warning("item.reload() fejlede for '%s': %s", item_title, e)

            p_rating    = getattr(item, "rating", None)
            a_rating    = getattr(item, "audienceRating", None)
            final_rating = p_rating if p_rating else a_rating
            logger.debug(
                "Plex ratings for '%s': rating=%s audienceRating=%s → bruger=%s",
                item_title, p_rating, a_rating, final_rating,
            )

            return {
                "status":            STATUS_FOUND,
                "title":             item_title,
                "year":              item_year,
                "ratingKey":         item.ratingKey,
                "machineIdentifier": plex.machineIdentifier,
                "rating":            final_rating,
            }

    return {"status": STATUS_MISSING}


def _collection_sync(
    keyword: str,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    """Simpel Plex-tekstsøgning med animations-filter."""
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
        key=lambda x: (getattr(x, "year", 0) or 0, getattr(x, "audienceRating", 0) or 0),
        reverse=True,
    )
    top_main = [_slim(i) for i in main_results[:_COLLECTION_MAX_MAIN]]

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
                    "status": STATUS_FOUND, "title": item_title,
                    "year": item_year, "type": plex_type,
                    "specs": _stream_info(item),
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
            return {"status": "ok", "source": getattr(source_item, "title", title),
                    "similar": similar[:_MAX_RESULTS]}
    except Exception as e:
        logger.warning("Plex related() failed: %s", e)

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
        return {"status": STATUS_ERROR, "message": f"Kunne ikke søge på titler: {e}"}

    if not tmdb_results:
        return {"status": "ok", "found_in_plex": [], "missing_from_plex": [], "total": 0}

    norm_kw  = _normalise(collection_name).split()[0]
    relevant = [
        r for r in tmdb_results
        if norm_kw in _normalise(r.get("title") or r.get("original_title") or "")
    ] or tmdb_results[:15]

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
        (found_in_plex if in_plex else missing_from_plex).append(entry)

    return {
        "status": "ok", "collection": collection_name,
        "found_in_plex": found_in_plex[:_MAX_RESULTS],
        "missing_from_plex": missing_from_plex[:_MAX_RESULTS],
        "total_checked": len(relevant),
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
            "found":   [],
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

    return {"valid": False, "message": f"Brugernavnet '{plex_username}' blev ikke fundet."}


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

# ── Watchlist ─────────────────────────────────────────────────────────────────

async def add_to_watchlist(
    title: str,
    plex_username: str | None = None,
) -> bool:
    """
    Tilføj en titel til Plex Watchlist via myPlexAccount.searchDiscover().
    Returnerer True ved success, False hvis titlen ikke kunne findes.
    Kører synkron PlexAPI i thread pool for at undgå blocking.
    """
    try:
        return await asyncio.to_thread(
            partial(_add_to_watchlist_sync, title=title, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("add_to_watchlist error: %s", e)
        return False


def _add_to_watchlist_sync(title: str, plex_username: str | None = None) -> bool:
    """Synkron implementering — kører i thread pool via asyncio.to_thread."""
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        logger.error("add_to_watchlist: kunne ikke forbinde: %s", plex)
        return False

    try:
        account = plex.myPlexAccount()
        results = account.searchDiscover(title)
        if not results:
            logger.warning("Watchlist: ingen resultater for '%s' i Discover", title)
            return False

        item = results[0]
        account.addToWatchlist(item)
        logger.info("Watchlist: '%s' tilføjet ('%s')", title, getattr(item, "title", title))
        return True

    except Exception as e:
        logger.error("Watchlist add error for '%s': %s", title, e)
        return False