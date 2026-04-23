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
_MAX_RESULTS = 25      # Hard cap on all list responses sent to the AI.

_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_LANG = "da-DK"


# ── Lightweight item serialiser ───────────────────────────────────────────────

def _slim(item) -> dict:
    """
    Convert a PlexAPI media object to a minimal dict for the AI.

    Keeps ONLY: title, year, rating, genres (max 3), summary.
    Everything else — file paths, codecs, artwork URLs, GUIDs — is dropped.
    This is the single choke-point for token reduction in Plex responses.
    """
    genres = [g.tag for g in getattr(item, "genres", [])][:3]
    summary = (getattr(item, "summary", "") or "").strip()
    # Truncate long summaries — the AI doesn't need the full synopsis.
    if len(summary) > 200:
        summary = summary[:197] + "…"

    return {
        "title":   getattr(item, "title", "Ukendt"),
        "year":    getattr(item, "year", None),
        "rating":  getattr(item, "audienceRating", None),
        "genres":  genres,
        "summary": summary or None,
    }


# ── Title normalisation ───────────────────────────────────────────────────────

def _normalise(title: str) -> str:
    """
    Normalise a title for fuzzy comparison.
    Strips accents, lowercases, replaces hyphens, removes punctuation,
    collapses spaces, and strips leading articles.
    """
    nfkd = unicodedata.normalize("NFKD", title)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    s = ascii_str.lower()
    s = re.sub(r"[-_]", " ", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(the|a|an|den|det|en|et)\s+", "", s)
    return s


def _titles_match(title_a: str, title_b: str) -> bool:
    return _normalise(title_a) == _normalise(title_b)


# ── Plex connection helper ────────────────────────────────────────────────────

def _connect(plex_username: str | None = None) -> PlexServer | dict:
    """
    Return a user-scoped PlexServer connection.

    Priority order:
      1. Server owner      → use admin token directly
      2. Shared friend     → get token via account.user().get_token()
      3. Managed home user → get token via switchHomeUser()
      4. No match          → fall back to admin token with warning
    """
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

        # ── 1. Server owner ───────────────────────────────────────────────────
        owner_names = {
            (account.username or "").lower(),
            (account.email or "").lower(),
            (account.title or "").lower(),
        }
        if norm in owner_names:
            logger.debug("Plex connect: owner '%s'", plex_username)
            return admin_plex

        # ── 2. Shared friend (has own Plex account) ───────────────────────────
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
                    logger.debug("Plex connect: shared friend '%s'", plex_username)
                    return PlexServer(PLEX_URL, user_token, timeout=15)
                except Exception as e:
                    logger.warning(
                        "get_token() failed for shared user '%s': %s — trying switchHomeUser",
                        plex_username, e,
                    )

        # ── 3. Managed home user (no own Plex account) ───────────────────────
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
                    logger.debug("Plex connect: managed home user '%s'", plex_username)
                    return PlexServer(PLEX_URL, switched.authToken, timeout=15)
                except Exception as e:
                    logger.warning(
                        "switchHomeUser() failed for '%s': %s — falling back to admin",
                        plex_username, e,
                    )
                    return admin_plex

        # ── 4. No match ───────────────────────────────────────────────────────
        logger.warning(
            "Plex user '%s' not found in friends or home users — falling back to admin",
            plex_username,
        )
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
    """Search a section by first normalised word; return empty list on error."""
    try:
        first_word = _normalise(title).split()[0] if _normalise(title).split() else title
        return section.search(title=first_word)
    except Exception as e:
        logger.warning("Search error in '%s': %s", section.title, e)
        return []


# ── Technical specs helper (only used by get_plex_metadata) ──────────────────

def _stream_info(media_item) -> dict:
    """
    Extract technical specs from a Plex media item.
    NEVER returns raw file paths or directory names.
    Only called by _metadata_sync — never included in list responses.
    """
    info = {
        "resolution": None,
        "hdr": False,
        "video_codec": None,
        "audio_codec": None,
        "audio_channels": None,
        "bitrate_kbps": None,
        "container": None,
        "duration_minutes": None,
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
                if stype == 1:  # video
                    info["video_codec"] = getattr(stream, "codec", None)
                    color_trc = getattr(stream, "colorTrc", "") or ""
                    dv = getattr(stream, "DOVIPresent", False)
                    info["hdr"] = bool(
                        dv
                        or "smpte2084" in color_trc.lower()
                        or "arib-std-b67" in color_trc.lower()
                    )
                elif stype == 2 and not info["audio_codec"]:  # first audio track
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
    """Check whether a specific title exists in Plex."""
    try:
        return await asyncio.to_thread(
            partial(_check_sync, title=title, year=year, media_type=media_type, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("check_library error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_collection(
    keyword: str,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    """Search Plex for all titles matching a keyword. Capped at 25 results."""
    try:
        return await asyncio.to_thread(
            partial(_collection_sync, keyword=keyword, media_type=media_type, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_collection error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_on_deck(plex_username: str | None = None) -> dict:
    """Return the user's 'Continue Watching' list (up to 8 items)."""
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
    """Return technical specs for a title — never raw file paths."""
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
    """Return up to 6 random unwatched titles, optionally filtered by genre."""
    try:
        return await asyncio.to_thread(
            partial(_unwatched_sync, media_type=media_type, genre=genre, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("find_unwatched error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


async def get_similar_in_library(
    title: str,
    plex_username: str | None = None,
) -> dict:
    """Find titles in the Plex library similar to the given title."""
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
    """
    Compare a Plex collection against TMDB to find missing titles.
    Uses TMDB search to find the collection, then checks each title against Plex.
    """
    try:
        return await asyncio.to_thread(
            partial(_missing_sync, collection_name=collection_name, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_missing_from_collection error: %s", e)
        return {"status": STATUS_ERROR, "message": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SYNC IMPLEMENTATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _check_sync(
    title: str,
    year: int | None,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    """check_library — returns minimal match info, no heavy metadata."""
    plex_type = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    for section in _sections(plex, plex_type):
        for item in _safe_search(section, title):
            item_title = getattr(item, "title", "") or ""
            item_year  = getattr(item, "year", None)
            if not _titles_match(item_title, title):
                continue
            if year and item_year and abs(item_year - year) > 1:
                continue
            logger.info("Plex HIT: '%s' (%s) in '%s'", item_title, item_year, section.title)
            # Return only what the AI needs to confirm the title exists.
            return {"status": STATUS_FOUND, "title": item_title, "year": item_year}

    return {"status": STATUS_MISSING}


def _collection_sync(
    keyword: str,
    media_type: str,
    plex_username: str | None = None,
) -> dict:
    """get_collection — returns slim items, capped at _MAX_RESULTS."""
    plex_type = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    norm_keyword = _normalise(keyword)
    matches = []

    for section in _sections(plex, plex_type):
        for item in _safe_search(section, keyword):
            item_title = getattr(item, "title", "") or ""
            if norm_keyword.split()[0] in _normalise(item_title):
                matches.append(_slim(item))

    matches.sort(key=lambda x: x.get("year") or 0)
    capped = matches[:_MAX_RESULTS]
    return {"status": "ok", "found": capped, "count": len(capped)}


def _on_deck_sync(plex_username: str | None = None) -> dict:
    """get_on_deck — slim items only, episodes get show/season/episode fields."""
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
    """
    get_plex_metadata — the ONE function that returns technical specs.
    Never called as part of list responses.
    """
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    for plex_type in (_MOVIE_TYPE, _TV_TYPE):
        for section in _sections(plex, plex_type):
            for item in _safe_search(section, title):
                item_title = getattr(item, "title", "") or ""
                item_year  = getattr(item, "year", None)
                if not _titles_match(item_title, title):
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
    """find_unwatched — slim items, capped at 6 random picks."""
    plex_type = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    unwatched = []
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
    suggestions = unwatched[:6]
    return {"status": "ok", "suggestions": suggestions, "total_unwatched": len(unwatched)}


def _similar_sync(title: str, plex_username: str | None = None) -> dict:
    """get_similar_in_library — slim items, capped at _MAX_RESULTS."""
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    source_item    = None
    source_section = None

    for plex_type in (_MOVIE_TYPE, _TV_TYPE):
        for section in _sections(plex, plex_type):
            for item in _safe_search(section, title):
                if _titles_match(getattr(item, "title", ""), title):
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

    # Fallback: genre overlap within the same section.
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
    """
    get_missing_from_collection — find gaps between TMDB and Plex.
    Returns only title/year/tmdb_id — no heavy metadata.
    """
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

    norm_kw = _normalise(collection_name).split()[0]
    relevant = [
        r for r in tmdb_results
        if norm_kw in _normalise(r.get("title") or r.get("original_title") or "")
    ]
    if not relevant:
        relevant = tmdb_results[:15]

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return plex

    found_in_plex     = []
    missing_from_plex = []

    for movie in relevant:
        tmdb_title = movie.get("title") or movie.get("original_title") or ""
        tmdb_year  = int((movie.get("release_date") or "0")[:4] or 0) or None

        in_plex = False
        for section in _sections(plex, _MOVIE_TYPE):
            for item in _safe_search(section, tmdb_title):
                item_title = getattr(item, "title", "") or ""
                item_year  = getattr(item, "year", None)
                if not _titles_match(item_title, tmdb_title):
                    continue
                if tmdb_year and item_year and abs(item_year - tmdb_year) > 1:
                    continue
                in_plex = True
                break
            if in_plex:
                break

        # Only title/year/tmdb_id — the AI doesn't need anything else.
        entry = {"title": tmdb_title, "year": tmdb_year, "tmdb_id": movie.get("id")}
        if in_plex:
            found_in_plex.append(entry)
        else:
            missing_from_plex.append(entry)

    return {
        "status": "ok",
        "collection": collection_name,
        "found_in_plex":     found_in_plex[:_MAX_RESULTS],
        "missing_from_plex": missing_from_plex[:_MAX_RESULTS],
        "total_checked": len(relevant),
    }


# ── Plex user validation ──────────────────────────────────────────────────────

async def validate_plex_user(plex_username: str) -> dict:
    """
    Check whether a Plex username exists as a shared user on this server.
    Works for both the server owner and managed/shared users.
    """
    try:
        return await asyncio.to_thread(
            partial(_validate_user_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("validate_plex_user error: %s", e)
        return {"valid": False, "message": str(e)}


def _validate_user_sync(plex_username: str) -> dict:
    """Synchronous Plex user validation — runs in thread pool."""
    try:
        admin_plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
        account    = admin_plex.myPlexAccount()
    except Exception as e:
        return {"valid": False, "message": f"Forbindelsesfejl: {e}"}

    norm = plex_username.strip().lower()

    # 1. Server owner
    owner_names = {
        (account.username or "").lower(),
        (account.email or "").lower(),
        (account.title or "").lower(),
    }
    if norm in owner_names:
        display = account.title or account.username or plex_username
        return {"valid": True, "username": display, "user_type": "owner"}

    # 2. Shared friends
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

    # 3. Managed home users
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
        "valid": False,
        "message": f"Brugernavnet '{plex_username}' blev ikke fundet — hverken som ven eller hjembruger.",
    }


async def get_plex_for_user(plex_username: str):
    """Return a PlexServer instance scoped to a specific user."""
    try:
        return await asyncio.to_thread(
            partial(_get_user_server_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("get_plex_for_user error: %s", e)
        return None


def _get_user_server_sync(plex_username: str):
    """Return a user-scoped PlexServer — runs in thread pool."""
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