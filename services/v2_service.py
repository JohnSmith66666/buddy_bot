"""
services/v2_service.py - Datadrevet anbefalingsservice for film + TV.

CHANGES (v0.3.0 — TV support tilføjet):
  - NY PARAMETER: find_unwatched_v2 tager nu media_type ('movie' eller 'tv').
    Default 'movie' for bagudkompatibilitet med eksisterende kaldere.
  - NY: Auto-detect af media_type via subgenre_service hvis subgenre_id
    starter med 'tv_' prefix. Det betyder eksisterende kald som
    find_unwatched_v2('tv_murder_mystery') automatisk virker korrekt.
  - NY: Bruger get_plex_tv_index() når media_type='tv'.
  - NY: Database-laget bruger find_titles_by_subgenre() i stedet for
    find_films_by_subgenre() (som stadig findes som legacy wrapper).

UNCHANGED (v0.2.0 — bruger plex_cache for konsistens og performance):
  - PERFORMANCE: Andet+kald inden for 5 min er ~1ms i stedet for ~1-2 sek.
  - BUG FIX: _check_sync (i plex_service) bruger nu samme cache.

UNCHANGED (v0.1.0 — initial Etape 2):
  - find_unwatched_v2() async API (returnerer status-dict).
  - DB → Plex cross-check → unwatched-filter → smart-blanding logik.
  - Status-format ('ok'/'missing'/'error') uændret.

ARKITEKTUR:
  v2-service genbruger plex_cache for både film- og TV-index. Det betyder:
    - 1 enkelt sandhed for "hvilke titler har vi i Plex?"
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

from services.plex_cache import get_plex_movie_index, get_plex_tv_index
from services.plex_service import _slim
from services.subgenre_service import (
    detect_media_type,
    get_subgenre,
    validate_subgenre_id,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Public API — find_unwatched_v2
# ══════════════════════════════════════════════════════════════════════════════

async def find_unwatched_v2(
    subgenre_id: str,
    plex_username: str | None = None,
    limit: int = 5,
    media_type: str | None = None,
) -> dict:
    """
    Find usete titler i Plex der matcher en subgenre.

    Flow (5 trin):
      1. Validér subgenre + auto-detect media_type
      2. DB:    Hent kandidat-tmdb_ids fra tmdb_metadata via subgenre keywords
      3. Plex:  Hent cached {tmdb_id → PlexItem} index for det rigtige media_type
      4. Match: Behold kun titler der findes i Plex
      5. Filter: Behold kun titler med viewCount == 0 (usete)
      6. Output: Random sample af 'limit' titler, slim-format

    Args:
      subgenre_id:   ID fra subgenre_service (fx 'horror_slasher' eller 'tv_murder_mystery')
      plex_username: Plex-brugernavn (None = admin, hvis Home User cache fejler)
      limit:         Antal usete titler at returnere (default 5)
      media_type:    'movie' eller 'tv'. None = auto-detect via subgenre_id prefix.

    Returns:
      Success:
        {
          "status":         "ok",
          "subgenre":       "horror_slasher",
          "subgenre_label": "🪓 Motorsave & Ketchup",
          "media_type":     "movie",
          "results":        [<slim-dict>, ...],
          "stats": {
            "db_candidates":  120,
            "in_plex":        45,
            "unwatched":      32,
            "returned":       5,
          },
        }

      Empty (ingen matches eller alle set):
        {"status": "missing", "subgenre": "...", ...}

      Error (DB- eller Plex-fejl):
        {"status": "error", "message": "..."}
    """
    # Lazy import for at undgå circular dependency
    import database

    # ── 1. Validér subgenre + auto-detect media_type ──────────────────────────
    if not validate_subgenre_id(subgenre_id):
        logger.warning("find_unwatched_v2: ukendt subgenre_id='%s'", subgenre_id)
        return {
            "status":  "error",
            "message": f"Ukendt subgenre: '{subgenre_id}'",
        }

    # Auto-detect media_type hvis ikke angivet
    if media_type is None:
        media_type = detect_media_type(subgenre_id)
        if media_type is None:
            logger.error(
                "find_unwatched_v2: kunne ikke auto-detect media_type for '%s'",
                subgenre_id,
            )
            return {
                "status":  "error",
                "message": f"Kunne ikke afgøre om '{subgenre_id}' er film eller TV",
            }

    if media_type not in ("movie", "tv"):
        logger.error("find_unwatched_v2: ugyldig media_type='%s'", media_type)
        return {
            "status":  "error",
            "message": f"Ugyldig media_type: '{media_type}'",
        }

    subgenre       = get_subgenre(subgenre_id, media_type=media_type)
    subgenre_label = subgenre["label"] if subgenre else subgenre_id

    # ── 2. Hent kandidater fra DB ─────────────────────────────────────────────
    # Vi henter et bredt udsnit (limit * 6) for at have luft til:
    #   - Titler der ikke er i Plex (matched mod cache)
    #   - Titler brugeren har set
    #   - Random sample blandt resterende usete
    db_fetch_limit = max(limit * 6, 30)

    try:
        db_items = await database.find_titles_by_subgenre(
            subgenre_id=subgenre_id,
            media_type=media_type,
            limit=db_fetch_limit,
        )
    except Exception as e:
        logger.error(
            "find_unwatched_v2: DB-fejl for '%s'/%s: %s",
            subgenre_id, media_type, e,
        )
        return {
            "status":  "error",
            "message": f"Database-opslag fejlede: {e}",
        }

    if not db_items:
        logger.info(
            "find_unwatched_v2: '%s'/%s — ingen DB-kandidater",
            subgenre_id, media_type,
        )
        return {
            "status":         "missing",
            "subgenre":       subgenre_id,
            "subgenre_label": subgenre_label,
            "media_type":     media_type,
            "stats": {
                "db_candidates": 0,
                "in_plex":       0,
                "unwatched":     0,
                "returned":      0,
            },
        }

    # ── 3. Hent cached Plex-index (bygges hvis cold) ──────────────────────────
    try:
        if media_type == "movie":
            plex_index = await get_plex_movie_index(plex_username)
        else:  # tv
            plex_index = await get_plex_tv_index(plex_username)
    except Exception as e:
        logger.error(
            "find_unwatched_v2: Plex-cache fejl for '%s'/%s: %s",
            plex_username, media_type, e,
        )
        return {
            "status":  "error",
            "message": f"Plex-opslag fejlede: {e}",
        }

    if not plex_index:
        logger.warning(
            "find_unwatched_v2: tomt Plex-index for '%s'/%s",
            plex_username or "admin", media_type,
        )
        return {
            "status":         "missing",
            "subgenre":       subgenre_id,
            "subgenre_label": subgenre_label,
            "media_type":     media_type,
            "stats": {
                "db_candidates": len(db_items),
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
                db_items=db_items,
                plex_index=plex_index,
                limit=limit,
            )
        )
    except Exception as e:
        logger.error(
            "find_unwatched_v2: filter-fejl for '%s'/%s: %s",
            subgenre_id, media_type, e,
        )
        return {
            "status":  "error",
            "message": f"Filter-fejl: {e}",
        }

    # ── 5. Tilføj subgenre-metadata + log ─────────────────────────────────────
    result["subgenre"]       = subgenre_id
    result["subgenre_label"] = subgenre_label
    result["media_type"]     = media_type

    stats = result.get("stats", {})
    logger.info(
        "find_unwatched_v2: subgenre='%s' media=%s user='%s' — "
        "%d DB → %d in Plex → %d unwatched → %d returned",
        subgenre_id, media_type, plex_username or "admin",
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
    db_items: list[dict],
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

    for db_item in db_items:
        tmdb_id = db_item.get("tmdb_id")
        if not tmdb_id:
            continue

        plex_item = plex_index.get(tmdb_id)
        if plex_item is None:
            continue

        in_plex.append(plex_item)

        # Watch-status: viewCount == 0 betyder ikke set
        # Bemærk: For TV-serier er viewCount på show-niveau (sum af afsnit set).
        # Vi behandler en serie som "uset" hvis viewCount == 0 for hele showet.
        if not getattr(plex_item, "viewCount", 0):
            unwatched.append(plex_item)

    # ── Random sample af usete ────────────────────────────────────────────────
    if not unwatched:
        return {
            "status": "missing",
            "stats": {
                "db_candidates": len(db_items),
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
            "db_candidates": len(db_items),
            "in_plex":       len(in_plex),
            "unwatched":     len(unwatched),
            "returned":      sample_size,
        },
    }