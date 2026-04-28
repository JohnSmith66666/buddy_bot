"""
admin_bot/admin_database.py - Slim database module for the Buddy Admin bot.

CHANGES (v0.1.0 — initial):
  - Genbruger asyncpg connection pool pattern fra Buddys database.py.
  - Eksponerer KUN feedback-funktioner (admin-bot har ikke brug for fx
    tmdb_metadata, subgenre lookups, persona, watchlist osv.).
  - get_user_by_telegram_id(): bruges til at vise en lille kontekst om
    feedback-afsenderen (er de stadig whitelisted? hvilken Plex-bruger?).
  - SAMME TABEL som Buddy main — admin-bot LÆSER + opdaterer feedback der
    blev INDSAT af Buddy. Begge bots peger på samme MAIN-database via
    DATABASE_URL env-var.

DESIGN-PRINCIPPER:
  - Ingen DB-schema-opsætning her — admin-bot opretter ikke tabeller.
    Buddy main har allerede kaldt setup_feedback_table() ved opstart.
  - Hvis feedback-tabellen ikke findes endnu, fejler admin-bot tydeligt
    med en informativ besked.
  - JSON-håndtering matcher Buddys database.py så data-format er identisk.
"""

import json
import logging

import asyncpg

from admin_config import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════════

async def setup_db() -> None:
    """
    Initialise connection pool og verificér at feedback-tabellen findes.

    Vi opretter IKKE tabellen her — det er Buddys ansvar via setup_feedback_table.
    Hvis admin-bot starter før Buddy main har kørt mindst én gang mod denne DB,
    fejler vi tydeligt så du ved hvad der mangler.
    """
    global _pool
    logger.info("Connecting to PostgreSQL (admin-bot) …")
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL, min_size=1, max_size=5, command_timeout=30,
    )

    # Verificér at feedback-tabellen findes
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'feedback'
            ) AS exists
            """
        )

    if not row or not row["exists"]:
        raise RuntimeError(
            "feedback table doesn't exist in this database. "
            "Make sure Buddy main has been deployed first — it creates the table "
            "via setup_feedback_table() at startup. "
            "Check that DATABASE_URL points to the correct (MAIN) database."
        )

    logger.info("Database ready — feedback table found.")


async def close_db() -> None:
    """Luk connection pool ved shutdown."""
    if _pool:
        await _pool.close()


def _pool_ref() -> asyncpg.Pool:
    """Hent pool-reference med tydelig fejl hvis ikke initialiseret."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call setup_db() first.")
    return _pool


# ══════════════════════════════════════════════════════════════════════════════
# Feedback queries (læser/opdaterer fra fælles tabel)
# ══════════════════════════════════════════════════════════════════════════════

async def list_feedback(
    status_filter: str | None = None,
    type_filter: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Liste feedback-records sorteret efter nyeste først.

    Args:
      status_filter: 'new' | 'seen' | 'replied' | 'resolved' | 'active'
                     (active = new + seen + replied, dvs. ikke resolved)
                     None = alle
      type_filter:   'idea' | 'bug' | 'question' | 'praise'
                     None = alle
      limit:         Max antal records (default 20)

    Returns:
      Liste af dicts med alle felter. screenshot_file_ids parses til list.
    """
    where_parts: list[str] = []
    params: list = []
    param_idx = 1

    if status_filter == "active":
        where_parts.append("status IN ('new', 'seen', 'replied')")
    elif status_filter in ("new", "seen", "replied", "resolved"):
        where_parts.append(f"status = ${param_idx}")
        params.append(status_filter)
        param_idx += 1

    if type_filter in ("idea", "bug", "question", "praise"):
        where_parts.append(f"feedback_type = ${param_idx}")
        params.append(type_filter)
        param_idx += 1

    where_clause = ""
    if where_parts:
        where_clause = "WHERE " + " AND ".join(where_parts)

    params.append(limit)
    sql = f"""
        SELECT id, telegram_id, telegram_username, telegram_name,
               feedback_type, message, screenshot_file_ids,
               status, admin_reply, admin_replied_at,
               created_at, updated_at
        FROM feedback
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${param_idx}
    """

    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(sql, *params)

    result = []
    for row in rows:
        d = dict(row)
        try:
            d["screenshot_file_ids"] = (
                json.loads(d["screenshot_file_ids"])
                if isinstance(d["screenshot_file_ids"], str)
                else (d["screenshot_file_ids"] or [])
            )
        except Exception:
            d["screenshot_file_ids"] = []
        result.append(d)

    return result


async def get_feedback(feedback_id: int) -> dict | None:
    """
    Hent én feedback-record med fuld detalje.

    Returns:
      Dict med alle felter, eller None hvis ID ikke findes.
      screenshot_file_ids parses til list.
    """
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, telegram_id, telegram_username, telegram_name,
                   feedback_type, message, screenshot_file_ids,
                   status, admin_reply, admin_replied_at,
                   created_at, updated_at
            FROM feedback
            WHERE id = $1
            """,
            feedback_id,
        )

    if not row:
        return None

    d = dict(row)
    try:
        d["screenshot_file_ids"] = (
            json.loads(d["screenshot_file_ids"])
            if isinstance(d["screenshot_file_ids"], str)
            else (d["screenshot_file_ids"] or [])
        )
    except Exception:
        d["screenshot_file_ids"] = []
    return d


async def update_feedback_status(feedback_id: int, status: str) -> bool:
    """
    Opdater status på en feedback-record.

    Args:
      feedback_id: ID på record
      status:      'new' | 'seen' | 'replied' | 'resolved'

    Returns:
      True hvis record blev opdateret, False hvis ID ikke findes.
    """
    if status not in ("new", "seen", "replied", "resolved"):
        raise ValueError(f"Ugyldig status: '{status}'")

    async with _pool_ref().acquire() as conn:
        result = await conn.execute(
            """
            UPDATE feedback
            SET status     = $2,
                updated_at = NOW()
            WHERE id = $1
            """,
            feedback_id, status,
        )

    updated = result.endswith(" 1")
    if updated:
        logger.info("update_feedback_status: id=%d → '%s'", feedback_id, status)
    return updated


async def add_admin_reply(feedback_id: int, reply_text: str) -> bool:
    """
    Gem admin-svar og marker feedback som 'replied'.

    Atomisk operation: opdaterer admin_reply, admin_replied_at og status
    i én transaction.

    Returns:
      True hvis record blev opdateret, False hvis ID ikke findes.
    """
    async with _pool_ref().acquire() as conn:
        result = await conn.execute(
            """
            UPDATE feedback
            SET admin_reply      = $2,
                admin_replied_at = NOW(),
                status           = 'replied',
                updated_at       = NOW()
            WHERE id = $1
            """,
            feedback_id, reply_text,
        )

    updated = result.endswith(" 1")
    if updated:
        logger.info("add_admin_reply: id=%d (%d chars)", feedback_id, len(reply_text))
    return updated


async def count_feedback_by_status() -> dict:
    """
    Tæl feedback-records grupperet efter status og type.

    Returns:
      {
        "total": 42,
        "by_status": {"new": 5, "seen": 3, "replied": 10, "resolved": 24},
        "by_type":   {"idea": 15, "bug": 20, "question": 5, "praise": 2},
      }
    """
    async with _pool_ref().acquire() as conn:
        status_rows = await conn.fetch(
            "SELECT status, COUNT(*) AS cnt FROM feedback GROUP BY status"
        )
        type_rows = await conn.fetch(
            "SELECT feedback_type, COUNT(*) AS cnt FROM feedback GROUP BY feedback_type"
        )

    by_status = {"new": 0, "seen": 0, "replied": 0, "resolved": 0}
    for r in status_rows:
        by_status[r["status"]] = r["cnt"]

    by_type = {"idea": 0, "bug": 0, "question": 0, "praise": 0}
    for r in type_rows:
        by_type[r["feedback_type"]] = r["cnt"]

    return {
        "total":     sum(by_status.values()),
        "by_status": by_status,
        "by_type":   by_type,
    }


# ══════════════════════════════════════════════════════════════════════════════
# User queries (read-only — bruges til kontekst om feedback-afsender)
# ══════════════════════════════════════════════════════════════════════════════

async def get_user_by_telegram_id(telegram_id: int) -> dict | None:
    """
    Hent bruger-info for context i admin-visningen.

    Returnerer is_whitelisted + plex_username så admin kan se om feedback-
    afsenderen stadig er en aktiv tester.
    """
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT telegram_id, telegram_name, plex_username,
                   is_whitelisted, added_at
            FROM users
            WHERE telegram_id = $1
            """,
            telegram_id,
        )
    return dict(row) if row else None