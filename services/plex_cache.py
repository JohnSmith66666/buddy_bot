"""
services/plex_cache.py - Per-bruger TTL-cache for Plex-opslag.

CHANGES (v0.2.0 — udvidet cache-arkitektur):
  - TTL ændret fra 5 min → 60 min for ALLE caches.
    Begrundelse: Botten bruges af enkeltperson + få familie/venner. Realtids-
    friskhed er mindre vigtig end konsistent hurtighed. 60 min undgår de
    fleste cold-starts i løbet af en dag, og nyt indhold (via Radarr/Sonarr)
    bliver synligt indenfor maks 1 time.

  - 4 NYE CACHE-TYPER:
    * TV serie index ({tmdb_id: PlexItem}) — komplementerer movie index.
      Konsekvens: _check_sync har nu Lag 0 cache for BÅDE film og TV.
    * Actor cache ({tmdb_ids, imdb_ids}) — per (actor, user).
      Sparer 2-3 sek per skuespiller-tjek (det største flaskehals-tool).
    * Genre cache (list[PlexItem]) — per (media_type, genre, user).
      Bruges af find_unwatched for hurtige genre-baserede anbefalinger.
    * OnDeck cache (list[PlexItem]) — per bruger.
      Bruges af get_on_deck der ellers gør et fuldt API-kald hver gang.

  - SAMME ARKITEKTUR FOR ALLE CACHES:
    * Per-bruger isolation (admin, SKYNET, andre Home Users)
    * Async lock per cache-key forhindrer race conditions ved parallelle kald
    * Sync variant tilgængelig hvor det gavner _check_sync
    * Fallback-graceful: returnér tom dict ved fejl, lad caller fallbacke

UNCHANGED (v0.1.0 — initial implementation):
  - get_plex_movie_index() async + sync varianter — uændret API
  - invalidate_plex_cache() — udvidet til at rydde alle cache-typer
  - get_cache_stats() — udvidet med stats for alle 5 cache-typer

DESIGN-PRINCIPPER:
  - Per-bruger: hver bruger har sin egen cache-instans
  - 60 min TTL: konsistent tid for alle cache-typer
  - Async lock: ingen duplikeret arbejde ved samtidige kald
  - Lazy: cache bygges kun ved første kald per bruger per type
  - Self-cleanup: udløbne entries fjernes automatisk ved næste GET
  - Robusthed: cache-fejl returnerer tom data, så caller kan fallbacke

PERFORMANCE PER CACHE-TYPE:
  - Movie index:  cold ~1-2s (6.500 film), warm ~1ms
  - TV index:     cold ~500ms (1.150 serier), warm ~1ms
  - Actor cache:  cold ~2-3s (sektion.search), warm ~1ms
  - Genre cache:  cold ~1-2s (section.all + filter), warm ~1ms
  - OnDeck:       cold ~300ms, warm ~1ms

RAM-FORBRUG (worst case, 50 brugere):
  - Movie indexes:  50 × 5 MB = 250 MB
  - TV indexes:     50 × 1 MB = 50 MB
  - Actor caches:   500 unikke skuespillere × 5 KB = 2.5 MB
  - Genre caches:   50 × 30 genrer × 100 KB = 150 MB
  - OnDeck:         50 × 50 KB = 2.5 MB
  - Total max:      ~455 MB (5.7% af 8 GB RAM)
  - Realistisk:     ~50 MB (5 daglige brugere)

USAGE:
  from services.plex_cache import (
      get_plex_movie_index,
      get_plex_tv_index,
      get_actor_index,
      get_unwatched_by_genre,
      get_on_deck_cached,
  )

  movies = await get_plex_movie_index(plex_username)
  tv     = await get_plex_tv_index(plex_username)
  films  = await get_actor_index("Tom Hanks", plex_username)
  drama  = await get_unwatched_by_genre("movie", "drama", plex_username)
  deck   = await get_on_deck_cached(plex_username)
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Cache-konstanter
# ══════════════════════════════════════════════════════════════════════════════

_CACHE_TTL_SECS = 3600  # 60 minutter (v0.2.0 — var 300 / 5 min)

# Special key for None plex_username (bruger admin-token)
_ADMIN_KEY = "__admin__"


# ══════════════════════════════════════════════════════════════════════════════
# Cache state (per-process, nulstilles ved Railway redeploy)
# ══════════════════════════════════════════════════════════════════════════════

# Movie index: {user_key: (built_at, {tmdb_id: PlexItem})}
_movie_cache: dict[str, tuple[float, dict[int, object]]] = {}
_movie_locks: dict[str, asyncio.Lock] = {}

# TV index: {user_key: (built_at, {tmdb_id: PlexItem})}
_tv_cache: dict[str, tuple[float, dict[int, object]]] = {}
_tv_locks: dict[str, asyncio.Lock] = {}

# Actor cache: {(actor_normalized, user_key): (built_at, (tmdb_ids, imdb_ids))}
_actor_cache: dict[tuple[str, str], tuple[float, tuple[set[int], set[str]]]] = {}
_actor_locks: dict[tuple[str, str], asyncio.Lock] = {}

# Genre cache: {(media_type, genre_normalized, user_key): (built_at, list[PlexItem])}
_genre_cache: dict[tuple[str, str, str], tuple[float, list[object]]] = {}
_genre_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

# OnDeck cache: {user_key: (built_at, list[PlexItem])}
_on_deck_cache: dict[str, tuple[float, list[object]]] = {}
_on_deck_locks: dict[str, asyncio.Lock] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Public API — Movie index (uændret fra v0.1.0)
# ══════════════════════════════════════════════════════════════════════════════

async def get_plex_movie_index(
    plex_username: str | None = None,
) -> dict[int, object]:
    """
    Hent {tmdb_id: PlexItem} index for film med 60-min TTL-cache.
    Returnerer tom dict ved fejl — caller skal fallbacke.
    """
    cache_key = _make_key(plex_username)

    cached = _movie_cache.get(cache_key)
    if cached is not None:
        built_at, index = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "movie_cache HIT for '%s' (age=%.0fs, %d film)",
                plex_username or "admin", age, len(index),
            )
            return index
        logger.info(
            "movie_cache TTL udløbet for '%s' (age=%.0fs) — rebuilds",
            plex_username or "admin", age,
        )
        _movie_cache.pop(cache_key, None)

    lock = _movie_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _movie_cache.get(cache_key)
        if cached is not None:
            built_at, index = cached
            if time.time() - built_at < _CACHE_TTL_SECS:
                logger.debug("movie_cache HIT efter lock-wait for '%s'", plex_username or "admin")
                return index

        index = await _build_index_async(plex_username, media_type="movie")
        _movie_cache[cache_key] = (time.time(), index)

        logger.info(
            "movie_cache rebuilt for '%s': %d film med TMDB GUID",
            plex_username or "admin", len(index),
        )
        return index


def get_plex_movie_index_sync(
    plex_username: str | None = None,
) -> dict[int, object]:
    """Synkron variant — bruges fra _check_sync der allerede kører i thread pool."""
    cache_key = _make_key(plex_username)

    cached = _movie_cache.get(cache_key)
    if cached is not None:
        built_at, index = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "movie_cache HIT (sync) for '%s' (age=%.0fs, %d film)",
                plex_username or "admin", age, len(index),
            )
            return index
        logger.info(
            "movie_cache TTL udløbet (sync) for '%s' (age=%.0fs) — rebuilds",
            plex_username or "admin", age,
        )
        _movie_cache.pop(cache_key, None)

    index = _build_index_sync(plex_username, media_type="movie")
    _movie_cache[cache_key] = (time.time(), index)

    logger.info(
        "movie_cache rebuilt (sync) for '%s': %d film med TMDB GUID",
        plex_username or "admin", len(index),
    )
    return index


# ══════════════════════════════════════════════════════════════════════════════
# Public API — TV serie index (NY i v0.2.0)
# ══════════════════════════════════════════════════════════════════════════════

async def get_plex_tv_index(
    plex_username: str | None = None,
) -> dict[int, object]:
    """
    Hent {tmdb_id: PlexItem} index for TV-serier med 60-min TTL-cache.
    Komplementerer movie_index for at give _check_sync hurtig path for serier.
    Returnerer tom dict ved fejl — caller skal fallbacke.
    """
    cache_key = _make_key(plex_username)

    cached = _tv_cache.get(cache_key)
    if cached is not None:
        built_at, index = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "tv_cache HIT for '%s' (age=%.0fs, %d serier)",
                plex_username or "admin", age, len(index),
            )
            return index
        logger.info(
            "tv_cache TTL udløbet for '%s' (age=%.0fs) — rebuilds",
            plex_username or "admin", age,
        )
        _tv_cache.pop(cache_key, None)

    lock = _tv_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _tv_cache.get(cache_key)
        if cached is not None:
            built_at, index = cached
            if time.time() - built_at < _CACHE_TTL_SECS:
                logger.debug("tv_cache HIT efter lock-wait for '%s'", plex_username or "admin")
                return index

        index = await _build_index_async(plex_username, media_type="tv")
        _tv_cache[cache_key] = (time.time(), index)

        logger.info(
            "tv_cache rebuilt for '%s': %d serier med TMDB GUID",
            plex_username or "admin", len(index),
        )
        return index


def get_plex_tv_index_sync(
    plex_username: str | None = None,
) -> dict[int, object]:
    """Synkron variant af get_plex_tv_index."""
    cache_key = _make_key(plex_username)

    cached = _tv_cache.get(cache_key)
    if cached is not None:
        built_at, index = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            return index
        logger.info(
            "tv_cache TTL udløbet (sync) for '%s' (age=%.0fs) — rebuilds",
            plex_username or "admin", age,
        )
        _tv_cache.pop(cache_key, None)

    index = _build_index_sync(plex_username, media_type="tv")
    _tv_cache[cache_key] = (time.time(), index)

    logger.info(
        "tv_cache rebuilt (sync) for '%s': %d serier med TMDB GUID",
        plex_username or "admin", len(index),
    )
    return index


# ══════════════════════════════════════════════════════════════════════════════
# Public API — Actor cache (NY i v0.2.0)
# ══════════════════════════════════════════════════════════════════════════════

async def get_actor_index(
    actor_name: str,
    plex_username: str | None = None,
) -> tuple[set[int], set[str]]:
    """
    Hent (tmdb_ids, imdb_ids) for alle Plex-film hvor en skuespiller medvirker.
    Cached i 60 min per (actor, user).

    Returns:
      Tuple af to sæt:
        - tmdb_ids: alle TMDB IDs for film hvor skuespilleren medvirker
        - imdb_ids: alle IMDb IDs for samme film
      Tomme sæt ved fejl — caller skal fallbacke.

    Performance:
      - Cold: ~2-3 sek (section.search(actor=...) per film-sektion)
      - Warm: ~1ms (dict lookup)

    Eksempel impact:
      Tom Hanks med 22 film: 2.5s cold → 1ms warm = 99.96% hurtigere
    """
    actor_key = _normalize_actor(actor_name)
    user_key  = _make_key(plex_username)
    cache_key = (actor_key, user_key)

    cached = _actor_cache.get(cache_key)
    if cached is not None:
        built_at, ids = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "actor_cache HIT for '%s'/'%s' (age=%.0fs, %d TMDB)",
                actor_name, plex_username or "admin", age, len(ids[0]),
            )
            return ids
        logger.info(
            "actor_cache TTL udløbet for '%s'/'%s' (age=%.0fs) — rebuilds",
            actor_name, plex_username or "admin", age,
        )
        _actor_cache.pop(cache_key, None)

    lock = _actor_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _actor_cache.get(cache_key)
        if cached is not None:
            built_at, ids = cached
            if time.time() - built_at < _CACHE_TTL_SECS:
                return ids

        ids = await _build_actor_index_async(actor_name, plex_username)
        _actor_cache[cache_key] = (time.time(), ids)

        logger.info(
            "actor_cache rebuilt for '%s'/'%s': %d TMDB IDs, %d IMDb IDs",
            actor_name, plex_username or "admin", len(ids[0]), len(ids[1]),
        )
        return ids


# ══════════════════════════════════════════════════════════════════════════════
# Public API — Genre cache (NY i v0.2.0)
# ══════════════════════════════════════════════════════════════════════════════

async def get_unwatched_by_genre(
    media_type: str,
    genre: str | None,
    plex_username: str | None = None,
) -> list[object]:
    """
    Hent liste af USETE PlexItems der matcher en genre.
    Cached i 60 min per (media_type, genre, user).

    Args:
      media_type: 'movie' eller 'tv'
      genre:      Genre-navn (None = alle usete)
      plex_username: Plex-username (None = admin)

    Returns:
      Liste af PlexItems hvor viewCount == 0 og som matcher genren.
      Tom liste ved fejl eller hvis ingen matches.

    Performance:
      - Cold: ~1-2 sek (section.all() + filter)
      - Warm: ~1ms (returnerer cached liste)

    Note:
      Cachen indeholder STADIG raw PlexItem-referencer. Det betyder at
      viewCount aflæses ved cache-build, så hvis brugeren ser en film
      i de næste 60 min vises den stadig som "uset" indtil cache udløber.
      Det er en bevidst trade-off for hastighed.
    """
    genre_key = _normalize_genre(genre)
    user_key  = _make_key(plex_username)
    cache_key = (media_type, genre_key, user_key)

    cached = _genre_cache.get(cache_key)
    if cached is not None:
        built_at, items = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "genre_cache HIT for '%s'/'%s'/'%s' (age=%.0fs, %d items)",
                media_type, genre or "all", plex_username or "admin", age, len(items),
            )
            return items
        logger.info(
            "genre_cache TTL udløbet for '%s'/'%s'/'%s' — rebuilds",
            media_type, genre or "all", plex_username or "admin",
        )
        _genre_cache.pop(cache_key, None)

    lock = _genre_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _genre_cache.get(cache_key)
        if cached is not None:
            built_at, items = cached
            if time.time() - built_at < _CACHE_TTL_SECS:
                return items

        items = await _build_genre_index_async(media_type, genre, plex_username)
        _genre_cache[cache_key] = (time.time(), items)

        logger.info(
            "genre_cache rebuilt for '%s'/'%s'/'%s': %d unwatched items",
            media_type, genre or "all", plex_username or "admin", len(items),
        )
        return items


# ══════════════════════════════════════════════════════════════════════════════
# Public API — OnDeck cache (NY i v0.2.0)
# ══════════════════════════════════════════════════════════════════════════════

async def get_on_deck_cached(
    plex_username: str | None = None,
) -> list[object]:
    """
    Hent OnDeck (fortsæt med at se) for brugeren.
    Cached i 60 min per bruger.

    Returns:
      Liste af PlexItems (top 10), tom liste ved fejl.

    Performance:
      - Cold: ~300ms
      - Warm: ~1ms

    Note:
      OnDeck ændrer sig sjældent — typisk kun når brugeren starter eller
      afslutter et nyt program. 60 min TTL er fint, brugeren oplever
      maksimal 1 time forsinkelse på "fortsæt med at se"-listen.
    """
    cache_key = _make_key(plex_username)

    cached = _on_deck_cache.get(cache_key)
    if cached is not None:
        built_at, items = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "on_deck_cache HIT for '%s' (age=%.0fs, %d items)",
                plex_username or "admin", age, len(items),
            )
            return items
        logger.info(
            "on_deck_cache TTL udløbet for '%s' — rebuilds",
            plex_username or "admin",
        )
        _on_deck_cache.pop(cache_key, None)

    lock = _on_deck_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _on_deck_cache.get(cache_key)
        if cached is not None:
            built_at, items = cached
            if time.time() - built_at < _CACHE_TTL_SECS:
                return items

        items = await _build_on_deck_async(plex_username)
        _on_deck_cache[cache_key] = (time.time(), items)

        logger.info(
            "on_deck_cache rebuilt for '%s': %d items",
            plex_username or "admin", len(items),
        )
        return items


# ══════════════════════════════════════════════════════════════════════════════
# Cache management
# ══════════════════════════════════════════════════════════════════════════════

def invalidate_plex_cache(plex_username: str | None = None) -> None:
    """
    Tving rebuild af ALLE cache-typer for en bruger ved næste GET.

    Bruges fx når:
      - En film tilføjes til Plex via Radarr (vi vil have nye film med)
      - En serie tilføjes til Plex via Sonarr
      - Brugeren markerer noget som set (ikke kritisk pga. TTL)

    Hvis plex_username er None, invalideres ALLE caches for ALLE brugere.
    """
    if plex_username is None:
        sizes = {
            "movie":   len(_movie_cache),
            "tv":      len(_tv_cache),
            "actor":   len(_actor_cache),
            "genre":   len(_genre_cache),
            "on_deck": len(_on_deck_cache),
        }
        _movie_cache.clear()
        _tv_cache.clear()
        _actor_cache.clear()
        _genre_cache.clear()
        _on_deck_cache.clear()
        logger.info("plex_cache fully invalidated: %s", sizes)
        return

    user_key = _make_key(plex_username)
    cleared = []

    if user_key in _movie_cache:
        _movie_cache.pop(user_key, None)
        cleared.append("movie")
    if user_key in _tv_cache:
        _tv_cache.pop(user_key, None)
        cleared.append("tv")
    if user_key in _on_deck_cache:
        _on_deck_cache.pop(user_key, None)
        cleared.append("on_deck")

    # Actor og genre cache har sammensatte keys - find alle med matching user_key
    actor_keys = [k for k in _actor_cache if k[1] == user_key]
    for k in actor_keys:
        _actor_cache.pop(k, None)
    if actor_keys:
        cleared.append(f"actor({len(actor_keys)})")

    genre_keys = [k for k in _genre_cache if k[2] == user_key]
    for k in genre_keys:
        _genre_cache.pop(k, None)
    if genre_keys:
        cleared.append(f"genre({len(genre_keys)})")

    logger.info(
        "plex_cache invalidated for '%s': %s",
        plex_username, ", ".join(cleared) if cleared else "intet at rydde",
    )


def get_cache_stats() -> dict:
    """
    Diagnostic info om alle cache-typer.
    Bruges fx i admin-debug-kommandoer.
    """
    now = time.time()

    def _entries(cache: dict, label_fn) -> list[dict]:
        out = []
        for key, (built_at, value) in cache.items():
            age = now - built_at
            out.append({
                "key":           label_fn(key),
                "age_seconds":   round(age, 1),
                "ttl_remaining": max(0, round(_CACHE_TTL_SECS - age, 1)),
                "size":          len(value) if hasattr(value, "__len__") else 1,
                "expired":       age >= _CACHE_TTL_SECS,
            })
        return out

    return {
        "ttl_seconds": _CACHE_TTL_SECS,
        "movie":   _entries(_movie_cache,   lambda k: k if k != _ADMIN_KEY else "admin"),
        "tv":      _entries(_tv_cache,      lambda k: k if k != _ADMIN_KEY else "admin"),
        "actor":   _entries(_actor_cache,   lambda k: f"{k[0]}@{k[1] if k[1] != _ADMIN_KEY else 'admin'}"),
        "genre":   _entries(_genre_cache,   lambda k: f"{k[0]}/{k[1] or 'all'}@{k[2] if k[2] != _ADMIN_KEY else 'admin'}"),
        "on_deck": _entries(_on_deck_cache, lambda k: k if k != _ADMIN_KEY else "admin"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers — Key normalization
# ══════════════════════════════════════════════════════════════════════════════

def _make_key(plex_username: str | None) -> str:
    """Normalisér plex_username til cache-key. None → admin."""
    if plex_username is None:
        return _ADMIN_KEY
    return plex_username.strip().lower() or _ADMIN_KEY


def _normalize_actor(actor_name: str) -> str:
    """Normalisér skuespiller-navn til cache-key. Whitespace + case-insensitive."""
    return " ".join(actor_name.strip().lower().split())


def _normalize_genre(genre: str | None) -> str:
    """Normalisér genre til cache-key. None → '_all_'."""
    if not genre:
        return "_all_"
    return genre.strip().lower()


# ══════════════════════════════════════════════════════════════════════════════
# Internal builders — Movie/TV index
# ══════════════════════════════════════════════════════════════════════════════

async def _build_index_async(
    plex_username: str | None,
    media_type: str,
) -> dict[int, object]:
    """Async wrapper for _build_index_sync."""
    try:
        return await asyncio.to_thread(
            partial(_build_index_sync, plex_username=plex_username, media_type=media_type)
        )
    except Exception as e:
        logger.error("_build_index_async error for '%s'/'%s': %s",
                     plex_username or "admin", media_type, e)
        return {}


def _build_index_sync(
    plex_username: str | None,
    media_type: str,
) -> dict[int, object]:
    """
    Byg {tmdb_id: PlexItem} index for film eller TV.
    Kører synkront - kaldes fra thread pool eller sync context.
    """
    from services.plex_service import (
        _connect, _sections, _MOVIE_TYPE, _TV_TYPE, _extract_tmdb_id_from_guids,
    )

    plex_type = _TV_TYPE if media_type == "tv" else _MOVIE_TYPE

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        logger.warning(
            "_build_index_sync: Plex-forbindelse fejlede for '%s': %s",
            plex_username or "admin", plex,
        )
        return {}

    sections = _sections(plex, plex_type)
    if not sections:
        logger.warning(
            "_build_index_sync: ingen %s-sektioner for '%s'",
            media_type, plex_username or "admin",
        )
        return {}

    index: dict[int, object] = {}

    for section in sections:
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning(
                "_build_index_sync: section.all() fejl '%s' for '%s': %s",
                section.title, plex_username or "admin", e,
            )
            continue

        for item in all_items:
            tmdb_id = _extract_tmdb_id_from_guids(item)
            if tmdb_id and tmdb_id not in index:
                index[tmdb_id] = item

    return index


# ══════════════════════════════════════════════════════════════════════════════
# Internal builders — Actor index
# ══════════════════════════════════════════════════════════════════════════════

async def _build_actor_index_async(
    actor_name: str,
    plex_username: str | None,
) -> tuple[set[int], set[str]]:
    """Async wrapper for _build_actor_index_sync."""
    try:
        return await asyncio.to_thread(
            partial(_build_actor_index_sync, actor_name=actor_name, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("_build_actor_index_async error for '%s': %s", actor_name, e)
        return set(), set()


def _build_actor_index_sync(
    actor_name: str,
    plex_username: str | None,
) -> tuple[set[int], set[str]]:
    """
    Byg sæt af (tmdb_ids, imdb_ids) for alle Plex-film hvor en skuespiller medvirker.
    Identisk logik med plex_service._build_actor_guid_set, men cached version.
    """
    from services.plex_service import (
        _connect, _sections, _MOVIE_TYPE,
        _extract_tmdb_id_from_guids, _extract_imdb_id_from_guids,
    )

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return set(), set()

    tmdb_ids: set[int] = set()
    imdb_ids: set[str] = set()

    for section in _sections(plex, _MOVIE_TYPE):
        try:
            actor_items = section.search(actor=actor_name)
        except Exception as e:
            logger.warning(
                "_build_actor_index_sync: section.search fejl for '%s' i '%s': %s",
                actor_name, section.title, e,
            )
            actor_items = []

        for item in actor_items:
            tid = _extract_tmdb_id_from_guids(item)
            iid = _extract_imdb_id_from_guids(item)
            if tid:
                tmdb_ids.add(tid)
            if iid:
                imdb_ids.add(iid)

    return tmdb_ids, imdb_ids


# ══════════════════════════════════════════════════════════════════════════════
# Internal builders — Genre index (unwatched)
# ══════════════════════════════════════════════════════════════════════════════

async def _build_genre_index_async(
    media_type: str,
    genre: str | None,
    plex_username: str | None,
) -> list[object]:
    """Async wrapper for _build_genre_index_sync."""
    try:
        return await asyncio.to_thread(
            partial(_build_genre_index_sync,
                    media_type=media_type, genre=genre, plex_username=plex_username)
        )
    except Exception as e:
        logger.error(
            "_build_genre_index_async error for '%s'/'%s': %s",
            media_type, genre or "all", e,
        )
        return []


def _build_genre_index_sync(
    media_type: str,
    genre: str | None,
    plex_username: str | None,
) -> list[object]:
    """
    Byg liste af USETE PlexItems der matcher en genre.
    Identisk filter-logik med plex_service._unwatched_sync, bare cached.
    """
    from services.plex_service import (
        _connect, _sections, _MOVIE_TYPE, _TV_TYPE,
        _normalise, _genre_matches,
    )

    plex_type = _TV_TYPE if media_type == "tv" else _MOVIE_TYPE

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return []

    candidates: list[object] = []
    norm_genre = _normalise(genre) if genre else None

    for section in _sections(plex, plex_type):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning(
                "_build_genre_index_sync: section.all() fejl i '%s': %s",
                section.title, e,
            )
            continue

        for item in all_items:
            # Kun usete
            if getattr(item, "viewCount", 0):
                continue
            # Genre-filter
            if norm_genre:
                item_genres = [_normalise(g.tag) for g in getattr(item, "genres", [])]
                if not _genre_matches(norm_genre, item_genres):
                    continue
            candidates.append(item)

    return candidates


# ══════════════════════════════════════════════════════════════════════════════
# Internal builders — OnDeck
# ══════════════════════════════════════════════════════════════════════════════

async def _build_on_deck_async(
    plex_username: str | None,
) -> list[object]:
    """Async wrapper for _build_on_deck_sync."""
    try:
        return await asyncio.to_thread(
            partial(_build_on_deck_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("_build_on_deck_async error for '%s': %s", plex_username or "admin", e)
        return []


def _build_on_deck_sync(plex_username: str | None) -> list[object]:
    """
    Hent OnDeck (top 10) fra Plex.
    Identisk med plex_service._get_on_deck_sync, bare cached.
    """
    from services.plex_service import _connect

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        return []

    try:
        return list(plex.library.onDeck()[:10])
    except Exception as e:
        logger.error("_build_on_deck_sync error: %s", e)
        return []