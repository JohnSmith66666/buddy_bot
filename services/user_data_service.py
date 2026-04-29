"""
services/user_data_service.py - Data access layer for Buddy 2.0 user data.

CHANGES (v0.2.0 — cached metadata fields):
  - NY: add_to_watchlist() accepterer nu valgfri cached_title, cached_year, cached_rating
    parametre. Gemmes i DB ved INSERT, opdateres ved CONFLICT (hvis ikke-null).
  - NY: get_watchlist() returnerer nu også cached_title, cached_year, cached_rating
    i hver row.
  - NY: _ensure_cached_columns_exist() — idempotent kolonne-tilføjelse via DO-blok.
    Kaldes automatisk ved første add_to_watchlist() / get_watchlist().
  - 100% BAGUDKOMPATIBEL: Eksisterende kald uden de nye params virker stadig.

CHANGES (v0.1.0 — initial):
  - WATCHLIST sektion: add/remove/toggle/is_in/get/count.
  - USER PREFERENCES sektion: get (auto-create) + 3 update-funktioner.
  - FEATURE USAGE sektion: log + 2 stats-funktioner.

DESIGN-PRINCIPPER:
  - UI-agnostisk: Alle funktioner returnerer rene Python dicts/lists.
    Telegram-handlers er tynde adaptere; samme funktioner kan bruges af
    en fremtidig MiniApp-API uden ændringer.
  - Auto-create user_preferences: get_user_preferences() opretter
    automatisk en default-row hvis brugeren ikke har en — undgår
    NULL-checks i hver feature.
  - Fire-and-forget analytics: log_feature_usage() bruger asyncio.create_task
    så analytics aldrig blokerer en bruger-interaktion.
  - Atomiske operationer: toggle_watchlist() bruger en SQL-transaction
    så race conditions undgås.
  - JSONB-håndtering matcher database.py mønstret.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from database import _pool_ref

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Schema migration helper — tilføj cached_* kolonner hvis de mangler
# ══════════════════════════════════════════════════════════════════════════════

# Modulet-globalt flag så vi kun kører ALTER TABLE én gang per process-restart
_cached_columns_checked = False


async def _ensure_cached_columns_exist() -> None:
    """
    Tilføj cached_title, cached_year, cached_rating kolonner til
    user_watchlist tabellen hvis de ikke findes.

    Idempotent — bruger DO-blok så det er safe at køre flere gange.
    Kaldes automatisk ved første add_to_watchlist() / get_watchlist().
    """
    global _cached_columns_checked
    if _cached_columns_checked:
        return

    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='user_watchlist'
                    AND column_name='cached_title'
                ) THEN
                    ALTER TABLE user_watchlist ADD COLUMN cached_title TEXT;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='user_watchlist'
                    AND column_name='cached_year'
                ) THEN
                    ALTER TABLE user_watchlist ADD COLUMN cached_year INTEGER;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='user_watchlist'
                    AND column_name='cached_rating'
                ) THEN
                    ALTER TABLE user_watchlist ADD COLUMN cached_rating NUMERIC(3, 1);
                END IF;
            END $$;
            """
        )

    _cached_columns_checked = True
    logger.info("user_watchlist cached_* kolonner verificeret")


# ══════════════════════════════════════════════════════════════════════════════
# WATCHLIST — add/remove/toggle/check/list
# ══════════════════════════════════════════════════════════════════════════════

async def add_to_watchlist(
    telegram_id: int,
    tmdb_id: int,
    media_type: str,
    notes: str | None = None,
    cached_title: str | None = None,
    cached_year: int | None = None,
    cached_rating: float | None = None,
) -> bool:
    """
    Tilføj en titel til brugerens watchlist.

    Idempotent: Hvis titlen allerede er i listen, opdateres notes (hvis givet)
    OG cached_* felter (hvis givet) — men added_at bevares.

    Args:
      telegram_id:   Brugerens Telegram ID
      tmdb_id:       TMDB ID på filmen/serien
      media_type:    'movie' eller 'tv'
      notes:         Valgfri brugernote
      cached_title:  Titel fra Plex/TMDB (gemmes for hurtig visning)
      cached_year:   Udgivelsesår
      cached_rating: Rating (0.0 - 10.0)

    Returns:
      True hvis nyt entry oprettet, False hvis opdateret/allerede der.
    """
    if media_type not in ("movie", "tv"):
        logger.warning("add_to_watchlist: ugyldig media_type='%s'", media_type)
        return False

    # Tilføj cached_* kolonner hvis de mangler
    await _ensure_cached_columns_exist()

    # Filtrér "Ukendt" placeholder ud så vi ikke gemmer dem
    if cached_title and cached_title.strip().lower() in ("ukendt", "unknown", ""):
        cached_title = None

    async with _pool_ref().acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO user_watchlist (
                telegram_id, tmdb_id, media_type, notes,
                cached_title, cached_year, cached_rating
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (telegram_id, tmdb_id, media_type) DO UPDATE
                SET notes         = COALESCE(EXCLUDED.notes,         user_watchlist.notes),
                    cached_title  = COALESCE(EXCLUDED.cached_title,  user_watchlist.cached_title),
                    cached_year   = COALESCE(EXCLUDED.cached_year,   user_watchlist.cached_year),
                    cached_rating = COALESCE(EXCLUDED.cached_rating, user_watchlist.cached_rating)
            """,
            telegram_id, tmdb_id, media_type, notes,
            cached_title, cached_year, cached_rating,
        )

    # asyncpg returnerer "INSERT 0 1" for ny row, "INSERT 0 0" hvis kun update
    is_new = result.endswith(" 1")

    if is_new:
        logger.info(
            "watchlist add: telegram_id=%s tmdb_id=%s media=%s title=%r",
            telegram_id, tmdb_id, media_type, cached_title,
        )

    return is_new


async def remove_from_watchlist(
    telegram_id: int,
    tmdb_id: int,
    media_type: str,
) -> bool:
    """
    Fjern en titel fra brugerens watchlist.

    Returns:
      True hvis titlen blev fjernet, False hvis den ikke var i listen.
    """
    async with _pool_ref().acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM user_watchlist
            WHERE telegram_id = $1 AND tmdb_id = $2 AND media_type = $3
            """,
            telegram_id, tmdb_id, media_type,
        )

    removed = result.endswith(" 1")
    if removed:
        logger.info(
            "watchlist remove: telegram_id=%s tmdb_id=%s media=%s",
            telegram_id, tmdb_id, media_type,
        )
    return removed


async def is_in_watchlist(
    telegram_id: int,
    tmdb_id: int,
    media_type: str,
) -> bool:
    """Hurtigt tjek om en titel er i brugerens watchlist."""
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM user_watchlist
            WHERE telegram_id = $1 AND tmdb_id = $2 AND media_type = $3
            LIMIT 1
            """,
            telegram_id, tmdb_id, media_type,
        )
    return row is not None


async def toggle_watchlist(
    telegram_id: int,
    tmdb_id: int,
    media_type: str,
) -> str:
    """
    Toggle en titel i watchlist — atomisk operation.

    Hvis titlen IKKE er i listen → tilføj den, returnér 'added'.
    Hvis titlen ER i listen → fjern den, returnér 'removed'.

    Bruger en SQL-transaction for at undgå race conditions hvis brugeren
    trykker hurtigt 2× på toggle-knappen i Telegram.

    Args:
      telegram_id: Brugerens Telegram ID
      tmdb_id:     TMDB ID
      media_type:  'movie' eller 'tv'

    Returns:
      'added' hvis titlen blev tilføjet, 'removed' hvis fjernet.
      'error' ved ugyldig input eller DB-fejl.
    """
    if media_type not in ("movie", "tv"):
        logger.warning("toggle_watchlist: ugyldig media_type='%s'", media_type)
        return "error"

    async with _pool_ref().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT 1 FROM user_watchlist
                WHERE telegram_id = $1 AND tmdb_id = $2 AND media_type = $3
                FOR UPDATE
                """,
                telegram_id, tmdb_id, media_type,
            )

            if existing:
                await conn.execute(
                    """
                    DELETE FROM user_watchlist
                    WHERE telegram_id = $1 AND tmdb_id = $2 AND media_type = $3
                    """,
                    telegram_id, tmdb_id, media_type,
                )
                logger.info(
                    "watchlist toggle→removed: telegram_id=%s tmdb_id=%s media=%s",
                    telegram_id, tmdb_id, media_type,
                )
                return "removed"
            else:
                await conn.execute(
                    """
                    INSERT INTO user_watchlist (telegram_id, tmdb_id, media_type)
                    VALUES ($1, $2, $3)
                    """,
                    telegram_id, tmdb_id, media_type,
                )
                logger.info(
                    "watchlist toggle→added: telegram_id=%s tmdb_id=%s media=%s",
                    telegram_id, tmdb_id, media_type,
                )
                return "added"


async def get_watchlist(
    telegram_id: int,
    limit: int = 50,
    media_type: str | None = None,
) -> list[dict]:
    """
    Hent brugerens watchlist sorteret efter nyeste først.

    v0.2.0: Returnerer nu også cached_title, cached_year, cached_rating.

    Args:
      telegram_id: Brugerens Telegram ID
      limit:       Max antal entries (default 50)
      media_type:  Valgfri filter — 'movie', 'tv' eller None for alle

    Returns:
      Liste af dicts med tmdb_id, media_type, added_at, notes,
      cached_title, cached_year, cached_rating.
      Tom liste hvis brugeren intet har gemt.
    """
    # Tilføj cached_* kolonner hvis de mangler (idempotent)
    await _ensure_cached_columns_exist()

    where_parts = ["telegram_id = $1"]
    params: list = [telegram_id]

    if media_type in ("movie", "tv"):
        where_parts.append("media_type = $2")
        params.append(media_type)

    where_clause = " AND ".join(where_parts)
    params.append(limit)

    sql = f"""
        SELECT tmdb_id, media_type, added_at, notes,
               cached_title, cached_year, cached_rating
        FROM user_watchlist
        WHERE {where_clause}
        ORDER BY added_at DESC
        LIMIT ${len(params)}
    """

    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(row) for row in rows]


async def count_watchlist(
    telegram_id: int,
    media_type: str | None = None,
) -> int:
    """
    Tæl antal entries i brugerens watchlist.

    Args:
      media_type: Valgfri filter — 'movie', 'tv' eller None for total
    """
    where_parts = ["telegram_id = $1"]
    params: list = [telegram_id]

    if media_type in ("movie", "tv"):
        where_parts.append("media_type = $2")
        params.append(media_type)

    where_clause = " AND ".join(where_parts)
    sql = f"SELECT COUNT(*) AS cnt FROM user_watchlist WHERE {where_clause}"

    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(sql, *params)

    return row["cnt"] if row else 0


# ══════════════════════════════════════════════════════════════════════════════
# USER PREFERENCES — favorites + notification settings (auto-create row)
# ══════════════════════════════════════════════════════════════════════════════

# Default values brugt ved auto-create af user_preferences row
_DEFAULT_NOTIFICATION_SETTINGS: dict = {
    "new_movies":    True,   # Notification når en bestilt film er klar
    "new_episodes":  True,   # Notification når nye episoder af en serie kommer
    "weekly_digest": False,  # Ugentlig oversigt af nye titler (Phase 2+)
}


async def get_user_preferences(telegram_id: int) -> dict:
    """
    Hent brugerens præferencer. Opretter auto en default-row hvis ikke findes.

    Auto-create giver os mulighed for at undgå NULL-checks overalt — alle
    features kan stole på at preferences eksisterer.

    Returns:
      Dict med:
        - favorite_genres:        list[str]
        - favorite_actors:        list[str]
        - notification_settings:  dict (parsed fra JSONB)
    """
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_preferences (telegram_id, notification_settings)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (telegram_id) DO NOTHING
            RETURNING telegram_id
            """,
            telegram_id,
            json.dumps(_DEFAULT_NOTIFICATION_SETTINGS),
        )

        # Hvis INSERT ikke gjorde noget (already exists), så fetch
        prefs = await conn.fetchrow(
            """
            SELECT favorite_genres, favorite_actors, notification_settings
            FROM user_preferences
            WHERE telegram_id = $1
            """,
            telegram_id,
        )

    if prefs is None:
        # Edge case: hvis INSERT lige fejlede og der stadig ikke er en row
        return {
            "favorite_genres":       [],
            "favorite_actors":       [],
            "notification_settings": dict(_DEFAULT_NOTIFICATION_SETTINGS),
        }

    notif_raw = prefs["notification_settings"]
    if isinstance(notif_raw, str):
        try:
            notif_settings = json.loads(notif_raw)
        except json.JSONDecodeError:
            notif_settings = dict(_DEFAULT_NOTIFICATION_SETTINGS)
    elif isinstance(notif_raw, dict):
        notif_settings = notif_raw
    else:
        notif_settings = dict(_DEFAULT_NOTIFICATION_SETTINGS)

    return {
        "favorite_genres":       list(prefs["favorite_genres"] or []),
        "favorite_actors":       list(prefs["favorite_actors"] or []),
        "notification_settings": notif_settings,
    }


async def update_favorite_genres(
    telegram_id: int,
    genres: list[str],
) -> None:
    """Opdatér listen af favoritgenrer (overskriver eksisterende)."""
    await get_user_preferences(telegram_id)  # Sikrer row eksisterer

    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE user_preferences
            SET favorite_genres = $2,
                updated_at = NOW()
            WHERE telegram_id = $1
            """,
            telegram_id, genres,
        )

    logger.info(
        "user_preferences: opdaterede favorite_genres for telegram_id=%s (%d genrer)",
        telegram_id, len(genres),
    )


async def update_favorite_actors(
    telegram_id: int,
    actors: list[str],
) -> None:
    """Opdatér listen af favoritskuespillere (overskriver eksisterende)."""
    await get_user_preferences(telegram_id)

    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE user_preferences
            SET favorite_actors = $2,
                updated_at = NOW()
            WHERE telegram_id = $1
            """,
            telegram_id, actors,
        )

    logger.info(
        "user_preferences: opdaterede favorite_actors for telegram_id=%s (%d skuespillere)",
        telegram_id, len(actors),
    )


async def update_notification_settings(
    telegram_id: int,
    settings: dict,
) -> None:
    """
    Merge-style opdatering af notification_settings JSONB felt.

    Eksisterende keys bevares hvis ikke i 'settings' parameter.
    Nye keys tilføjes.
    """
    current = await get_user_preferences(telegram_id)
    merged = {**current["notification_settings"], **settings}

    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE user_preferences
            SET notification_settings = $2::jsonb,
                updated_at = NOW()
            WHERE telegram_id = $1
            """,
            telegram_id, json.dumps(merged),
        )

    logger.info(
        "user_preferences: opdaterede notification_settings for telegram_id=%s",
        telegram_id,
    )


async def get_notification_setting(
    telegram_id: int,
    key: str,
    default: bool = False,
) -> bool:
    """
    Hurtig læsning af én notification-indstilling.

    Bruges fx i webhook_service.py:
      if await get_notification_setting(user_id, "new_movies", default=True):
          await send_notification(...)

    Returns:
      bool værdi for nøglen, eller default hvis nøglen ikke findes.
    """
    prefs = await get_user_preferences(telegram_id)
    return prefs["notification_settings"].get(key, default)


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE USAGE — analytics (fire-and-forget)
# ══════════════════════════════════════════════════════════════════════════════

async def _log_feature_usage_inner(
    telegram_id: int,
    feature: str,
    action: str | None,
    metadata: dict | None,
) -> None:
    """
    Intern implementation — den faktiske DB-skrivning.

    Wrapped i try/except for at sikre at fire-and-forget aldrig
    propagerer exceptions tilbage til event-loopet.
    """
    try:
        async with _pool_ref().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO feature_usage (telegram_id, feature, action, metadata)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                telegram_id,
                feature,
                action,
                json.dumps(metadata) if metadata else None,
            )
    except Exception as e:
        # Vi swallow'er fejlen bevidst — analytics må ALDRIG ødelægge
        # en bruger-interaktion.
        logger.warning(
            "log_feature_usage failed (silent): feature=%s telegram_id=%s err=%s",
            feature, telegram_id, e,
        )


def log_feature_usage(
    telegram_id: int,
    feature: str,
    action: str | None = None,
    metadata: dict | None = None,
) -> None:
    """
    Log en feature-brug. Fire-and-forget — venter ikke på DB.

    NOTE: Dette er en SYNKRON funktion (ikke async!) der internt opretter
    en async-task. Det betyder du kan kalde den fra både sync og async
    kontekst uden 'await':

        log_feature_usage(user_id, "watchlist", "add", {"tmdb_id": 27205})

    Args:
      telegram_id: Brugerens Telegram ID
      feature:     Feature-navn (fx "watchlist", "recommendations", "archaeologist")
      action:      Specifik handling (fx "add", "remove", "view", "dismiss")
      metadata:    Valgfri ekstra kontekst som JSONB (fx {"tmdb_id": 12345})
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_log_feature_usage_inner(
            telegram_id, feature, action, metadata,
        ))
    except RuntimeError:
        # Ingen running loop (sync context) — log advarsel og dropp
        logger.debug(
            "log_feature_usage: ingen async loop, dropper event "
            "feature=%s telegram_id=%s",
            feature, telegram_id,
        )


async def get_feature_usage_stats(
    telegram_id: int | None = None,
    days: int = 30,
) -> dict:
    """
    Hent aggregerede feature usage stats.

    Args:
      telegram_id: Hvis None, aggregerer på tværs af alle brugere
      days:        Lookback-periode i dage (default 30)

    Returns:
      Dict med:
        - period_days:    int
        - total_events:   int
        - by_feature:     dict[str, int]
        - by_action:      dict[str, int]
        - unique_users:   int (hvis telegram_id is None)
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    where_parts = ["used_at >= $1"]
    params: list = [cutoff]

    if telegram_id is not None:
        where_parts.append("telegram_id = $2")
        params.append(telegram_id)

    where_clause = " AND ".join(where_parts)

    async with _pool_ref().acquire() as conn:
        # Per-feature counts
        feature_rows = await conn.fetch(
            f"""
            SELECT feature, COUNT(*) AS cnt
            FROM feature_usage
            WHERE {where_clause}
            GROUP BY feature
            ORDER BY cnt DESC
            """,
            *params,
        )

        # Per-action counts
        action_rows = await conn.fetch(
            f"""
            SELECT action, COUNT(*) AS cnt
            FROM feature_usage
            WHERE {where_clause} AND action IS NOT NULL
            GROUP BY action
            ORDER BY cnt DESC
            """,
            *params,
        )

        # Unique users (kun relevant når aggregating på tværs)
        unique_users = 0
        if telegram_id is None:
            users_row = await conn.fetchrow(
                f"""
                SELECT COUNT(DISTINCT telegram_id) AS cnt
                FROM feature_usage
                WHERE {where_clause}
                """,
                *params,
            )
            unique_users = users_row["cnt"] if users_row else 0

    by_feature = {row["feature"]: row["cnt"] for row in feature_rows}
    by_action = {row["action"]: row["cnt"] for row in action_rows}
    total = sum(by_feature.values())

    return {
        "period_days":  days,
        "total_events": total,
        "by_feature":   by_feature,
        "by_action":    by_action,
        "unique_users": unique_users,
    }


async def get_user_feature_usage(
    telegram_id: int,
    days: int = 30,
) -> dict:
    """
    Hent feature usage for én specifik bruger.

    Kan bruges til at lave personlige stats ("Du har brugt watchlist 12 gange
    denne måned") eller til debugging af bruger-rapporterede problemer.

    Returns:
      Dict med:
        - period_days:  int
        - total_events: int
        - by_feature:   dict[str, int]
        - first_event:  datetime | None
        - last_event:   datetime | None
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT feature, COUNT(*) AS cnt
            FROM feature_usage
            WHERE telegram_id = $1 AND used_at >= $2
            GROUP BY feature
            ORDER BY cnt DESC
            """,
            telegram_id, cutoff,
        )

        bounds_row = await conn.fetchrow(
            """
            SELECT MIN(used_at) AS first_event, MAX(used_at) AS last_event
            FROM feature_usage
            WHERE telegram_id = $1 AND used_at >= $2
            """,
            telegram_id, cutoff,
        )

    by_feature = {row["feature"]: row["cnt"] for row in rows}
    total = sum(by_feature.values())

    return {
        "period_days":  days,
        "total_events": total,
        "by_feature":   by_feature,
        "first_event":  bounds_row["first_event"] if bounds_row else None,
        "last_event":   bounds_row["last_event"]  if bounds_row else None,
    }