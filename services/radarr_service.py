"""
database.py - PostgreSQL connection pool, table management,
user whitelist, Plex username storage, onboarding state and interaction logging.

CHANGES vs previous version:
- Added `onboarding_state` column to `users` table so Railway restarts
  never lose users mid-onboarding (replaces the in-memory set in main.py).
- Log pruning now uses a single DELETE with ORDER BY + OFFSET instead of
  a correlated subquery, which is faster on large tables.
"""

import logging
from datetime import datetime, timezone

import asyncpg

from config import DATABASE_URL, LOG_HISTORY_LIMIT

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id      BIGINT PRIMARY KEY,
    telegram_name    TEXT,
    plex_username    TEXT,
    is_whitelisted   BOOLEAN     NOT NULL DEFAULT FALSE,
    onboarding_state TEXT,                          -- NULL | 'awaiting_plex'
    added_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Migration: add onboarding_state if an older version of the table exists.
_MIGRATE_ONBOARDING_STATE = """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS onboarding_state TEXT;
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


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def setup_db() -> None:
    global _pool
    logger.info("Connecting to PostgreSQL …")
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL, min_size=2, max_size=10, command_timeout=30,
    )
    async with _pool.acquire() as conn:
        await conn.execute(_CREATE_USERS_TABLE)
        await conn.execute(_MIGRATE_ONBOARDING_STATE)
        await conn.execute(_CREATE_INTERACTION_LOG_TABLE)
        await conn.execute(_CREATE_LOG_INDEX)
    logger.info("Database ready.")


async def close_db() -> None:
    if _pool:
        await _pool.close()


def _pool_ref() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call setup_db() first.")
    return _pool


# ── User helpers ──────────────────────────────────────────────────────────────

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
    """Whitelist an existing user and mark them as awaiting Plex setup."""
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
    logger.info("User %s approved.", telegram_id)


async def get_plex_username(telegram_id: int) -> str | None:
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT plex_username FROM users WHERE telegram_id = $1", telegram_id
        )
    return row["plex_username"] if row else None


async def set_plex_username(telegram_id: int, plex_username: str) -> None:
    """Save the verified Plex username and clear the onboarding state."""
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
    logger.info("plex_username='%s' saved for telegram_id=%s", plex_username, telegram_id)


# ── Onboarding state ──────────────────────────────────────────────────────────

async def get_onboarding_state(telegram_id: int) -> str | None:
    """Return the user's current onboarding_state ('awaiting_plex' or None)."""
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT onboarding_state FROM users WHERE telegram_id = $1", telegram_id
        )
    return row["onboarding_state"] if row else None


async def set_onboarding_state(telegram_id: int, state: str | None) -> None:
    """Set or clear the onboarding_state for a user."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            "UPDATE users SET onboarding_state = $1 WHERE telegram_id = $2",
            state, telegram_id,
        )


# ── Interaction log ───────────────────────────────────────────────────────────

async def log_message(
    telegram_id: int,
    direction: str,
    message_text: str,
) -> None:
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO interaction_log (telegram_id, direction, message_text, logged_at)
            VALUES ($1, $2, $3, $4)
            """,
            telegram_id, direction, message_text, datetime.now(timezone.utc),
        )
        # FIX: Pruning rewritten as a single DELETE with ORDER BY + OFFSET.
        # This avoids the correlated subquery which is slow on large tables.
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