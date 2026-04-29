"""
services/watchlist_sync_service.py - Hybrid Plex watchlist sync.

Synkroniserer brugerens Plex Discover watchlist med vores PostgreSQL
user_watchlist tabel. Lazy sync med 5 min cache betyder at:

  - Ved første åbn: API-kald til Plex, gem i DB
  - Inden for 5 min: vis cached data fra DB direkte
  - Efter 5 min: ny sync kører, opdaterer DB

Match Plex 1:1 (auto-fjern):
  Hvis bruger fjerner en titel i Plex-appen, fjernes den også fra vores DB
  ved næste sync. Plex er kilde til sandhed.

CHANGES (v0.1.0 — initial):
  - sync_user_watchlist() hovedfunktion med diff-logik.
  - is_sync_needed() tjek mod 5 min cache.
  - touch_last_synced() opdaterer timestamp.
  - get_watchlist_with_metadata() returnerer beriget data til UI.
"""

import logging
from datetime import datetime, timedelta, timezone

from database import _pool_ref
from services import user_data_service
from services.plex_watchlist_helpers import fetch_plex_watchlist

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Konstanter
# ══════════════════════════════════════════════════════════════════════════════

# Hvor længe cached watchlist betragtes som "fersk" inden vi syncer igen
SYNC_CACHE_TTL_MINUTES = 5


# ══════════════════════════════════════════════════════════════════════════════
# Sync state — gemmes i users tabellen
# ══════════════════════════════════════════════════════════════════════════════

async def _ensure_sync_column_exists() -> None:
    """
    Tilføj watchlist_last_synced_at kolonne til users tabel hvis den mangler.

    Kaldes ved første sync — idempotent. Bruger ALTER TABLE IF NOT EXISTS
    er ikke standard PostgreSQL, så vi bruger DO-blok i stedet.
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='users'
                    AND column_name='watchlist_last_synced_at'
                ) THEN
                    ALTER TABLE users
                    ADD COLUMN watchlist_last_synced_at TIMESTAMPTZ;
                END IF;
            END $$;
            """
        )


async def _ensure_source_column_exists() -> None:
    """
    Tilføj source kolonne til user_watchlist tabel hvis den mangler.

    'manual' = bruger trykkede 📌 knap
    'synced' = kom fra Plex sync
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='user_watchlist'
                    AND column_name='source'
                ) THEN
                    ALTER TABLE user_watchlist
                    ADD COLUMN source TEXT DEFAULT 'synced';
                END IF;
            END $$;
            """
        )


async def get_last_synced_at(telegram_id: int) -> datetime | None:
    """Hent timestamp for sidste sync — None hvis aldrig synced."""
    await _ensure_sync_column_exists()
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT watchlist_last_synced_at FROM users WHERE telegram_id = $1",
            telegram_id,
        )
        if row is None:
            return None
        return row["watchlist_last_synced_at"]


async def touch_last_synced(telegram_id: int) -> None:
    """Sæt sidste sync til 'nu'."""
    await _ensure_sync_column_exists()
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET watchlist_last_synced_at = NOW()
            WHERE telegram_id = $1
            """,
            telegram_id,
        )


async def is_sync_needed(telegram_id: int) -> bool:
    """
    Tjek om der skal syncs (cache er udløbet) eller om vi kan vise cached data.

    Returns:
      True hvis sync er nødvendig (aldrig synced eller >5 min siden).
      False hvis cached data stadig er fersk.
    """
    last_synced = await get_last_synced_at(telegram_id)
    if last_synced is None:
        return True

    # asyncpg returnerer timezone-aware datetime
    age = datetime.now(timezone.utc) - last_synced
    return age > timedelta(minutes=SYNC_CACHE_TTL_MINUTES)


# ══════════════════════════════════════════════════════════════════════════════
# Hovedfunktion: sync_user_watchlist
# ══════════════════════════════════════════════════════════════════════════════

async def sync_user_watchlist(
    telegram_id: int,
    plex_username: str | None = None,
    force: bool = False,
) -> dict:
    """
    Synkroniser brugerens Plex Discover watchlist med vores DB.

    Args:
      telegram_id:   Brugerens Telegram ID
      plex_username: Plex-brugernavn (None = admin)
      force:         Hvis True, sync uanset cache-alder

    Returns:
      dict med stats:
        {
          "synced": True/False,         # Om sync faktisk kørte
          "source": "plex"/"cache",     # Hvor data kom fra
          "added": int,                 # Nye items tilføjet til DB
          "removed": int,               # Items fjernet fra DB
          "total": int,                 # Total items i DB efter sync
          "error": str | None,          # Fejl-besked hvis fejlet
        }
    """
    await _ensure_source_column_exists()

    # Tjek om sync er nødvendig
    if not force and not await is_sync_needed(telegram_id):
        # Vis cached data direkte
        cached = await user_data_service.get_watchlist(telegram_id)
        return {
            "synced": False,
            "source": "cache",
            "added":  0,
            "removed": 0,
            "total":  len(cached),
            "error":  None,
        }

    # Sync er nødvendig — hent fra Plex
    logger.info(
        "Watchlist sync starter for telegram_id=%s (plex=%s)",
        telegram_id, plex_username,
    )

    try:
        plex_items = await fetch_plex_watchlist(plex_username=plex_username)
    except Exception as e:
        logger.error("sync_user_watchlist Plex fetch fejl: %s", e)
        return {
            "synced":  False,
            "source":  "error",
            "added":   0,
            "removed": 0,
            "total":   0,
            "error":   f"Kunne ikke kontakte Plex: {e}",
        }

    # Lav set med (tmdb_id, media_type) tupler for Plex-data
    plex_set = {
        (item["tmdb_id"], item["media_type"])
        for item in plex_items
    }

    # Hent nuværende DB-state
    current_db_items = await user_data_service.get_watchlist(telegram_id)
    db_set = {
        (item["tmdb_id"], item["media_type"])
        for item in current_db_items
    }

    # Diff
    to_add    = plex_set - db_set
    to_remove = db_set - plex_set

    added_count = 0
    removed_count = 0

    # Tilføj nye items
    for tmdb_id, media_type in to_add:
        try:
            success = await user_data_service.add_to_watchlist(
                telegram_id=telegram_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
            )
            if success:
                added_count += 1
                # Marker source som 'synced' (default fra DDL)
                # Hvis 📌 knap-koden allerede har sat 'manual', overskriver
                # vi ikke det her — kun nye records får 'synced'.
        except Exception as e:
            logger.warning(
                "sync add fejl for tmdb_id=%s media=%s: %s",
                tmdb_id, media_type, e,
            )

    # Fjern items der ikke længere er i Plex (auto-fjern strategi)
    for tmdb_id, media_type in to_remove:
        try:
            success = await user_data_service.remove_from_watchlist(
                telegram_id=telegram_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
            )
            if success:
                removed_count += 1
        except Exception as e:
            logger.warning(
                "sync remove fejl for tmdb_id=%s media=%s: %s",
                tmdb_id, media_type, e,
            )

    # Opdatér last_synced_at
    await touch_last_synced(telegram_id)

    total = await user_data_service.count_watchlist(telegram_id)

    logger.info(
        "Watchlist sync færdig for telegram_id=%s: +%d, -%d, total=%d",
        telegram_id, added_count, removed_count, total,
    )

    return {
        "synced":  True,
        "source":  "plex",
        "added":   added_count,
        "removed": removed_count,
        "total":   total,
        "error":   None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# UI helper: hent watchlist med beriget data
# ══════════════════════════════════════════════════════════════════════════════

async def get_watchlist_with_metadata(
    telegram_id: int,
    plex_username: str | None = None,
    auto_sync: bool = True,
) -> dict:
    """
    Hent brugerens watchlist med beriget metadata (titel, år, rating fra TMDB).

    Hvis auto_sync=True, sync'er først hvis cache er udløbet.

    Args:
      telegram_id:   Brugerens Telegram ID
      plex_username: Plex-brugernavn (None = admin)
      auto_sync:     Hvis True, sync hvis cache er gammel

    Returns:
      dict:
        {
          "items": [
            {
              "tmdb_id":    603,
              "media_type": "movie",
              "title":      "The Matrix",
              "year":       1999,
              "rating":     8.7,
              "added_at":   datetime,
            },
            ...
          ],
          "sync_status": "synced" | "cached" | "error",
          "last_synced_at": datetime | None,
          "error": str | None,
        }
    """
    sync_result = {"synced": False, "error": None}

    if auto_sync:
        sync_result = await sync_user_watchlist(telegram_id, plex_username)

    # Hent watchlist fra DB
    db_items = await user_data_service.get_watchlist(telegram_id)

    # Berig med TMDB metadata fra vores tmdb_metadata tabel
    enriched_items = []
    for item in db_items:
        enriched = dict(item)  # Kopiér så vi ikke muterer originalen

        # Forsøg at hente titel/år/rating fra tmdb_metadata cache
        try:
            async with _pool_ref().acquire() as conn:
                meta_row = await conn.fetchrow(
                    """
                    SELECT title, year
                    FROM tmdb_metadata
                    WHERE tmdb_id = $1 AND media_type = $2
                    """,
                    item["tmdb_id"], item["media_type"],
                )
                if meta_row:
                    enriched["title"] = meta_row["title"]
                    enriched["year"]  = meta_row["year"]
                else:
                    enriched["title"] = f"#{item['tmdb_id']}"
                    enriched["year"]  = None
        except Exception as e:
            logger.warning(
                "get_watchlist_with_metadata enrichment fejl tmdb=%s: %s",
                item["tmdb_id"], e,
            )
            enriched["title"] = f"#{item['tmdb_id']}"
            enriched["year"]  = None

        enriched_items.append(enriched)

    last_synced_at = await get_last_synced_at(telegram_id)

    if sync_result.get("error"):
        sync_status = "error"
    elif sync_result.get("synced"):
        sync_status = "synced"
    else:
        sync_status = "cached"

    return {
        "items":          enriched_items,
        "sync_status":    sync_status,
        "last_synced_at": last_synced_at,
        "error":          sync_result.get("error"),
    }