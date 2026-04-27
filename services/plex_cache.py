"""
services/v2_service.py - Datadrevet film-anbefalingsservice (Etape 2).

CHANGES (v0.2.0 — bruger plex_cache for konsistens og performance):
  - REFAKTOR: _build_plex_movie_index er FJERNET (flyttet til plex_cache.py).
  - REFAKTOR: _enrich_and_filter_sync er splittet — cache-lookup sker nu
    async via plex_cache.get_plex_movie_index, så vi får TTL-fordelene.
  - PERFORMANCE: Andet+kald inden for 5 min er ~1ms i stedet for ~1-2 sek.
  - BUG FIX: _check_sync (i plex_service) bruger nu samme cache, så vi får
    konsistente svar mellem find_unwatched_v2 og show_confirmation. Tidligere
    kunne en film være "i Plex" iflg. v2 men "ikke i Plex" iflg. _check_sync's
    Lag 0 GUID-tjek (`section.search(guid=...)` fejler intermittent for nogle film).

UNCHANGED (v0.1.0 — initial Etape 2):
  - find_unwatched_v2() async API uændret.
  - DB → Plex cross-check → unwatched-filter → smart-blanding logik uændret.
  - Status-format ('ok'/'missing'/'error') uændret.

ARKITEKTUR:
  v2-service genbruger nu plex_cache for film-index. Det betyder:
    - 1 enkelt sandhed for "hvilke film har vi i Plex?"
    - Cachen deles mellem find_unwatched_v2 og _check_sync
    - Ingen duplikeret Plex-scanning

DESIGN-PRINCIPPER:
  - Database-laget kender intet om Plex.
  - Plex-laget kender intet om subgenrer.
  - Denne fil er FLISEN der binder dem sammen.
  - Returnerer struktureret status-dict — kaster ALDRIG exceptions ud i
    AI-laget. Fejl logges og returneres som status='error'.
"""

import asyncio
import logging
import random
from functools import partial

from services.plex_cache import get_plex_movie_index
from services.plex_service import _slim
from services.subgenre_service import get_subgenre, validate_subgenre_id

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Public API — find_unwatched_v2
# ══════════════════════════════════════════════════════════════════════════════

async def find_unwatched_v2(
    subgenre_id: str,
    plex_username: str | None = None,
    limit: int = 5,
) -> dict:
    """
    Find usete film i Plex der matcher en subgenre.

    Flow (4 trin):
      1. DB:    Hent kandidat-tmdb_ids fra tmdb_metadata via subgenre keywords
      2. Plex:  Hent cached {tmdb_id → PlexItem} index (bygges hvis cold)
      3. Match: Behold kun film der findes i Plex
      4. Filter: Behold kun film med viewCount == 0 (usete)
      5. Output: Random sample af 'limit' film, slim-format

    Args:
      subgenre_id:   ID fra subgenre_service (fx 'horror_slasher')
      plex_username: Plex-brugernavn (None = admin, hvis Home User cache fejler)
      limit:         Antal usete film at returnere (default 5)

    Returns:
      Success:
        {
          "status":         "ok",
          "subgenre":       "horror_slasher",
          "subgenre_label": "🪓 Motorsave & Ketchup",
          "results":        [<slim-dict>, ...],
          "stats": {
            "db_candidates":  120,
            "in_plex":        45,
            "unwatched":      32,
            "returned":       5,
          },
        }

      Empty (ingen matches eller alle set):
        {"status": "missing", "subgenre": "...", "subgenre_label": "...", "stats": {...}}

      Error (DB- eller Plex-fejl):
        {"status": "error", "message": "..."}

      Invalid subgenre:
        {"status": "error", "message": "Ukendt subgenre: '...'"}
    """
    # Lazy import for at undgå circular dependency
    import database

    # ── 1. Validér subgenre ───────────────────────────────────────────────────
    if not validate_subgenre_id(subgenre_id):
        logger.warning("find_unwatched_v2: ukendt subgenre_id='%s'", subgenre_id)
        return {
            "status":  "error",
            "message": f"Ukendt subgenre: '{subgenre_id}'",
        }

    subgenre       = get_subgenre(subgenre_id)
    subgenre_label = subgenre["label"]

    # ── 2. Hent kandidater fra DB ─────────────────────────────────────────────
    # Vi henter et bredt udsnit (limit * 6) for at have luft til:
    #   - Film der ikke er i Plex (matched mod cache)
    #   - Film brugeren har set
    #   - Random sample blandt resterende usete
    db_fetch_limit = max(limit * 6, 30)

    try:
        db_films = await database.find_films_by_subgenre(
            subgenre_id=subgenre_id,
            limit=db_fetch_limit,
        )
    except Exception as e:
        logger.error("find_unwatched_v2: DB-fejl for '%s': %s", subgenre_id, e)
        return {
            "status":  "error",
            "message": f"Database-opslag fejlede: {e}",
        }

    if not db_films:
        logger.info("find_unwatched_v2: '%s' — ingen DB-kandidater", subgenre_id)
        return {
            "status":         "missing",
            "subgenre":       subgenre_id,
            "subgenre_label": subgenre_label,
            "stats": {
                "db_candidates": 0,
                "in_plex":       0,
                "unwatched":     0,
                "returned":      0,
            },
        }

    # ── 3. Hent cached Plex-index (bygges hvis cold) ──────────────────────────
    try:
        plex_index = await get_plex_movie_index(plex_username)
    except Exception as e:
        logger.error("find_unwatched_v2: Plex-cache fejl for '%s': %s", plex_username, e)
        return {
            "status":  "error",
            "message": f"Plex-opslag fejlede: {e}",
        }

    if not plex_index:
        logger.warning("find_unwatched_v2: tomt Plex-index for '%s'", plex_username or "admin")
        return {
            "status":         "missing",
            "subgenre":       subgenre_id,
            "subgenre_label": subgenre_label,
            "stats": {
                "db_candidates": len(db_films),
                "in_plex":       0,
                "unwatched":     0,
                "returned":      0,
            },
        }

    # ── 4. Cross-check + filter (i thread pool så _slim ikke blokerer) ────────
    try:
        result = await asyncio.to_thread(
            partial(
                _filter_and_sample_sync,
                db_films=db_films,
                plex_index=plex_index,
                limit=limit,
            )
        )
    except Exception as e:
        logger.error("find_unwatched_v2: filter-fejl for '%s': %s", subgenre_id, e)
        return {
            "status":  "error",
            "message": f"Filter-fejl: {e}",
        }

    # ── 5. Tilføj subgenre-metadata + log ─────────────────────────────────────
    result["subgenre"]       = subgenre_id
    result["subgenre_label"] = subgenre_label

    stats = result.get("stats", {})
    logger.info(
        "find_unwatched_v2: subgenre='%s' user='%s' — "
        "%d DB → %d in Plex → %d unwatched → %d returned",
        subgenre_id, plex_username or "admin",
        stats.get("db_candidates", 0),
        stats.get("in_plex",       0),
        stats.get("unwatched",     0),
        stats.get("returned",      0),
    )

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Synkron filter + sample (kører i thread pool)
# ══════════════════════════════════════════════════════════════════════════════

def _filter_and_sample_sync(
    db_films: list[dict],
    plex_index: dict[int, object],
    limit: int,
) -> dict:
    """
    Synkron del: cross-check DB-kandidater mod cached Plex-index, filter + sample.

    Kører i thread pool fordi _slim(item) tilgår PlexItem-attributter der
    potentielt udløser lazy loading (PlexAPI er synkron).

    Returns:
      Status dict (uden subgenre-metadata — det tilføjes i find_unwatched_v2):
        {"status": "ok"|"missing", "results": [...], "stats": {...}}
    """
    in_plex:   list = []
    unwatched: list = []

    for db_film in db_films:
        tmdb_id = db_film.get("tmdb_id")
        if not tmdb_id:
            continue

        plex_item = plex_index.get(tmdb_id)
        if plex_item is None:
            continue

        in_plex.append(plex_item)

        # Watch-status: viewCount == 0 betyder ikke set
        if not getattr(plex_item, "viewCount", 0):
            unwatched.append(plex_item)

    # ── Random sample af usete ────────────────────────────────────────────────
    if not unwatched:
        return {
            "status": "missing",
            "stats": {
                "db_candidates": len(db_films),
                "in_plex":       len(in_plex),
                "unwatched":     0,
                "returned":      0,
            },
        }

    sample_size = min(limit, len(unwatched))
    chosen      = random.sample(unwatched, sample_size)

    return {
        "status":  "ok",
        "results": [_slim(item) for item in chosen],
        "stats": {
            "db_candidates": len(db_films),
            "in_plex":       len(in_plex),
            "unwatched":     len(unwatched),
            "returned":      sample_size,
        },
    }