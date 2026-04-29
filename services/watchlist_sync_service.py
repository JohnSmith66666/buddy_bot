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

CHANGES (v0.2.0 — fix manglende titler):
  - FIX: Gemmer nu cached_title, cached_year, cached_rating direkte i DB
    ved sync. Tidligere var titlerne kun i tmdb_metadata cachen — som ikke
    indeholder titler brugeren ikke har på Plex.
  - get_watchlist_with_metadata() bruger nu primært cached_title fra DB,
    med fallback til tmdb_metadata for gamle records uden cached titel.
  - Næste sync efter denne deployment vil populate cached_* for alle
    eksisterende records.

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

    v0.2.0: Gemmer nu også cached_title, cached_year, cached_rating fra Plex
    direkte i DB så watchlist-visningen altid har titler tilgængelige.

    Args:
      telegram_id:   Brugerens Telegram ID
      plex_username: Plex-brugernavn (None = admin)
      force:         Hvis True, sync uanset cache-alder

    Returns:
      dict med stats (synced, source, added, removed, total, error)
    """
    await _ensure_source_column_exists()

    # Tjek om sync er nødvendig
    if not force and not await is_sync_needed(telegram_id):
        cached = await user_data_service.get_watchlist(telegram_id)
        return {
            "synced":  False,
            "source":  "cache",
            "added":   0,
            "removed": 0,
            "total":   len(cached),
            "error":   None,
        }

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

    # Byg lookup-dict fra Plex med (tmdb_id, media_type) → fuld info
    plex_lookup = {
        (item["tmdb_id"], item["media_type"]): item
        for item in plex_items
    }
    plex_set = set(plex_lookup.keys())

    # Hent nuværende DB-state
    current_db_items = await user_data_service.get_watchlist(telegram_id)
    db_set = {
        (item["tmdb_id"], item["media_type"])
        for item in current_db_items
    }

    # Diff
    to_add    = plex_set - db_set
    to_remove = db_set - plex_set
    to_update = plex_set & db_set  # Eksisterende — opdatér cached_* hvis NULL

    added_count = 0
    removed_count = 0
    updated_count = 0

    # Tilføj NYE items med fuld metadata fra Plex
    for key in to_add:
        plex_item = plex_lookup[key]
        try:
            success = await user_data_service.add_to_watchlist(
                telegram_id=telegram_id,
                tmdb_id=plex_item["tmdb_id"],
                media_type=plex_item["media_type"],
                cached_title=plex_item.get("title"),
                cached_year=plex_item.get("year"),
                cached_rating=plex_item.get("rating"),
            )
            if success:
                added_count += 1
        except Exception as e:
            logger.warning(
                "sync add fejl for tmdb_id=%s media=%s: %s",
                plex_item["tmdb_id"], plex_item["media_type"], e,
            )

    # OPDATÉR EKSISTERENDE items med cached_* hvis de mangler
    # (vigtigt for ALLE eksisterende rows fra v0.1.0 der ikke havde cached_*)
    for key in to_update:
        plex_item = plex_lookup[key]
        try:
            # add_to_watchlist håndterer ON CONFLICT UPDATE for cached_* felter
            await user_data_service.add_to_watchlist(
                telegram_id=telegram_id,
                tmdb_id=plex_item["tmdb_id"],
                media_type=plex_item["media_type"],
                cached_title=plex_item.get("title"),
                cached_year=plex_item.get("year"),
                cached_rating=plex_item.get("rating"),
            )
            updated_count += 1
        except Exception as e:
            logger.warning(
                "sync update fejl for tmdb_id=%s media=%s: %s",
                plex_item["tmdb_id"], plex_item["media_type"], e,
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
        "Watchlist sync færdig for telegram_id=%s: +%d, -%d, ~%d, total=%d",
        telegram_id, added_count, removed_count, updated_count, total,
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
    Hent brugerens watchlist med beriget metadata (titel, år, rating).

    v0.2.0: Bruger primært cached_* felter fra DB. Fallback til tmdb_metadata
    for gamle records (oprettet inden v0.2.0) der ikke har cached_*.

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

    # Berig hver item: brug cached_* primært, fallback til tmdb_metadata
    enriched_items = []
    for item in db_items:
        enriched = dict(item)

        # Forsøg primært at bruge cached_* fra DB (sat ved sync)
        title  = item.get("cached_title")
        year   = item.get("cached_year")
        rating = item.get("cached_rating")

        # Fallback: hvis cached_title mangler, prøv tmdb_metadata
        if not title:
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
                        title = meta_row["title"]
                        if not year:
                            year = meta_row["year"]
            except Exception as e:
                logger.warning(
                    "get_watchlist_with_metadata fallback fejl tmdb=%s: %s",
                    item["tmdb_id"], e,
                )

        # Sidste fallback: vis ID hvis vi virkelig intet har
        if not title:
            title = f"#{item['tmdb_id']}"

        enriched["title"]  = title
        enriched["year"]   = year
        enriched["rating"] = float(rating) if rating else None

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