"""
services/plex_cache.py - Per-bruger TTL-cache for Plex movie index.

CHANGES (v0.1.0 — initial implementation, fix til "Tilføj til Plex"-bug):
  - NY: get_plex_movie_index(plex_username) — TTL-cached lookup
    Bygger {tmdb_id: PlexItem} index for én bruger og cacher det i 5 min.
    Genbruges af BÅDE find_unwatched_v2 OG _check_sync's Lag 0.

  - PROBLEM DETTE LØSER:
    Før denne cache havde vi to forskellige Plex-tjek der kunne svare
    forskelligt for samme film:
    1. find_unwatched_v2 → _build_plex_movie_index (section.all() + GUID)
       FANDT filmen ✅
    2. _check_sync Lag 0 → section.search(guid='tmdb://X')
       FANDT IKKE filmen ❌ (Plex's GUID-index er ikke altid synkron)
    Resultat: Brugeren så 'Tilføj til Plex' selvom filmen var i biblioteket.

  - LØSNING:
    Begge funktioner bruger nu samme cache → samme datakilde →
    konsistente svar. Cache bygges via section.all() + manuel GUID-extract,
    som er den mest pålidelige metode.

DESIGN:
  - Per-bruger: hver Plex-username får sin egen cache (admin, Home Users)
  - TTL: 5 minutter — kort nok til at fange viewCount-ændringer,
    langt nok til at give massiv performance-boost ved gentagne kald
  - Async lock: forhindrer race condition hvor 2 samtidige requests
    bygger samme cache parallelt
  - Lazy: cache bygges kun når en bruger rent faktisk har brug for den
  - Self-cleanup: udløbne cache-entries fjernes automatisk ved næste GET

PERFORMANCE:
  - Cold cache (første kald): ~1-2 sek (section.all() over hele biblioteket)
  - Warm cache (subsequent): ~1ms (dict lookup)
  - RAM: ~5MB per bruger med 6.500 film (PlexItem-referencer)

USAGE:
  from services.plex_cache import get_plex_movie_index

  index = await get_plex_movie_index(plex_username="stream365_admin")
  plex_item = index.get(tmdb_id)  # None hvis ikke i biblioteket
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

_CACHE_TTL_SECS = 300  # 5 minutter

# Special key for None plex_username (bruger admin-token)
_ADMIN_KEY = "__admin__"


# ══════════════════════════════════════════════════════════════════════════════
# Cache state (per-process, nulstilles ved Railway redeploy)
# ══════════════════════════════════════════════════════════════════════════════

# Format: {cache_key: (built_at_unix_timestamp, {tmdb_id: PlexItem})}
_cache: dict[str, tuple[float, dict[int, object]]] = {}

# Per-bruger lock så vi ikke bygger samme cache parallelt
# ved 2 samtidige requests fra samme bruger
_locks: dict[str, asyncio.Lock] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

async def get_plex_movie_index(
    plex_username: str | None = None,
) -> dict[int, object]:
    """
    Hent {tmdb_id: PlexItem} index for en bruger med 5-min TTL-cache.

    Args:
      plex_username: Plex-username eller None (bruger admin-token).
                     Cache er per-bruger.

    Returns:
      dict mapping tmdb_id → PlexItem.
      PlexItem.viewCount reflekterer den specifikke brugers watch-status.
      Tom dict hvis Plex-forbindelsen fejler.

    Performance:
      - Cold (første kald per bruger per 5 min): ~1-2 sek
      - Warm (subsequent): ~1ms

    Thread-safety:
      Bruger asyncio.Lock per bruger så samtidige kald deler samme cache-build
      i stedet for at duplikere arbejdet.
    """
    cache_key = _make_key(plex_username)

    # ── Hurtig path: cache HIT ────────────────────────────────────────────────
    cached = _cache.get(cache_key)
    if cached is not None:
        built_at, index = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "plex_cache HIT for '%s' (age=%.0fs, %d film)",
                plex_username or "admin", age, len(index),
            )
            return index
        # TTL udløbet — fjern fra cache og byg ny
        logger.info(
            "plex_cache TTL udløbet for '%s' (age=%.0fs) — rebuilds",
            plex_username or "admin", age,
        )
        _cache.pop(cache_key, None)

    # ── Cache MISS — byg under per-bruger lock ────────────────────────────────
    # Lock'en sikrer at hvis 2 requests rammer samme bruger samtidig,
    # bygger vi cachen ÉN gang og deler resultatet.
    lock = _locks.setdefault(cache_key, asyncio.Lock())

    async with lock:
        # Re-tjek cachen efter vi fik lock'en — en anden coroutine
        # kan have bygget den mens vi ventede.
        cached = _cache.get(cache_key)
        if cached is not None:
            built_at, index = cached
            if time.time() - built_at < _CACHE_TTL_SECS:
                logger.debug("plex_cache HIT efter lock-wait for '%s'", plex_username or "admin")
                return index

        # Byg cachen
        index = await _build_index(plex_username)
        _cache[cache_key] = (time.time(), index)

        logger.info(
            "plex_cache rebuilt for '%s': %d film med TMDB GUID",
            plex_username or "admin", len(index),
        )
        return index


def get_plex_movie_index_sync(
    plex_username: str | None = None,
) -> dict[int, object]:
    """
    Synkron variant af get_plex_movie_index.

    Bruges fra synkrone funktioner der allerede kører i thread pool
    (fx _check_sync i plex_service.py). Tager samme cache som async-versionen
    så de deler datakilde.

    Args:
      plex_username: Plex-username eller None (admin).

    Returns:
      dict mapping tmdb_id → PlexItem. Tom dict ved fejl.

    Thread-safety:
      Modsat async-versionen bruger denne IKKE asyncio.Lock (det giver ingen
      mening i synkron kontekst). I praksis er race conditions ufarlige fordi:
      1. CPython's GIL gør dict.get/set atomic
      2. Worst case er at 2 samtidige requests bygger samme cache parallelt
         — den sidste til at skrive vinder, og begge får et gyldigt index
      3. Cachen bygges sjældent (en gang per 5 min) så kollisioner er sjældne

    Performance: Samme som async-versionen.
    """
    cache_key = _make_key(plex_username)

    # ── Hurtig path: cache HIT ────────────────────────────────────────────────
    cached = _cache.get(cache_key)
    if cached is not None:
        built_at, index = cached
        age = time.time() - built_at
        if age < _CACHE_TTL_SECS:
            logger.debug(
                "plex_cache HIT (sync) for '%s' (age=%.0fs, %d film)",
                plex_username or "admin", age, len(index),
            )
            return index
        logger.info(
            "plex_cache TTL udløbet (sync) for '%s' (age=%.0fs) — rebuilds",
            plex_username or "admin", age,
        )
        _cache.pop(cache_key, None)

    # ── Cache MISS — byg synkront ─────────────────────────────────────────────
    index = _build_index_sync(plex_username)
    _cache[cache_key] = (time.time(), index)

    logger.info(
        "plex_cache rebuilt (sync) for '%s': %d film med TMDB GUID",
        plex_username or "admin", len(index),
    )
    return index


def invalidate_plex_cache(plex_username: str | None = None) -> None:
    """
    Tving rebuild af cache for en specifik bruger ved næste get_plex_movie_index().

    Bruges fx når:
      - En film tilføjes til Plex via Radarr (vi vil have nye film med)
      - Brugeren markerer noget som set (ikke kritisk pga. TTL)

    Hvis plex_username er None, invalideres ALLE caches.
    """
    if plex_username is None:
        count = len(_cache)
        _cache.clear()
        logger.info("plex_cache fully invalidated (%d brugere)", count)
        return

    cache_key = _make_key(plex_username)
    if cache_key in _cache:
        _cache.pop(cache_key, None)
        logger.info("plex_cache invalidated for '%s'", plex_username)


def get_cache_stats() -> dict:
    """
    Returnér diagnostic-info om cachen.
    Bruges fx i admin-debug-kommandoer.
    """
    now = time.time()
    entries = []
    for key, (built_at, index) in _cache.items():
        age = now - built_at
        entries.append({
            "user":         key if key != _ADMIN_KEY else "admin",
            "age_seconds":  round(age, 1),
            "ttl_remaining": max(0, round(_CACHE_TTL_SECS - age, 1)),
            "film_count":   len(index),
            "expired":      age >= _CACHE_TTL_SECS,
        })

    return {
        "cache_size":  len(_cache),
        "ttl_seconds": _CACHE_TTL_SECS,
        "entries":     entries,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_key(plex_username: str | None) -> str:
    """Normalisér plex_username til cache-key. None → admin."""
    if plex_username is None:
        return _ADMIN_KEY
    return plex_username.strip().lower() or _ADMIN_KEY


async def _build_index(plex_username: str | None) -> dict[int, object]:
    """
    Byg det egentlige index ved at scanne hele Plex film-biblioteket.
    Kører i thread pool fordi PlexAPI er synkron.
    """
    try:
        return await asyncio.to_thread(
            partial(_build_index_sync, plex_username=plex_username)
        )
    except Exception as e:
        logger.error("_build_index error for '%s': %s", plex_username or "admin", e)
        return {}


def _build_index_sync(plex_username: str | None) -> dict[int, object]:
    """
    Synkron implementering — kører i thread pool.

    Bruger section.all() (hele biblioteket) i stedet for section.search()
    for at undgå Plex' default-limit på 20 film. Hver film køres gennem
    _extract_tmdb_id_from_guids så kun film med TMDB GUID inkluderes —
    det er ~99% af biblioteket på en velvedligeholdt Plex-server.

    Bemærk: Lazy import af plex_service-helpers for at undgå circular import.
    """
    from services.plex_service import (
        _connect, _sections, _MOVIE_TYPE, _extract_tmdb_id_from_guids,
    )

    plex = _connect(plex_username)
    if isinstance(plex, dict):
        # _connect returnerer dict ved fejl
        logger.warning(
            "_build_index_sync: Plex-forbindelse fejlede for '%s': %s",
            plex_username or "admin", plex,
        )
        return {}

    sections = _sections(plex, _MOVIE_TYPE)
    if not sections:
        logger.warning(
            "_build_index_sync: ingen film-sektioner for '%s'",
            plex_username or "admin",
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