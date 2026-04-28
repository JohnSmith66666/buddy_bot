"""
services/plex_cache.py - In-memory cache for Plex-indekser med TTL.

CHANGES (v0.3.0 — TV-index tilføjet):
  - NY: get_plex_tv_index() async + get_plex_tv_index_sync() per-username.
    Samme TTL-arkitektur (60min cache, lock-protected build) som movie-indeks.
  - NY: _build_plex_tv_index_sync() bygger {tmdb_id → PlexItem} for TV-shows
    via section.all() + GUID-extract (samme pattern som movie-indeks).
  - REFAKTOR: Cache-strukturen er nu en tuple per (username, media_type) i
    stedet for kun (username) — så film og TV cacher uafhængigt af hinanden.
  - BAGUDKOMPATIBILITET: get_plex_movie_index() + get_plex_movie_index_sync()
    bevarer samme API. Eksisterende kaldere brækker ikke.

UNCHANGED (v0.2.0 — multi-cache):
  - 60 min TTL per index — match vores generelle data-friskhedsbehov.
  - Lock-protected build for at undgå thundering herd ved cold start.
  - Per-username cache (admin har separat cache fra hver Plex Home User).

UNCHANGED (v0.1.0 — initial cache):
  - get_plex_movie_index() returnerer {tmdb_id → PlexItem} dict.
  - Bygges via section.all() + GUID-extract (pålideligt — section.search(guid=..)
    fejler intermittent for nogle items).

DESIGN-PRINCIPPER:
  - Cache er process-local (ikke delt på tværs af containers).
  - Bygges lazily ved første kald, derefter genbruges i 60 min.
  - Lock sikrer at parallelle kald ikke bygger samme cache flere gange.
  - Sync-versionen bruges af _check_sync (i plex_service) som ikke kører i async-context.
"""

import asyncio
import logging
import time
from threading import Lock

from services.plex_service import (
    _MOVIE_TYPE,
    _TV_TYPE,
    _connect,
    _extract_tmdb_id_from_guids,
    _sections,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Cache-konfiguration
# ══════════════════════════════════════════════════════════════════════════════

# Time-To-Live for index-cache (60 min)
_CACHE_TTL_SECS = 60 * 60

# Cache-key format: (plex_username_or_None, media_type) → (timestamp, index_dict)
# media_type ∈ {"movie", "tv"}
_index_cache: dict[tuple, tuple[float, dict]] = {}

# Async lock per (username, media_type) — undgår thundering herd ved cold start
_async_locks: dict[tuple, asyncio.Lock] = {}

# Sync lock for thread-safe build fra _check_sync
_sync_lock = Lock()


# ══════════════════════════════════════════════════════════════════════════════
# Public API — Movie index (async + sync)
# ══════════════════════════════════════════════════════════════════════════════

async def get_plex_movie_index(plex_username: str | None = None) -> dict[int, object]:
    """
    Returnér cached {tmdb_id → PlexItem} index for film.

    Bygger cachen ved første kald (cold). Subsequent kald inden for TTL
    returnerer cached data (~1ms response time).

    Args:
      plex_username: Plex-brugernavn. None = admin (default).

    Returns:
      Dict mapping tmdb_id (int) til PlexItem-objekter.
      Tom dict hvis Plex-fejl eller intet bibliotek.
    """
    return await _get_index_async(plex_username, media_type="movie")


def get_plex_movie_index_sync(plex_username: str | None = None) -> dict[int, object]:
    """
    Synkron version af get_plex_movie_index for brug i sync-kontekst.

    Bruges fra _check_sync (i plex_service) som kører i thread pool og
    ikke har adgang til async event loop.

    Returns:
      Dict mapping tmdb_id (int) til PlexItem-objekter.
    """
    return _get_index_sync(plex_username, media_type="movie")


# ══════════════════════════════════════════════════════════════════════════════
# Public API — TV index (NY i v0.3.0)
# ══════════════════════════════════════════════════════════════════════════════

async def get_plex_tv_index(plex_username: str | None = None) -> dict[int, object]:
    """
    Returnér cached {tmdb_id → PlexItem} index for TV-shows.

    Bygger cachen ved første kald (cold). Subsequent kald inden for TTL
    returnerer cached data.

    Args:
      plex_username: Plex-brugernavn. None = admin (default).

    Returns:
      Dict mapping tmdb_id (int) til PlexItem-objekter (TV-shows).

    Bemærk: For TV-shows er PlexItem.viewCount summen af afsnit set i hele
    showet. En serie er "uset" når viewCount == 0 (intet afsnit set).
    """
    return await _get_index_async(plex_username, media_type="tv")


def get_plex_tv_index_sync(plex_username: str | None = None) -> dict[int, object]:
    """Synkron version af get_plex_tv_index for brug i sync-kontekst."""
    return _get_index_sync(plex_username, media_type="tv")


# ══════════════════════════════════════════════════════════════════════════════
# Cache invalidation (manual purge)
# ══════════════════════════════════════════════════════════════════════════════

def invalidate_cache(
    plex_username: str | None = None,
    media_type: str | None = None,
) -> int:
    """
    Manuelt purge cached index.

    Args:
      plex_username: Hvis angivet, kun for denne bruger. None = alle brugere.
      media_type:    Hvis angivet ('movie' eller 'tv'), kun for denne type.
                     None = både film og TV.

    Returns:
      Antal cache-entries der blev fjernet.
    """
    keys_to_remove = []

    for key in list(_index_cache.keys()):
        cached_user, cached_media = key

        if plex_username is not None and cached_user != plex_username:
            continue
        if media_type is not None and cached_media != media_type:
            continue

        keys_to_remove.append(key)

    for key in keys_to_remove:
        _index_cache.pop(key, None)
        _async_locks.pop(key, None)

    if keys_to_remove:
        logger.info(
            "plex_cache: invalidated %d entries (user='%s', media='%s')",
            len(keys_to_remove), plex_username or "ALL", media_type or "ALL",
        )

    return len(keys_to_remove)


# ══════════════════════════════════════════════════════════════════════════════
# Internal — async cache-get with build-on-miss
# ══════════════════════════════════════════════════════════════════════════════

async def _get_index_async(
    plex_username: str | None,
    media_type: str,
) -> dict[int, object]:
    """
    Async version: returnér cached index, byg hvis cold/expired.

    Lock-protected for at undgå thundering herd: hvis 5 parallelle kald
    rammer cold cache, kun 1 bygger — de øvrige venter på resultatet.
    """
    cache_key = (plex_username, media_type)
    now = time.time()

    # Quick-path: warm cache hit
    cached = _index_cache.get(cache_key)
    if cached is not None:
        ts, index = cached
        age = now - ts
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "plex_cache HIT (%s/%s): %d items, age=%.0fs",
                plex_username or "admin", media_type, len(index), age,
            )
            return index

    # Cold or expired: acquire lock to avoid duplicate build
    if cache_key not in _async_locks:
        _async_locks[cache_key] = asyncio.Lock()

    async with _async_locks[cache_key]:
        # Double-check inside lock (another coroutine may have built it)
        cached = _index_cache.get(cache_key)
        if cached is not None:
            ts, index = cached
            if time.time() - ts < _CACHE_TTL_SECS:
                return index

        # Build fresh
        logger.info(
            "plex_cache COLD (%s/%s) — building index...",
            plex_username or "admin", media_type,
        )

        try:
            index = await asyncio.to_thread(
                _build_index_sync, plex_username, media_type,
            )
        except Exception as e:
            logger.error(
                "plex_cache build fejl (%s/%s): %s",
                plex_username or "admin", media_type, e,
            )
            return {}

        _index_cache[cache_key] = (time.time(), index)

        logger.info(
            "plex_cache BUILT (%s/%s): %d items",
            plex_username or "admin", media_type, len(index),
        )

        return index


# ══════════════════════════════════════════════════════════════════════════════
# Internal — sync cache-get with build-on-miss
# ══════════════════════════════════════════════════════════════════════════════

def _get_index_sync(
    plex_username: str | None,
    media_type: str,
) -> dict[int, object]:
    """
    Sync version af _get_index_async.

    Bruges fra _check_sync der kører i thread pool og ikke har async context.
    Thread-safe via _sync_lock.
    """
    cache_key = (plex_username, media_type)
    now = time.time()

    # Quick-path: warm cache hit (uden lock for hastighed)
    cached = _index_cache.get(cache_key)
    if cached is not None:
        ts, index = cached
        age = now - ts
        if age < _CACHE_TTL_SECS:
            return index

    # Cold or expired: acquire lock
    with _sync_lock:
        # Double-check inside lock
        cached = _index_cache.get(cache_key)
        if cached is not None:
            ts, index = cached
            if time.time() - ts < _CACHE_TTL_SECS:
                return index

        # Build fresh
        logger.info(
            "plex_cache COLD-SYNC (%s/%s) — building index...",
            plex_username or "admin", media_type,
        )

        try:
            index = _build_index_sync(plex_username, media_type)
        except Exception as e:
            logger.error(
                "plex_cache build-sync fejl (%s/%s): %s",
                plex_username or "admin", media_type, e,
            )
            return {}

        _index_cache[cache_key] = (time.time(), index)

        logger.info(
            "plex_cache BUILT-SYNC (%s/%s): %d items",
            plex_username or "admin", media_type, len(index),
        )

        return index


# ══════════════════════════════════════════════════════════════════════════════
# Internal — index builder (delt mellem film og TV)
# ══════════════════════════════════════════════════════════════════════════════

def _build_index_sync(
    plex_username: str | None,
    media_type: str,
) -> dict[int, object]:
    """
    Byg {tmdb_id → PlexItem} index ved at scanne alle sektioner af det rigtige type.

    Args:
      plex_username: Plex-bruger eller None for admin.
      media_type:    'movie' eller 'tv'.

    Returns:
      Dict mapping tmdb_id → PlexItem (uden TMDB ID skipped).

    NB: Vi bruger section.all() + manuel GUID-extract i stedet for
    section.search(guid=...) fordi den server-side filter har vist sig at
    fejle intermittent for ~5% af items.
    """
    if media_type not in ("movie", "tv"):
        raise ValueError(f"Ugyldig media_type: '{media_type}'")

    plex_type = _MOVIE_TYPE if media_type == "movie" else _TV_TYPE

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        # _connect returnerede error-dict
        logger.warning(
            "_build_index_sync: kunne ikke connecte til Plex for '%s': %s",
            plex_username or "admin", plex,
        )
        return {}

    sections = _sections(plex, plex_type)
    if not sections:
        logger.warning(
            "_build_index_sync: ingen %s-sektioner fundet for '%s'",
            media_type, plex_username or "admin",
        )
        return {}

    index: dict[int, object] = {}
    total_items = 0
    items_with_tmdb = 0

    for section in sections:
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning(
                "_build_index_sync: section.all() fejl i '%s': %s",
                section.title, e,
            )
            continue

        for item in all_items:
            total_items += 1
            tmdb_id = _extract_tmdb_id_from_guids(item)
            if tmdb_id is not None:
                items_with_tmdb += 1
                # Ved duplikater (samme TMDB ID i flere sektioner) bruges første
                if tmdb_id not in index:
                    index[tmdb_id] = item

    logger.info(
        "_build_index_sync (%s/%s): %d items scannet, %d har TMDB ID, %d unique",
        plex_username or "admin", media_type,
        total_items, items_with_tmdb, len(index),
    )

    return index