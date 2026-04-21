"""
database.py - PostgreSQL connection pool, automatic table creation,
whitelist enforcement, and interaction logging.
"""

import logging
from datetime import datetime, timezone

import asyncpg

from config import DATABASE_URL, LOG_HISTORY_LIMIT

logger = logging.getLogger(__name__)

# Module-level connection pool — initialised once in setup_db().
_pool: asyncpg.Pool | None = None


# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_WHITELIST_TABLE = """
CREATE TABLE IF NOT EXISTS whitelist (
    telegram_id   BIGINT PRIMARY KEY,
    username      TEXT,
    added_by      BIGINT,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes         TEXT
);
"""

_CREATE_INTERACTION_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS interaction_log (
    id            BIGSERIAL PRIMARY KEY,
    telegram_id   BIGINT      NOT NULL,
    username      TEXT,
    direction     TEXT        NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    message_text  TEXT,
    logged_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_INTERACTION_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_interaction_log_telegram_id
    ON interaction_log (telegram_id, logged_at DESC);
"""


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def setup_db() -> None:
    """
    Create the connection pool and ensure all tables exist.
    Call this once at bot startup before processing any updates.
    """
    global _pool

    logger.info("Connecting to PostgreSQL …")
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )

    async with _pool.acquire() as conn:
        await conn.execute(_CREATE_WHITELIST_TABLE)
        await conn.execute(_CREATE_INTERACTION_LOG_TABLE)
        await conn.execute(_CREATE_INTERACTION_LOG_INDEX)

    logger.info("Database setup complete — tables verified.")


async def close_db() -> None:
    """Gracefully close the connection pool on shutdown."""
    if _pool:
        await _pool.close()
        logger.info("Database connection pool closed.")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised. Call setup_db() first.")
    return _pool


# ── Whitelist ─────────────────────────────────────────────────────────────────

async def is_whitelisted(telegram_id: int) -> bool:
    """Return True if the given Telegram user ID is on the whitelist."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM whitelist WHERE telegram_id = $1", telegram_id
        )
    return row is not None


async def add_to_whitelist(
    telegram_id: int,
    username: str | None = None,
    added_by: int | None = None,
    notes: str | None = None,
) -> None:
    """Insert a user into the whitelist (no-op if they already exist)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO whitelist (telegram_id, username, added_by, notes)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO NOTHING
            """,
            telegram_id,
            username,
            added_by,
            notes,
        )
    logger.info("Whitelisted user %s (%s)", telegram_id, username)


# ── Interaction log ───────────────────────────────────────────────────────────

async def log_message(
    telegram_id: int,
    direction: str,
    message_text: str,
    username: str | None = None,
) -> None:
    """
    Persist a single message to the interaction log.

    Args:
        telegram_id:  The Telegram user ID.
        direction:    Either 'incoming' (user → bot) or 'outgoing' (bot → user).
        message_text: The raw message content.
        username:     Telegram @username if available.
    """
    if direction not in ("incoming", "outgoing"):
        raise ValueError(f"Invalid direction '{direction}'. Use 'incoming' or 'outgoing'.")

    pool = _get_pool()
    async with pool.acquire() as conn:
        # Insert the new row.
        await conn.execute(
            """
            INSERT INTO interaction_log (telegram_id, username, direction, message_text, logged_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            telegram_id,
            username,
            direction,
            message_text,
            datetime.now(timezone.utc),
        )

        # Enforce per-user row limit to prevent unbounded table growth.
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
            telegram_id,
            LOG_HISTORY_LIMIT,
        )


async def get_recent_history(telegram_id: int, limit: int = 20) -> list[dict]:
    """
    Retrieve the most recent interactions for a user, oldest first.

    Returns a list of dicts with keys: direction, message_text, logged_at.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT direction, message_text, logged_at
            FROM interaction_log
            WHERE telegram_id = $1
            ORDER BY logged_at DESC
            LIMIT $2
            """,
            telegram_id,
            limit,
        )
    # Reverse so the list is chronological (oldest → newest).
    return [dict(row) for row in reversed(rows)]
