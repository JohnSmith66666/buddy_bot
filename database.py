"""
database.py - PostgreSQL connection pool, table management,
user whitelist, Plex username storage, onboarding state, interaktionshistorik,
pending requests, TMDB metadata-cache, feedback-system OG Buddy 2.0 foundation.

CHANGES (v0.15.0 — Buddy 2.0 foundation tables):
  - NY: setup_user_watchlist_table() — bruger-watchlist (telegram_id, tmdb_id,
    media_type, added_at, notes). Foundation for "📺 Min watchlist" feature.
    Composite primary key (telegram_id, tmdb_id, media_type) sikrer ingen
    duplikater. Indekseret på telegram_id for hurtig listing.

  - NY: setup_user_preferences_table() — samlet bruger-præferencer
    (favorite_genres, favorite_actors, notification_settings) som JSONB.
    Bruges af "🎯 Anbefalet til mig" + fremtidige personalisering.

  - NY: setup_user_achievements_table() — forberedelse til Phase 2/3
    achievement-system. Tabellen er klar, men ingen kode bruger den endnu.
    Bygges nu fordi det er gratis og undgår migration senere.

  - NY: setup_feature_usage_table() — analytics/tracking af hvilke features
    bruges hvor meget. KRITISK for at vide hvor man skal investere tid.
    Indekseret på (feature, used_at DESC) for hurtige aggregeringer.

  - NY: ALLE setup_*_table() kalder samles nu i setup_db() for konsistens.
    Tidligere blev setup_pending_requests, setup_tmdb_metadata_table og
    setup_feedback_table kaldt fra main.py's on_startup. Nu er det ét sted.
    main.py SKAL opdateres til ikke længere at kalde dem separat (de virker
    stadig idempotent, så ingen breaking change ved overgang).

  - PERFORMANCE: Pool-størrelse hævet fra max_size=10 → max_size=20.
    Forberedelse til 100 brugere. Railway PostgreSQL kan håndtere det.
    min_size=2 → min_size=5 for færre cold-start latency.

  - 100% BAGUDKOMPATIBEL: Alle eksisterende funktioner uændrede. Ingen
    eksisterende tabeller røres. Kun TILFØJELSER.

UNCHANGED (v0.14.2 — Batch B bulk-actions + thread-tracking):
  - update_feedback_status_bulk(ids, status) → int
  - parse_id_range(spec) → list[int]

UNCHANGED (v0.14.1 — first-time tester detection):
  - is_first_time_feedback(telegram_id) → bool

UNCHANGED (v0.14.0 — feedback system):
  - feedback tabel + alle funktioner (submit_feedback, list_feedback,
    get_feedback, update_feedback_status, add_admin_reply,
    count_feedback_by_status).

UNCHANGED (v0.13.0 — media-aware subgenre lookup):
  - find_titles_by_subgenre, find_films_by_subgenre legacy wrapper,
    count_titles_by_subgenre.

UNCHANGED (v0.11.0 — P0/P1 performance pakke):
  - log_message statistical DELETE.
  - CTE-baseret subgenre query.

UNCHANGED (v0.10.8 — Etape 1 af subgenre-projekt):
  - GIN-index på keywords-kolonnen.
  - Smart blanding på Python-side.

UNCHANGED (v0.10.6 — TMDB metadata cache):
  - tmdb_metadata tabel + GIN-indekser.
"""

import json
import logging
import random
from datetime import datetime, timezone

import asyncpg

from config import DATABASE_URL, LOG_HISTORY_LIMIT

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# ── P0-2: Probability for log cleanup ─────────────────────────────────────────
_LOG_CLEANUP_PROBABILITY = 0.1


# ══════════════════════════════════════════════════════════════════════════════
# Schema — Users + Logs
# ══════════════════════════════════════════════════════════════════════════════

_CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id      BIGINT PRIMARY KEY,
    telegram_name    TEXT,
    plex_username    TEXT,
    is_whitelisted   BOOLEAN     NOT NULL DEFAULT FALSE,
    onboarding_state TEXT,
    persona_id       TEXT        NOT NULL DEFAULT 'buddy',
    added_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_MIGRATE_ONBOARDING_STATE = """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS onboarding_state TEXT;
"""

_MIGRATE_PERSONA_ID = """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS persona_id TEXT DEFAULT 'buddy';
"""

_CREATE_INTERACTION_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS interaction_log (
    id           BIGSERIAL PRIMARY KEY,
    telegram_id  BIGINT      NOT NULL,
    direction    TEXT        NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    message_text TEXT,
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_log_telegram_id
    ON interaction_log (telegram_id, logged_at DESC);
"""


# ══════════════════════════════════════════════════════════════════════════════
# Schema — Pending requests
# ══════════════════════════════════════════════════════════════════════════════

_CREATE_PENDING_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS pending_requests (
    token        TEXT        PRIMARY KEY,
    telegram_id  BIGINT      NOT NULL,
    media_type   TEXT        NOT NULL,
    tmdb_id      INTEGER     NOT NULL,
    tvdb_id      INTEGER,
    title        TEXT        NOT NULL,
    year         INTEGER,
    genres       JSONB,
    original_language TEXT,
    season_numbers    JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_PENDING_INDEX = """
CREATE INDEX IF NOT EXISTS idx_pending_telegram_id
    ON pending_requests (telegram_id, created_at DESC);
"""


# ══════════════════════════════════════════════════════════════════════════════
# Schema — TMDB metadata cache
# ══════════════════════════════════════════════════════════════════════════════

_CREATE_TMDB_METADATA_TABLE = """
CREATE TABLE IF NOT EXISTS tmdb_metadata (
    tmdb_id        INTEGER     NOT NULL,
    media_type     TEXT        NOT NULL CHECK (media_type IN ('movie', 'tv')),
    title          TEXT,
    year           INTEGER,
    tmdb_genres    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    keywords       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    status         TEXT        NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'fetched', 'error', 'not_found')),
    error_message  TEXT,
    fetched_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tmdb_id, media_type)
);
"""

_CREATE_TMDB_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tmdb_metadata_status
    ON tmdb_metadata (status);
"""

_CREATE_TMDB_KEYWORDS_GIN = """
CREATE INDEX IF NOT EXISTS idx_tmdb_metadata_keywords
    ON tmdb_metadata USING GIN (keywords);
"""

_CREATE_TMDB_GENRES_GIN = """
CREATE INDEX IF NOT EXISTS idx_tmdb_metadata_genres
    ON tmdb_metadata USING GIN (tmdb_genres);
"""


# ══════════════════════════════════════════════════════════════════════════════
# Schema — Feedback (v0.14.0)
# ══════════════════════════════════════════════════════════════════════════════

_CREATE_FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS feedback (
    id               SERIAL PRIMARY KEY,
    telegram_id      BIGINT      NOT NULL,
    telegram_username TEXT,
    telegram_name    TEXT,
    feedback_type    TEXT        NOT NULL
                     CHECK (feedback_type IN ('idea', 'bug', 'question', 'praise')),
    message          TEXT        NOT NULL,
    screenshot_file_ids JSONB    NOT NULL DEFAULT '[]'::jsonb,
    status           TEXT        NOT NULL DEFAULT 'new'
                     CHECK (status IN ('new', 'seen', 'replied', 'resolved')),
    admin_reply      TEXT,
    admin_replied_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_FEEDBACK_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_feedback_status
    ON feedback (status, created_at DESC);
"""

_CREATE_FEEDBACK_TYPE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_feedback_type
    ON feedback (feedback_type, created_at DESC);
"""

_CREATE_FEEDBACK_USER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_feedback_telegram_id
    ON feedback (telegram_id, created_at DESC);
"""


# ══════════════════════════════════════════════════════════════════════════════
# Schema — Buddy 2.0 foundation tables (NYE i v0.15.0)
# ══════════════════════════════════════════════════════════════════════════════

# ── User watchlist ────────────────────────────────────────────────────────────
# Lagrer de titler en bruger har gemt for senere visning.
# Composite PK forhindrer duplikater per (bruger, titel, type).
_CREATE_USER_WATCHLIST_TABLE = """
CREATE TABLE IF NOT EXISTS user_watchlist (
    telegram_id  BIGINT      NOT NULL,
    tmdb_id      INTEGER     NOT NULL,
    media_type   TEXT        NOT NULL CHECK (media_type IN ('movie', 'tv')),
    added_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes        TEXT,
    PRIMARY KEY (telegram_id, tmdb_id, media_type)
);
"""

_CREATE_WATCHLIST_USER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_user_watchlist_telegram_id
    ON user_watchlist (telegram_id, added_at DESC);
"""


# ── User preferences ──────────────────────────────────────────────────────────
# Samlet brugerprofil med præferencer. JSONB-felter giver fleksibilitet til at
# tilføje nye præferencer uden migration.
#
# Eksempler på data:
#   favorite_genres:       ["Action", "Sci-Fi", "Comedy"]
#   favorite_actors:       [{"name": "Tom Hanks", "tmdb_id": 31}]
#   notification_settings: {"new_movies": true, "new_episodes": true,
#                           "weekly_digest": false}
_CREATE_USER_PREFERENCES_TABLE = """
CREATE TABLE IF NOT EXISTS user_preferences (
    telegram_id           BIGINT      PRIMARY KEY,
    favorite_genres       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    favorite_actors       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    notification_settings JSONB       NOT NULL DEFAULT '{}'::jsonb,
    computed_at           TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


# ── User achievements ─────────────────────────────────────────────────────────
# Forberedelse til Phase 2/3 achievement-system. Tabellen er klar, men ingen
# kode bruger den endnu. Bygges nu fordi schema-ændring senere er dyrt.
_CREATE_USER_ACHIEVEMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS user_achievements (
    telegram_id    BIGINT      NOT NULL,
    achievement_id TEXT        NOT NULL,
    unlocked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata       JSONB,
    PRIMARY KEY (telegram_id, achievement_id)
);
"""

_CREATE_ACHIEVEMENTS_USER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_user_achievements_telegram_id
    ON user_achievements (telegram_id, unlocked_at DESC);
"""


# ── Feature usage (analytics) ─────────────────────────────────────────────────
# Tracker hvilke features bruges hvor meget. KRITISK for at prioritere
# udvikling — vi vil ikke bruge tid på features ingen rør.
#
# Eksempel-rows:
#   feature='watchlist',       action='add',           metadata={"tmdb_id": 27205}
#   feature='recommendations', action='view',          metadata={}
#   feature='archaeologist',   action='dismiss_movie', metadata={"tmdb_id": 12345}
_CREATE_FEATURE_USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS feature_usage (
    id           BIGSERIAL PRIMARY KEY,
    telegram_id  BIGINT      NOT NULL,
    feature      TEXT        NOT NULL,
    action       TEXT,
    metadata     JSONB,
    used_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_FEATURE_USAGE_FEATURE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_feature_usage_feature
    ON feature_usage (feature, used_at DESC);
"""

_CREATE_FEATURE_USAGE_USER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_feature_usage_telegram_id
    ON feature_usage (telegram_id, used_at DESC);
"""


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle (v0.15.0 — alle setups samlet i setup_db)
# ══════════════════════════════════════════════════════════════════════════════

async def setup_db() -> None:
    """
    Initialise PostgreSQL connection pool og opret/opdater alle tabeller.

    v0.15.0: Alle setup_*_table() funktioner kaldes nu fra ÉT sted.
    main.py behøver ikke længere kalde dem separat fra on_startup() —
    de er alle idempotente, så det er sikkert hvis main.py stadig kalder
    dem (overgang uden breaking change).

    Pool-konfiguration tunet til 100 brugere:
      min_size=5  → færre cold-start latencies
      max_size=20 → headroom til peak load
    """
    global _pool
    logger.info("Connecting to PostgreSQL …")
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=5,
        max_size=20,
        command_timeout=30,
    )

    async with _pool.acquire() as conn:
        # ── Core tables ───────────────────────────────────────────────────────
        await conn.execute(_CREATE_USERS_TABLE)
        await conn.execute(_MIGRATE_ONBOARDING_STATE)
        await conn.execute(_MIGRATE_PERSONA_ID)
        await conn.execute(_CREATE_INTERACTION_LOG_TABLE)
        await conn.execute(_CREATE_LOG_INDEX)

        # ── Pending requests ──────────────────────────────────────────────────
        await conn.execute(_CREATE_PENDING_REQUESTS_TABLE)
        await conn.execute(_CREATE_PENDING_INDEX)

        # ── TMDB metadata cache ───────────────────────────────────────────────
        await conn.execute(_CREATE_TMDB_METADATA_TABLE)
        await conn.execute(_CREATE_TMDB_STATUS_INDEX)
        await conn.execute(_CREATE_TMDB_KEYWORDS_GIN)
        await conn.execute(_CREATE_TMDB_GENRES_GIN)

        # ── Feedback system ───────────────────────────────────────────────────
        await conn.execute(_CREATE_FEEDBACK_TABLE)
        await conn.execute(_CREATE_FEEDBACK_STATUS_INDEX)
        await conn.execute(_CREATE_FEEDBACK_TYPE_INDEX)
        await conn.execute(_CREATE_FEEDBACK_USER_INDEX)

        # ── Buddy 2.0 foundation (NY i v0.15.0) ──────────────────────────────
        await conn.execute(_CREATE_USER_WATCHLIST_TABLE)
        await conn.execute(_CREATE_WATCHLIST_USER_INDEX)
        await conn.execute(_CREATE_USER_PREFERENCES_TABLE)
        await conn.execute(_CREATE_USER_ACHIEVEMENTS_TABLE)
        await conn.execute(_CREATE_ACHIEVEMENTS_USER_INDEX)
        await conn.execute(_CREATE_FEATURE_USAGE_TABLE)
        await conn.execute(_CREATE_FEATURE_USAGE_FEATURE_INDEX)
        await conn.execute(_CREATE_FEATURE_USAGE_USER_INDEX)

    logger.info(
        "Database ready — pool(%d-%d) | tables: users, log, pending, tmdb_metadata, "
        "feedback, user_watchlist, user_preferences, user_achievements, feature_usage",
        5, 20,
    )


async def close_db() -> None:
    if _pool:
        await _pool.close()


def _pool_ref() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call setup_db() first.")
    return _pool


# ══════════════════════════════════════════════════════════════════════════════
# Bagudkompatibilitet — separate setup_*_table() funktioner
# ══════════════════════════════════════════════════════════════════════════════
# Disse funktioner kaldtes tidligere fra main.py's on_startup(). De gør nu
# ingenting (alt er allerede oprettet i setup_db) men beholdes så main.py
# ikke knækker hvis den stadig kalder dem under overgangen.
#
# main.py BØR opdateres til ikke at kalde dem længere, men det er ikke kritisk.

async def setup_pending_requests() -> None:
    """[LEGACY] No-op. Kaldes nu fra setup_db()."""
    logger.debug("setup_pending_requests() called — already done in setup_db()")


async def setup_tmdb_metadata_table() -> None:
    """[LEGACY] No-op. Kaldes nu fra setup_db()."""
    logger.debug("setup_tmdb_metadata_table() called — already done in setup_db()")


async def setup_feedback_table() -> None:
    """[LEGACY] No-op. Kaldes nu fra setup_db()."""
    logger.debug("setup_feedback_table() called — already done in setup_db()")


# ══════════════════════════════════════════════════════════════════════════════
# User helpers
# ══════════════════════════════════════════════════════════════════════════════

async def get_user(telegram_id: int) -> dict | None:
    """Return the full user row or None if not found."""
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1", telegram_id
        )
    return dict(row) if row else None


async def upsert_user(telegram_id: int, telegram_name: str | None = None) -> None:
    """Insert a new user (not whitelisted) or update their Telegram name."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, telegram_name)
            VALUES ($1, $2)
            ON CONFLICT (telegram_id) DO UPDATE
                SET telegram_name = COALESCE(EXCLUDED.telegram_name, users.telegram_name)
            """,
            telegram_id, telegram_name,
        )


async def is_whitelisted(telegram_id: int) -> bool:
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_whitelisted FROM users WHERE telegram_id = $1", telegram_id
        )
    return bool(row and row["is_whitelisted"])


async def approve_user(telegram_id: int) -> None:
    """
    Whitelist a user AND set onboarding_state='awaiting_plex' atomically.
    Used by admin approval callback.
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET is_whitelisted   = TRUE,
                onboarding_state = 'awaiting_plex'
            WHERE telegram_id = $1
            """,
            telegram_id,
        )
    logger.info("User %s approved (whitelisted + awaiting_plex)", telegram_id)


async def get_plex_username(telegram_id: int) -> str | None:
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT plex_username FROM users WHERE telegram_id = $1", telegram_id
        )
    return row["plex_username"] if row else None


async def set_plex_username(telegram_id: int, plex_username: str) -> None:
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET plex_username    = $1,
                onboarding_state = NULL
            WHERE telegram_id = $2
            """,
            plex_username, telegram_id,
        )
    logger.info("plex_username='%s' gemt for telegram_id=%s", plex_username, telegram_id)


async def get_persona(telegram_id: int) -> str:
    """Hent brugerens valgte persona_id. Falder tilbage til 'buddy'."""
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT persona_id FROM users WHERE telegram_id = $1", telegram_id
        )
    return (row["persona_id"] if row and row["persona_id"] else "buddy")


async def set_persona(telegram_id: int, persona_id: str) -> None:
    """Gem brugerens valgte persona_id."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            "UPDATE users SET persona_id = $1 WHERE telegram_id = $2",
            persona_id, telegram_id,
        )
    logger.info("persona_id='%s' gemt for telegram_id=%s", persona_id, telegram_id)


# ══════════════════════════════════════════════════════════════════════════════
# Onboarding state
# ══════════════════════════════════════════════════════════════════════════════

async def get_onboarding_state(telegram_id: int) -> str | None:
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT onboarding_state FROM users WHERE telegram_id = $1", telegram_id
        )
    return row["onboarding_state"] if row else None


async def set_onboarding_state(telegram_id: int, state: str | None) -> None:
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            "UPDATE users SET onboarding_state = $1 WHERE telegram_id = $2",
            state, telegram_id,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Interaction log (P0-2: Statistical cleanup)
# ══════════════════════════════════════════════════════════════════════════════

async def log_message(
    telegram_id: int,
    direction: str,
    message_text: str,
) -> None:
    """
    Log en besked til interaction_log tabellen.
    P0-2: DELETE kører kun statistisk hver 10. gang for at spare DB-load.
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO interaction_log (telegram_id, direction, message_text, logged_at)
            VALUES ($1, $2, $3, $4)
            """,
            telegram_id, direction, message_text, datetime.now(timezone.utc),
        )

        if random.random() < _LOG_CLEANUP_PROBABILITY:
            await conn.execute(
                """
                DELETE FROM interaction_log
                WHERE telegram_id = $1
                  AND id NOT IN (
                      SELECT id FROM interaction_log
                      WHERE telegram_id = $1
                      ORDER BY logged_at DESC
                      LIMIT $2
                  )
                """,
                telegram_id, LOG_HISTORY_LIMIT,
            )


async def get_all_whitelisted_users() -> list[dict]:
    """Return all whitelisted users with their plex_username and telegram_id."""
    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(
            "SELECT telegram_id, telegram_name, plex_username FROM users WHERE is_whitelisted = TRUE"
        )
    return [dict(row) for row in rows]


# ══════════════════════════════════════════════════════════════════════════════
# Pending requests
# ══════════════════════════════════════════════════════════════════════════════

async def save_pending_request(token: str, telegram_id: int, data: dict) -> None:
    """Save media details for a pending confirmation."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_requests
                (token, telegram_id, media_type, tmdb_id, tvdb_id, title, year,
                 genres, original_language, season_numbers)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (token) DO UPDATE SET
                telegram_id=EXCLUDED.telegram_id,
                media_type=EXCLUDED.media_type,
                tmdb_id=EXCLUDED.tmdb_id,
                tvdb_id=EXCLUDED.tvdb_id,
                title=EXCLUDED.title,
                year=EXCLUDED.year,
                genres=EXCLUDED.genres,
                original_language=EXCLUDED.original_language,
                season_numbers=EXCLUDED.season_numbers,
                created_at=NOW()
            """,
            token,
            telegram_id,
            data.get("media_type"),
            data.get("tmdb_id"),
            data.get("tvdb_id"),
            data.get("title"),
            data.get("year"),
            json.dumps(data.get("genres", [])),
            data.get("original_language", "en"),
            json.dumps(data.get("season_numbers", [])),
        )


async def get_pending_request(token: str) -> dict | None:
    """Retrieve and delete a pending request by token."""
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM pending_requests WHERE token=$1 RETURNING *", token
        )
    if not row:
        return None
    d = dict(row)
    d["genres"]         = json.loads(d["genres"])         if d["genres"]         else []
    d["season_numbers"] = json.loads(d["season_numbers"]) if d["season_numbers"] else []
    return d


async def delete_pending_requests_for_user(telegram_id: int) -> None:
    """Clean up all pending requests for a user (e.g. on cancel)."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            "DELETE FROM pending_requests WHERE telegram_id=$1", telegram_id
        )


# ══════════════════════════════════════════════════════════════════════════════
# TMDB metadata cache
# ══════════════════════════════════════════════════════════════════════════════

async def seed_tmdb_metadata(items: list[dict]) -> dict:
    """Seed tmdb_metadata med pending records. Idempotent."""
    if not items:
        return {"inserted": 0, "skipped": 0, "total_input": 0}

    insert_sql = """
        INSERT INTO tmdb_metadata (tmdb_id, media_type, title, year, status)
        VALUES ($1, $2, $3, $4, 'pending')
        ON CONFLICT (tmdb_id, media_type) DO NOTHING
    """

    inserted = 0
    async with _pool_ref().acquire() as conn:
        async with conn.transaction():
            for item in items:
                tmdb_id = item.get("tmdb_id")
                if not tmdb_id:
                    continue
                result = await conn.execute(
                    insert_sql,
                    tmdb_id,
                    item.get("media_type"),
                    item.get("title"),
                    item.get("year"),
                )
                if result.endswith(" 1"):
                    inserted += 1

    skipped = len(items) - inserted
    logger.info("seed_tmdb_metadata: %d inserted, %d skipped", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped, "total_input": len(items)}


async def get_metadata_status() -> dict:
    """Returnér optælling af records pr. status."""
    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT media_type, status, COUNT(*) AS cnt
            FROM tmdb_metadata
            GROUP BY media_type, status
            """
        )

    by_media: dict[str, dict[str, int]] = {
        "movie": {"pending": 0, "fetched": 0, "error": 0, "not_found": 0},
        "tv":    {"pending": 0, "fetched": 0, "error": 0, "not_found": 0},
    }
    total_by_status = {"pending": 0, "fetched": 0, "error": 0, "not_found": 0}

    for row in rows:
        media  = row["media_type"]
        status = row["status"]
        cnt    = row["cnt"]
        if media in by_media and status in by_media[media]:
            by_media[media][status] = cnt
            total_by_status[status] += cnt

    return {
        "by_media_type":   by_media,
        "by_status":       total_by_status,
        "total":           sum(total_by_status.values()),
    }


async def get_pending_metadata_items(limit: int = 100) -> list[dict]:
    """Hent pending items klar til TMDB-fetch. Sorteret ældste først."""
    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tmdb_id, media_type, title, year
            FROM tmdb_metadata
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
    return [dict(row) for row in rows]


async def update_tmdb_metadata(
    tmdb_id: int,
    media_type: str,
    keywords: list[str],
    tmdb_genres: list[str],
    status: str = "fetched",
    error_message: str | None = None,
) -> None:
    """Opdater en metadata-record efter TMDB-fetch."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE tmdb_metadata
            SET keywords      = $1::jsonb,
                tmdb_genres   = $2::jsonb,
                status        = $3,
                error_message = $4,
                fetched_at    = NOW()
            WHERE tmdb_id = $5 AND media_type = $6
            """,
            json.dumps(keywords),
            json.dumps(tmdb_genres),
            status,
            error_message,
            tmdb_id,
            media_type,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Subgenre lookup — Plex/TMDB genre mapping
# ═════════════════════════════════════════════════════════════════════════════

# Mapping fra dansk Plex-genre til engelske TMDB-genrer.
# v0.15.1 (FIX): Genoprettet danske mapping efter regression i tidligere
# session. Plex-server SKYNET bruger danske genre-navne (Komedie, Gyser,
# Kriminalitet, etc.) men tmdb_metadata.tmdb_genres indeholder engelske
# værdier fra TMDB API. Uden denne mapping ville filteret matche 0 hits
# for danske subgenrer som crime_mafia (Kriminalitet → Crime).
_PLEX_TO_TMDB_GENRE = {
    "Komedie":         ["Comedy"],
    "Gyser":           ["Horror"],
    "Kriminalitet":    ["Crime"],
    "Familie":         ["Family"],
    "Romantik":        ["Romance"],
    "Krig":            ["War", "War & Politics"],
    "Mysterium":       ["Mystery"],
    "Musik":           ["Music"],
    "Historie":        ["History"],
    "Action":          ["Action"],
    "Drama":           ["Drama"],
    "Thriller":        ["Thriller"],
    "Adventure":       ["Adventure"],
    "Fantasy":         ["Fantasy"],
    "Sci-fi":          ["Science Fiction", "Sci-Fi & Fantasy"],
    "Science Fiction": ["Science Fiction", "Sci-Fi & Fantasy"],
    "Animation":       ["Animation"],
    "Documentary":     ["Documentary"],
    "Western":         ["Western"],
    "Biography":       ["Drama"],
    "Musical":         ["Music"],
}


def _plex_genre_to_tmdb(plex_genre: str) -> list[str]:
    """Konverter et Plex-genre-navn til en liste af tilsvarende TMDB-genrer."""
    return _PLEX_TO_TMDB_GENRE.get(plex_genre, [plex_genre]))


# ══════════════════════════════════════════════════════════════════════════════
# Subgenre-baseret titel-lookup (v0.13.0 — media-aware)
# ══════════════════════════════════════════════════════════════════════════════

async def find_titles_by_subgenre(
    subgenre_id: str,
    media_type: str,
    limit: int = 30,
) -> list[dict]:
    """
    Find titler i tmdb_metadata der matcher en subgenre.

    Bruger CTE-baseret query for hurtig lookup via GIN-indekset på keywords.
    Smart-blanding på Python-side: Skiftevis nye (≥cutoff_year) og klassikere
    for at give et varieret udvalg.

    Args:
      subgenre_id: ID fra subgenre_service (fx 'horror_slasher')
      media_type:  'movie' eller 'tv'
      limit:       Max antal returnerede titler

    Returns:
      Liste af dicts med tmdb_id, title, year, tmdb_genres, keywords.
    """
    from services.subgenre_service import get_subgenre

    subgenre = get_subgenre(subgenre_id, media_type=media_type)
    if subgenre is None:
        logger.warning(
            "find_titles_by_subgenre: ukendt subgenre_id='%s' media='%s'",
            subgenre_id, media_type,
        )
        return []

    keywords:    list[str]   = subgenre["keywords"]
    plex_genre:  str | None  = subgenre["plex_genre"]
    cutoff_year: int         = subgenre.get("cutoff_year", 2010)

    if not keywords:
        return []

    where_parts = ["status = 'fetched'", "media_type = $1", "keywords ?| $2::text[]"]
    params: list = [media_type, keywords]

    if plex_genre:
        tmdb_genre_alts = _plex_genre_to_tmdb(plex_genre)
        where_parts.append("tmdb_genres ?| $3::text[]")
        params.append(tmdb_genre_alts)

    where_clause = " AND ".join(where_parts)
    sql = f"""
        WITH candidates AS (
            SELECT tmdb_id, media_type, title, year, tmdb_genres, keywords
            FROM tmdb_metadata
            WHERE {where_clause}
        )
        SELECT *
        FROM candidates
        ORDER BY year DESC NULLS LAST
        LIMIT $%d
    """ % (len(params) + 1)

    params.append(max(limit * 4, 100))  # Hent flere for at have plads til smart-blanding

    try:
        async with _pool_ref().acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception as e:
        logger.error("find_titles_by_subgenre SQL fejl: %s", e)
        return []

    if not rows:
        return []

    # Smart-blanding: skiftevis nye og klassikere
    new_items:     list[dict] = []
    classic_items: list[dict] = []

    for row in rows:
        item_dict = dict(row)
        # asyncpg returnerer JSONB som str — parse til list
        if isinstance(item_dict.get("tmdb_genres"), str):
            item_dict["tmdb_genres"] = json.loads(item_dict["tmdb_genres"])
        if isinstance(item_dict.get("keywords"), str):
            item_dict["keywords"] = json.loads(item_dict["keywords"])

        if item_dict.get("year") and item_dict["year"] >= cutoff_year:
            new_items.append(item_dict)
        else:
            classic_items.append(item_dict)

    # Smart-blanding
    result: list[dict] = []
    new_idx, classic_idx = 0, 0
    use_new = True

    while len(result) < limit:
        if use_new and new_idx < len(new_items):
            result.append(new_items[new_idx])
            new_idx += 1
        elif not use_new and classic_idx < len(classic_items):
            result.append(classic_items[classic_idx])
            classic_idx += 1
        elif new_idx < len(new_items):
            result.append(new_items[new_idx])
            new_idx += 1
        elif classic_idx < len(classic_items):
            result.append(classic_items[classic_idx])
            classic_idx += 1
        else:
            break

        use_new = not use_new

    logger.info(
        "find_titles_by_subgenre: subgenre='%s' media=%s returnerede %d titler "
        "(plex_genre='%s', %d nye + %d klassikere af %d kandidater)",
        subgenre_id, media_type, len(result), plex_genre or "ANY",
        len(new_items), len(classic_items), len(rows),
    )

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Bagudkompatibel wrapper (BEVARES for at undgå breaking changes)
# ══════════════════════════════════════════════════════════════════════════════

async def find_films_by_subgenre(
    subgenre_id: str,
    limit: int = 30,
) -> list[dict]:
    """
    [LEGACY] Find FILM der matcher en subgenre.

    Tynd wrapper omkring find_titles_by_subgenre med media_type='movie'.
    Bevares for at undgå breaking changes i v2_service og andre kaldere.
    """
    return await find_titles_by_subgenre(
        subgenre_id=subgenre_id,
        media_type="movie",
        limit=limit,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Subgenre coverage audit
# ══════════════════════════════════════════════════════════════════════════════

async def count_titles_by_subgenre(
    subgenre_id: str,
    media_type: str,
) -> int:
    """Tæl hvor mange titler der matcher en subgenre for et givent media_type."""
    from services.subgenre_service import get_subgenre

    subgenre = get_subgenre(subgenre_id, media_type=media_type)
    if subgenre is None:
        logger.warning(
            "count_titles_by_subgenre: ukendt subgenre_id='%s' for media_type='%s'",
            subgenre_id, media_type,
        )
        return 0

    keywords:   list[str]   = subgenre["keywords"]
    plex_genre: str | None  = subgenre["plex_genre"]

    if not keywords:
        return 0

    where_parts: list[str] = [
        "status = 'fetched'",
        "media_type = $1",
        "keywords ?| $2::text[]",
    ]
    params: list = [media_type, keywords]

    if plex_genre:
        tmdb_genre_alts = _plex_genre_to_tmdb(plex_genre)
        where_parts.append("tmdb_genres ?| $3::text[]")
        params.append(tmdb_genre_alts)

    where_clause = " AND ".join(where_parts)
    sql = f"SELECT COUNT(*) AS cnt FROM tmdb_metadata WHERE {where_clause}"

    try:
        async with _pool_ref().acquire() as conn:
            row = await conn.fetchrow(sql, *params)
        return row["cnt"] if row else 0
    except Exception as e:
        logger.error("count_titles_by_subgenre SQL-fejl for '%s'/%s: %s",
                     subgenre_id, media_type, e)
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Feedback system (v0.14.0)
# ══════════════════════════════════════════════════════════════════════════════

async def submit_feedback(
    telegram_id: int,
    feedback_type: str,
    message: str,
    screenshot_file_ids: list[str] | None = None,
    telegram_username: str | None = None,
    telegram_name: str | None = None,
) -> int:
    """Gem en ny feedback-record. Returnerer feedback-ID til notifikation."""
    file_ids_json = json.dumps(screenshot_file_ids or [])

    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO feedback (
                telegram_id, telegram_username, telegram_name,
                feedback_type, message, screenshot_file_ids
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING id
            """,
            telegram_id, telegram_username, telegram_name,
            feedback_type, message, file_ids_json,
        )

    feedback_id = row["id"]
    logger.info(
        "feedback submitted: id=%s telegram_id=%s type=%s screenshots=%d",
        feedback_id, telegram_id, feedback_type, len(screenshot_file_ids or []),
    )
    return feedback_id


async def list_feedback(
    status_filter: str | None = None,
    type_filter: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Liste feedback-records sorteret efter nyeste først."""
    where_parts: list[str] = []
    params: list = []
    pn = 0

    if status_filter == "active":
        pn += 1
        where_parts.append(f"status != ${pn}")
        params.append("resolved")
    elif status_filter:
        pn += 1
        where_parts.append(f"status = ${pn}")
        params.append(status_filter)

    if type_filter:
        pn += 1
        where_parts.append(f"feedback_type = ${pn}")
        params.append(type_filter)

    pn += 1
    params.append(limit)

    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = f"""
        SELECT *
        FROM feedback
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${pn}
    """

    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(sql, *params)

    results = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("screenshot_file_ids"), str):
            d["screenshot_file_ids"] = json.loads(d["screenshot_file_ids"])
        results.append(d)
    return results


async def get_feedback(feedback_id: int) -> dict | None:
    """Hent fuld detalje for én feedback-record."""
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM feedback WHERE id = $1", feedback_id
        )
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("screenshot_file_ids"), str):
        d["screenshot_file_ids"] = json.loads(d["screenshot_file_ids"])
    return d


async def update_feedback_status(feedback_id: int, status: str) -> bool:
    """Opdater status på en feedback-record. Returnerer True hvis row blev opdateret."""
    if status not in ("new", "seen", "replied", "resolved"):
        logger.warning("update_feedback_status: ugyldig status '%s'", status)
        return False

    async with _pool_ref().acquire() as conn:
        result = await conn.execute(
            """
            UPDATE feedback
            SET status     = $1,
                updated_at = NOW()
            WHERE id = $2
            """,
            status, feedback_id,
        )
    return result.endswith(" 1")


async def add_admin_reply(feedback_id: int, reply_text: str) -> bool:
    """Gem admin-svar atomisk sammen med status='replied'."""
    async with _pool_ref().acquire() as conn:
        result = await conn.execute(
            """
            UPDATE feedback
            SET admin_reply      = $1,
                admin_replied_at = NOW(),
                status           = 'replied',
                updated_at       = NOW()
            WHERE id = $2
            """,
            reply_text, feedback_id,
        )
    return result.endswith(" 1")


async def count_feedback_by_status() -> dict:
    """Returnér optælling af feedback pr. status."""
    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, COUNT(*) AS cnt FROM feedback GROUP BY status"
        )
    counts = {"new": 0, "seen": 0, "replied": 0, "resolved": 0}
    for row in rows:
        counts[row["status"]] = row["cnt"]
    counts["total"] = sum(counts.values())
    return counts


async def is_first_time_feedback(telegram_id: int) -> bool:
    """
    Returnerer True hvis brugeren ALDRIG har sendt feedback før.

    Bruges af main.py til at tagge admin-notifikationen med "🆕 NY TESTER"
    så Jesper instant ved at det er en førstegangs-bruger.
    """
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM feedback WHERE telegram_id = $1 LIMIT 1",
            telegram_id,
        )
    return row is None


# ══════════════════════════════════════════════════════════════════════════════
# Bulk operations + helpers (v0.14.2)
# ══════════════════════════════════════════════════════════════════════════════

def parse_id_range(spec: str) -> list[int]:
    """
    Parse en bruger-specificeret ID-range/list til konkrete IDs.

    Eksempler:
      "1-5"     → [1,2,3,4,5]
      "5,7,9"   → [5,7,9]
      "1,3-5,8" → [1,3,4,5,8]

    Returnerer sorteret liste af unique IDs. Ignorerer ugyldige tokens.
    """
    if not spec or not spec.strip():
        return []

    ids: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            try:
                start_str, end_str = part.split("-", 1)
                start = int(start_str.strip())
                end   = int(end_str.strip())
                if start > end:
                    start, end = end, start
                ids.update(range(start, end + 1))
            except (ValueError, AttributeError):
                logger.warning("parse_id_range: ugyldig range '%s' — ignorerer", part)
                continue
        else:
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning("parse_id_range: ugyldigt token '%s' — ignorerer", part)
                continue

    return sorted(ids)


async def update_feedback_status_bulk(ids: list[int], status: str) -> int:
    """
    Opdater status for FLERE feedback-records på én gang.

    Bruges af admin-bot's /seen 1-20 og /resolve 5,7,9 kommandoer.
    Returnerer antal records faktisk opdateret.
    """
    if not ids:
        return 0
    if status not in ("new", "seen", "replied", "resolved"):
        logger.warning("update_feedback_status_bulk: ugyldig status '%s'", status)
        return 0

    async with _pool_ref().acquire() as conn:
        result = await conn.execute(
            """
            UPDATE feedback
            SET status     = $1,
                updated_at = NOW()
            WHERE id = ANY($2::int[])
            """,
            status, ids,
        )

    # asyncpg returnerer "UPDATE N" — udtræk N
    try:
        affected = int(result.split()[-1])
    except (ValueError, IndexError):
        affected = 0

    logger.info(
        "update_feedback_status_bulk: status='%s' ids=%s → %d opdateret",
        status, ids, affected,
    )
    return affected