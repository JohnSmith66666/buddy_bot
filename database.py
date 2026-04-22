"""
database.py - PostgreSQL connection pool, table management,
user whitelist, Plex username storage and interaction logging.
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
    telegram_id    BIGINT PRIMARY KEY,
    telegram_name  TEXT,
    plex_username  TEXT,
    is_whitelisted BOOLEAN NOT NULL DEFAULT FALSE,
    added_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
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
    """Whitelist an existing user."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_whitelisted = TRUE WHERE telegram_id = $1",
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
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            "UPDATE users SET plex_username = $1 WHERE telegram_id = $2",
            plex_username, telegram_id,
        )
    logger.info("plex_username='%s' saved for telegram_id=%s", plex_username, telegram_id)


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
        # Prune old rows per user
        await conn.execute(
            """
            DELETE FROM interaction_log
            WHERE id IN (
                SELECT id FROM interaction_log
                WHERE telegram_id = $1
                ORDER BY logged_at DESC
                OFFSET $2
            )
            """,
            telegram_id, LOG_HISTORY_LIMIT,
        )