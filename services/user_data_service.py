"""
services/user_data_service.py - Data access layer for Buddy 2.0 user data.

Dette service-modul wrapper alle queries til de tre Buddy 2.0 foundation
tabeller (user_watchlist, user_preferences, feature_usage). Ingen
feature-handler bør skrive rå SQL — alle data-operationer går gennem dette lag.

DESIGN-PRINCIPPER:
  - UI-agnostisk: Alle funktioner returnerer rene Python dicts/lists.
    Telegram-handlers er tynde adaptere; samme funktioner kan bruges af
    en fremtidig MiniApp-API uden ændringer.
  - Auto-create user_preferences: get_user_preferences() opretter
    automatisk en default-row hvis brugeren ikke har en — undgår
    NULL-checks i hver feature.
  - Fire-and-forget analytics: log_feature_usage() bruger asyncio.create_task
    så analytics aldrig blokerer en bruger-interaktion. Hvis loggen fejler,
    mister vi det data-punkt — men brugeren mærker ingen latency.
  - Atomiske operationer: toggle_watchlist() bruger en SQL-transaction
    så race conditions undgås når brugere trykker hurtigt 2× på en knap.
  - JSONB-håndtering matcher database.py mønstret (asyncpg returnerer
    JSONB som str → vi parser med json.loads).

CHANGES (v0.1.0 — initial):
  - WATCHLIST sektion: add/remove/toggle/is_in/get/count.
  - USER PREFERENCES sektion: get (auto-create) + 3 update-funktioner.
  - FEATURE USAGE sektion: log + 2 stats-funktioner.
  - Achievements API droppet bevidst — tabellen ligger klar i database.py
    men funktioner bygges først når vi har konkrete use-cases (Phase 3+).

UNCHANGED:
  - Bruger samme connection pool som database.py via _pool_ref().
  - Ingen circular imports — importerer kun fra database.py.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from database import _pool_ref

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# WATCHLIST — add/remove/toggle/check/list
# ══════════════════════════════════════════════════════════════════════════════

async def add_to_watchlist(
    telegram_id: int,
    tmdb_id: int,
    media_type: str,
    notes: str | None = None,
) -> bool:
    """
    Tilføj en titel til brugerens watchlist.

    Idempotent: Hvis titlen allerede er i listen, opdateres notes (hvis givet)
    men added_at bevares. Returnerer True hvis ny tilføjelse, False hvis
    allerede eksisterede.

    Args:
      telegram_id: Brugerens Telegram ID
      tmdb_id:     TMDB ID på filmen/serien
      media_type:  'movie' eller 'tv'
      notes:       Valgfri brugernote (fx "Skal ses med kæresten")

    Returns:
      True hvis nyt entry oprettet, False hvis opdateret/allerede der.
    """
    if media_type not in ("movie", "tv"):
        logger.warning("add_to_watchlist: ugyldig media_type='%s'", media_type)
        return False

    async with _pool_ref().acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO user_watchlist (telegram_id, tmdb_id, media_type, notes)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id, tmdb_id, media_type) DO UPDATE
                SET notes = COALESCE(EXCLUDED.notes, user_watchlist.notes)
            """,
            telegram_id, tmdb_id, media_type, notes,
        )

    # asyncpg returnerer "INSERT 0 1" for ny row, "INSERT 0 0" hvis kun update
    is_new = result.endswith(" 1")

    if is_new:
        logger.info(
            "watchlist add: telegram_id=%s tmdb_id=%s media=%s",
            telegram_id, tmdb_id, media_type,
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
            # Tjek nuværende state inden for transaction
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

    Args:
      telegram_id: Brugerens Telegram ID
      limit:       Max antal entries (default 50)
      media_type:  Valgfri filter — 'movie', 'tv' eller None for alle

    Returns:
      Liste af dicts med tmdb_id, media_type, added_at, notes.
      Tom liste hvis brugeren intet har gemt.
    """
    where_parts = ["telegram_id = $1"]
    params: list = [telegram_id]

    if media_type in ("movie", "tv"):
        where_parts.append("media_type = $2")
        params.append(media_type)

    where_clause = " AND ".join(where_parts)
    params.append(limit)

    sql = f"""
        SELECT tmdb_id, media_type, added_at, notes
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
        - telegram_id: int
        - favorite_genres: list[str]
        - favorite_actors: list[dict]  (fx [{"name": "Tom Hanks", "tmdb_id": 31}])
        - notification_settings: dict
        - computed_at: datetime | None
        - updated_at: datetime
    """
    async with _pool_ref().acquire() as conn:
        # Forsøg upsert med defaults — RETURNING giver os den endelige row
        row = await conn.fetchrow(
            """
            INSERT INTO user_preferences (
                telegram_id, favorite_genres, favorite_actors, notification_settings
            )
            VALUES ($1, '[]'::jsonb, '[]'::jsonb, $2::jsonb)
            ON CONFLICT (telegram_id) DO UPDATE
                SET telegram_id = EXCLUDED.telegram_id  -- no-op, men returnerer row
            RETURNING *
            """,
            telegram_id,
            json.dumps(_DEFAULT_NOTIFICATION_SETTINGS),
        )

    result = dict(row)

    # asyncpg returnerer JSONB som str — parse til Python objects
    for key in ("favorite_genres", "favorite_actors", "notification_settings"):
        if isinstance(result.get(key), str):
            try:
                result[key] = json.loads(result[key])
            except json.JSONDecodeError:
                logger.warning(
                    "get_user_preferences: kunne ikke parse %s for telegram_id=%s",
                    key, telegram_id,
                )
                result[key] = [] if key != "notification_settings" else {}

    return result


async def update_favorite_genres(
    telegram_id: int,
    genres: list[str],
) -> None:
    """
    Opdater brugerens favorit-genrer.

    Bruges af "🎯 Anbefalet til mig" feature (Sprint 3) til at gemme
    de top-3 genrer der er computet fra Tautulli-historik.
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_preferences (
                telegram_id, favorite_genres, notification_settings
            )
            VALUES ($1, $2::jsonb, $3::jsonb)
            ON CONFLICT (telegram_id) DO UPDATE
                SET favorite_genres = EXCLUDED.favorite_genres,
                    computed_at     = NOW(),
                    updated_at      = NOW()
            """,
            telegram_id,
            json.dumps(genres),
            json.dumps(_DEFAULT_NOTIFICATION_SETTINGS),
        )

    logger.info(
        "preferences update: telegram_id=%s favorite_genres=%s",
        telegram_id, genres,
    )


async def update_favorite_actors(
    telegram_id: int,
    actors: list[dict],
) -> None:
    """
    Opdater brugerens favorit-skuespillere.

    Args:
      actors: Liste af dicts med fx [{"name": "Tom Hanks", "tmdb_id": 31}]
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_preferences (
                telegram_id, favorite_actors, notification_settings
            )
            VALUES ($1, $2::jsonb, $3::jsonb)
            ON CONFLICT (telegram_id) DO UPDATE
                SET favorite_actors = EXCLUDED.favorite_actors,
                    updated_at      = NOW()
            """,
            telegram_id,
            json.dumps(actors),
            json.dumps(_DEFAULT_NOTIFICATION_SETTINGS),
        )

    logger.info(
        "preferences update: telegram_id=%s favorite_actors_count=%d",
        telegram_id, len(actors),
    )


async def update_notification_settings(
    telegram_id: int,
    settings: dict,
) -> None:
    """
    Opdater brugerens notifikations-indstillinger.

    Merger med eksisterende settings — kun de nøgler der sendes opdateres.
    Bruges når brugeren toggler en notifikation til/fra i UI.

    Eksempel:
      update_notification_settings(123, {"new_movies": False})
      # Lader new_episodes og weekly_digest være uændrede.
    """
    if not isinstance(settings, dict):
        logger.warning(
            "update_notification_settings: settings skal være dict, fik %s",
            type(settings).__name__,
        )
        return

    # Hent eksisterende settings og merge
    current = await get_user_preferences(telegram_id)
    merged_settings = {**current["notification_settings"], **settings}

    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE user_preferences
            SET notification_settings = $1::jsonb,
                updated_at            = NOW()
            WHERE telegram_id = $2
            """,
            json.dumps(merged_settings),
            telegram_id,
        )

    logger.info(
        "notification settings update: telegram_id=%s changes=%s",
        telegram_id, settings,
    )


async def get_notification_setting(
    telegram_id: int,
    key: str,
    default: bool | None = None,
) -> bool | None:
    """
    Convenience helper — hent én specifik notifikations-setting.

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
        # en bruger-interaktion. Vi logger det dog for at kunne debugge.
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

    Hvis logging fejler internt, swallow'es exception så bruger-interaktionen
    aldrig blokeres. Mistede data-punkter er en acceptabel pris for hastighed.
    """
    try:
        asyncio.create_task(
            _log_feature_usage_inner(telegram_id, feature, action, metadata)
        )
    except RuntimeError:
        # Hvis vi kaldes uden et kørende event-loop (fx i tests), ignorer
        logger.debug(
            "log_feature_usage: ingen event-loop, springer over (feature=%s)",
            feature,
        )


async def get_feature_usage_stats(days: int = 30) -> dict:
    """
    Hent global feature-statistik for de sidste N dage.

    Bruges af /usage_stats admin-kommando (kommer senere) til at se
    hvilke features der bruges hvor meget. KRITISK input til prioritering.

    Args:
      days: Antal dage tilbage at aggregere (default 30)

    Returns:
      Dict med:
        - period_days:  int
        - total_events: int
        - by_feature:   dict[str, int]   (feature_name → count)
        - by_action:    dict[str, dict]  (feature_name → {action → count})
        - active_users: int              (unikke telegram_ids)
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT feature, action, COUNT(*) AS cnt
            FROM feature_usage
            WHERE used_at >= $1
            GROUP BY feature, action
            ORDER BY feature, action
            """,
            cutoff,
        )

        active_users_row = await conn.fetchrow(
            """
            SELECT COUNT(DISTINCT telegram_id) AS cnt
            FROM feature_usage
            WHERE used_at >= $1
            """,
            cutoff,
        )

    by_feature: dict[str, int]            = {}
    by_action:  dict[str, dict[str, int]] = {}
    total = 0

    for row in rows:
        feature = row["feature"]
        action  = row["action"] or "(none)"
        cnt     = row["cnt"]

        by_feature[feature] = by_feature.get(feature, 0) + cnt
        by_action.setdefault(feature, {})[action] = cnt
        total += cnt

    return {
        "period_days":  days,
        "total_events": total,
        "by_feature":   by_feature,
        "by_action":    by_action,
        "active_users": active_users_row["cnt"] if active_users_row else 0,
    }


async def get_user_feature_usage(
    telegram_id: int,
    days: int = 30,
) -> dict:
    """
    Hent feature-brug for én specifik bruger.

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