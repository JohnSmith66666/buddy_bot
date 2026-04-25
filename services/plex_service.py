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

CHANGES vs previous version (v0.9.9 — find_unwatched fix):
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
  - _check_sync(), _franchise_plex_check_sync(). Uændret.
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
                return guid_str.replace("imdb://", "").strip()
    except Exception as e:
        logger.debug("_extract_imdb_id_from_guids error: %s", e)
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
    s = title.lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(the|a|an|den|det|en|et)\s+", "", s)
    return s


def _titles_match(title_a: str, title_b: str) -> bool:
    return _normalise(title_a) == _normalise(title_b)


def _titles_match_fuzzy(title_a: str, title_b: str) -> bool:
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
        return {"status": STATUS_ERROR, "message": f"Forbindelsesfejl: {e}"}

    if not plex_username:
        return admin_plex

    try:
        account = admin_plex.myPlexAccount()
        norm    = plex_username.strip().lower()

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
    info = {"resolution": None, "hdr": False, "codec": None, "audio": None, "channels": None}
    try:
        for media in getattr(media_item, "media", []):
            for part in getattr(media, "parts", []):
                for stream in getattr(part, "streams", []):
                    if getattr(stream, "streamType", None) == 1:
                        info["resolution"] = getattr(stream, "displayTitle", None)
                        info["codec"]      = getattr(stream, "codec", None)
                        color_trc = getattr(stream, "colorTrc", "") or ""
                        info["hdr"] = "pq" in color_trc.lower() or "hlg" in color_trc.lower()
                    if getattr(stream, "streamType", None) == 2 and not info["audio"]:
                        info["audio"]    = getattr(stream, "codec", None)
                        info["channels"] = getattr(stream, "channels", None)
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
    Krydstjek en skuespillers top 20 film mod Plex.
    Flow: TMDB person → filmografi → Plex GUID-match → fuzzy fallback.
    """
    from services.tmdb_service import search_person, get_person_filmography

    # Trin 1: find person på TMDB
    person_results = await search_person(actor_name)
    if not person_results:
        return {"status": "not_found", "message": f"Ingen person fundet for '{actor_name}'."}

    person      = person_results[0]
    person_id   = person.get("id")
    actor_display = person.get("name", actor_name)

    # Trin 2: hent filmografi
    filmography = await get_person_filmography(person_id)
    top_movies  = (filmography.get("movies") or [])[:20]

    if not top_movies:
        return {"status": "ok", "actor": actor_display, "found_on_plex": [],
                "found_count": 0, "checked_top_n": 0}

    # Trin 3+4: hent Plex actor-resultater og byg GUID-indeks
    plex_result = await asyncio.to_thread(
        partial(_actor_sync, actor_name=actor_display, plex_username=plex_username)
    )
    plex_actor_titles = {
        _clean_title(item.get("title", ""))
        for item in plex_result.get("found", [])
    }

    plex_tmdb_ids: set[int] = set()
    plex_imdb_ids: set[str] = set()
    if plex_result.get("found"):
        plex_tmdb_ids, plex_imdb_ids = await asyncio.to_thread(
            partial(_build_actor_guid_set, actor_name=actor_display,
                    plex_username=plex_username)
        )

    logger.info(
        "check_actor_on_plex: Plex har %d titler med '%s' (%d via GUID, %d via titel)",
        len(plex_result.get("found", [])), actor_display,
        len(plex_tmdb_ids), len(plex_actor_titles),
    )

    # Trin 5: kryds-tjek top-20
    found_on_plex: list[dict] = []

    for movie in top_movies:
        tmdb_id        = movie.get("tmdb_id")
        title          = movie.get("title", "")
        original_title = movie.get("original_title", "") or title
        release        = movie.get("release_date", "")
        year           = int(release[:4]) if release and len(release) >= 4 and release[:4].isdigit() else None

        if tmdb_id and tmdb_id in plex_tmdb_ids:
            found_on_plex.append({"title": title, "year": year,
                                   "character": movie.get("character"), "tmdb_id": tmdb_id})
            continue

        imdb_id = movie.get("imdb_id") or ""
        if imdb_id and imdb_id in plex_imdb_ids:
            logger.info("IMDb GUID-match: '%s' → fundet via %s", title, imdb_id)
            found_on_plex.append({"title": title, "year": year,
                                   "character": movie.get("character"), "tmdb_id": tmdb_id})
            continue

        clean_tmdb          = _clean_title(title)
        clean_tmdb_original = _clean_title(original_title)
        titles_differ       = original_title != title

        fuzzy_hit = clean_tmdb in plex_actor_titles or (
            titles_differ and clean_tmdb_original in plex_actor_titles
        )

        if not fuzzy_hit:
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
            found_on_plex.append({"title": title, "year": year,
                                   "character": movie.get("character"), "tmdb_id": tmdb_id})

    found_count   = len(found_on_plex)
    missing_count = len(top_movies) - found_count

    logger.info(
        "check_actor_on_plex '%s': %d/%d fundet på Plex, %d ikke fundet i top-%d",
        actor_display, found_count, len(top_movies), missing_count, len(top_movies),
    )

    return {
        "status":         "ok",
        "actor":          actor_display,
        "tmdb_person_id": person_id,
        "found_on_plex":  found_on_plex,
        "found_count":    found_count,
        "checked_top_n":  len(top_movies),
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


async def validate_plex_user(plex_username: str) -> dict:
    try:
        return await asyncio.to_thread(
            partial(_validate_user_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("validate_plex_user error: %s", e)
        return {"valid": False, "message": str(e)}


async def add_to_watchlist(
    title: str,
    plex_username: str | None = None,
) -> bool:
    """Tilføj en titel til Plex Watchlist. Returnerer True ved success."""
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
        "guid":           f"tmdb://{tmdb_id}",
        "type":           plex_type_int,
        "X-Plex-Token":   PLEX_TOKEN,
    }
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            items = (
                data.get("MediaContainer", {})
                    .get("Metadata", [])
            )
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


# ══════════════════════════════════════════════════════════════════════════════
# SYNC IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════════════

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


def _check_sync(
    title: str,
    year: int | None,
    media_type: str,
    plex_username: str | None = None,
    tmdb_id: int | None = None,
) -> dict:
    """
    Tre lag:
      Lag 0 (GUID): Scan hele sektionen og match via TMDB GUID.
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
        # Lag 0: GUID-match
        if tmdb_id:
            try:
                for item in section.search():
                    if _extract_tmdb_id_from_guids(item) == tmdb_id:
                        return _build_result(item, "0/GUID")
            except Exception as e:
                logger.warning("GUID scan fejl i '%s': %s", section.title, e)

        # Lag 1+2/3: titel-søgning
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

            return _build_result(item, match_lag)

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


# ── Genre-synonym mapping ─────────────────────────────────────────────────────
# Plex gemmer genre-tags på blandet dansk/engelsk afhængig af biblioteksindstilling.
# Claude sender altid engelske genre-navne fra system-promptens leksikon.
# Denne mapping udvider søgningen til at matche begge sprog.
# Nøgle: normaliseret engelsk genre-term → liste af normaliserede synonymer der også matches.
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
    "scifi":       ["sciencefiction", "sci fi", "scifi"],
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
    Bruger substring-check så 'crime' matcher 'crime drama' osv.
    """
    # Byg sæt af alle genre-termer der skal matches (input + synonymer)
    search_terms = {norm_genre}
    search_terms.update(_GENRE_SYNONYMS.get(norm_genre, []))

    for item_genre in item_genre_tags:
        for term in search_terms:
            if term in item_genre:
                return True
    return False


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
    plex_type  = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE
    plex       = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    unwatched  = []
    norm_genre = _normalise(genre) if genre else None

    for section in _sections(plex, plex_type):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning(
                "find_unwatched: section.all() fejlede i '%s': %s", section.title, e
            )
            continue

        for item in all_items:
            if getattr(item, "viewCount", 0) > 0:
                continue
            if norm_genre:
                item_genres = [_normalise(g.tag) for g in getattr(item, "genres", [])]
                if not _genre_matches(norm_genre, item_genres):
                    continue
            unwatched.append(_slim(item))

    logger.info(
        "find_unwatched: fandt %d usete %s%s",
        len(unwatched), media_type,
        f" (genre={genre})" if genre else "",
    )
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

    found_in_plex     = []
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
        "status":           "ok",
        "collection":       collection_name,
        "found_in_plex":    found_in_plex[:_MAX_RESULTS],
        "missing_from_plex": missing_from_plex[:_MAX_RESULTS],
        "total_checked":    len(relevant),
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