"""
services/v2_service.py - Datadrevet film-anbefalingsservice (Etape 2).

CHANGES (v0.1.0 — initial implementation, Etape 2 af subgenre-projekt):
  - NY ASYNC: find_unwatched_v2(subgenre_id, plex_username, limit=5)
    Datadrevet erstatning for den gamle find_unwatched().
  - NY SYNC: _enrich_and_filter_sync() — bygger Plex-index, krydschecker
    DB-kandidater, filtrerer usete, returnerer slim-format.
  - NY SYNC: _build_plex_movie_index() — {tmdb_id: PlexItem} via section.all().

ARKITEKTUR:
  v2-service er BEVIDST adskilt fra plex_service.py. Plex_service.py er 1385
  linjer og indeholder den gamle find_unwatched() som vi vil bevare indtil
  Etape 4. Ved at lægge v2 i en separat fil holder vi:
    - Lower deploy-risk (ingen ændringer i 1385 linjer)
    - Klar separation v1/v2
    - Cirkulære imports undgås (subgenre_service → v2_service, ikke omvendt)

  v2_service genbruger plex_service's interne helpers (_connect, _sections,
  _slim, _extract_tmdb_id_from_guids) via direkte import. Det er OK at
  importere "private" funktioner fra plex_service inden for samme service-pakke.

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

from services.plex_service import (
    _connect,
    _extract_tmdb_id_from_guids,
    _MOVIE_TYPE,
    _sections,
    _slim,
)
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
      2. Plex:  Byg index af brugerens film-sektioner ({tmdb_id → PlexItem})
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
        {
          "status":         "missing",
          "subgenre":       "...",
          "subgenre_label": "...",
          "stats":          {...},
        }

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

    # ── 3 + 4. Plex cross-check + watch-status filter (i thread pool) ─────────
    try:
        result = await asyncio.to_thread(
            partial(
                _enrich_and_filter_sync,
                db_films=db_films,
                plex_username=plex_username,
                limit=limit,
            )
        )
    except Exception as e:
        logger.error("find_unwatched_v2: Plex-fejl for '%s': %s", subgenre_id, e)
        return {
            "status":  "error",
            "message": f"Plex-opslag fejlede: {e}",
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
# Synkron Plex-cross-check (kører i thread pool)
# ══════════════════════════════════════════════════════════════════════════════

def _enrich_and_filter_sync(
    db_films: list[dict],
    plex_username: str | None,
    limit: int,
) -> dict:
    """
    Synkron del af find_unwatched_v2: Plex-scan + filter + sample.

    Kører i thread pool via asyncio.to_thread() for ikke at blokere event-loop,
    fordi PlexAPI er synkron.

    Returns:
      Status dict (uden subgenre-metadata — det tilføjes i find_unwatched_v2):
        {"status": "ok"|"missing"|"error", "results": [...], "stats": {...}}
    """
    # ── Forbind til Plex ──────────────────────────────────────────────────────
    plex = _connect(plex_username)
    if isinstance(plex, dict):
        # _connect returnerer dict ved fejl
        logger.error("_enrich_and_filter_sync: Plex-forbindelse fejlede: %s", plex)
        return plex

    # ── Byg index af alle Plex-film med tmdb_id ───────────────────────────────
    plex_index = _build_plex_movie_index(plex)
    if not plex_index:
        logger.warning("_enrich_and_filter_sync: Plex-index er tomt — ingen film fundet")
        return {
            "status": "missing",
            "stats": {
                "db_candidates": len(db_films),
                "in_plex":       0,
                "unwatched":     0,
                "returned":      0,
            },
        }

    # ── Match DB-kandidater mod Plex-index ────────────────────────────────────
    # Vi bevarer DB-rækkefølgen (smart-blanding fra database.py).
    # PlexItems har viewCount-attribut der reflekterer den aktuelle bruger
    # (når plex_username blev brugt i _connect).
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


# ══════════════════════════════════════════════════════════════════════════════
# Plex movie index builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_plex_movie_index(plex) -> dict[int, object]:
    """
    Byg dict {tmdb_id → PlexItem} for alle film i alle film-sektioner.

    Bruges af find_unwatched_v2 til lyn-hurtigt cross-check af DB-kandidater
    mod brugerens faktiske Plex-bibliotek.

    Bruger section.all() (hele biblioteket) i stedet for section.search()
    (default Plex-limit 20). Hver film kører gennem _extract_tmdb_id_from_guids
    så kun film med TMDB GUID inkluderes — det er ~99% af biblioteket.

    Performance: ~1-2 sekunder for 6.500 film (ren Python-iteration uden
    netværkskald). Kunne caches med TTL men vi venter med det indtil v2
    er stabil og vi har målt rigtig brugerbelastning.

    PlexItem'et returneres direkte (ikke en kopi) så viewCount-attributten
    afspejler den aktuelle bruger (når plex_username er angivet til _connect).
    """
    plex_index: dict[int, object] = {}
    sections   = _sections(plex, _MOVIE_TYPE)

    if not sections:
        logger.warning("_build_plex_movie_index: ingen film-sektioner fundet")
        return plex_index

    for section in sections:
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning(
                "_build_plex_movie_index: section.all() fejl '%s': %s",
                section.title, e,
            )
            continue

        for item in all_items:
            tmdb_id = _extract_tmdb_id_from_guids(item)
            if tmdb_id and tmdb_id not in plex_index:
                plex_index[tmdb_id] = item

    logger.info(
        "_build_plex_movie_index: %d film med TMDB GUID indekseret",
        len(plex_index),
    )
    return plex_index