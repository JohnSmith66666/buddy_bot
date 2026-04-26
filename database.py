"""
database.py - PostgreSQL connection pool, table management,
user whitelist, Plex username storage, onboarding state, interaction logging,
pending requests OG TMDB metadata-cache (NY v0.10.6).

CHANGES vs previous version (v0.10.6 — TMDB metadata cache):
  - NY TABEL: tmdb_metadata
    * Cacher TMDB-genrer + keywords for alle Plex-titler (film + serier)
    * "Store All, Filter Later" princip — INGEN whitelist på data-laget
    * GIN-indekser på keywords + tmdb_genres for lyn-hurtige JSONB-queries
    * Felter til auto-refresh tilføjet (men logikken er IKKE aktiveret endnu)
  - NYE DB-FUNKTIONER:
    * setup_tmdb_metadata_table()  - opretter tabel + indexer
    * seed_tmdb_metadata()         - INSERT pending records (idempotent)
    * get_metadata_status()        - COUNT per status til /metadata_status
    * get_pending_metadata()       - SELECT næste batch til /fetch_metadata
    * update_metadata_success()    - UPDATE efter vellykket TMDB-fetch
    * update_metadata_error()      - UPDATE efter fejl
    * get_top_keywords()           - GROUP BY til /top_keywords (data discovery)

UNCHANGED:
  - Alle eksisterende user-, persona-, onboarding-, log- og pending_requests-funktioner.
  - persona_id kolonne, get_persona() / set_persona().
  - Logging arkitektur og connection pool setup.
"""

import json
import logging
from datetime import datetime, timezone

import asyncpg

from config import DATABASE_URL, LOG_HISTORY_LIMIT

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


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
# Schema — TMDB metadata cache (NY v0.10.6)
# ══════════════════════════════════════════════════════════════════════════════

# Designprincip: "Store All, Filter Later"
# - keywords gemmes RÅ uden filtrering
# - filtrering sker i Python-laget (find_unwatched_v2 + SUBGENRE_KEYWORDS)
# - title gemmes for nemmere debugging når vi inspicerer DB
# - status-felt gør resumable scanning muligt
# - GIN-indeksser muliggør lyn-hurtige JSONB-queries (millisekund)
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

# Status-index: hurtigt at finde næste batch af pending records
_CREATE_TMDB_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tmdb_metadata_status
    ON tmdb_metadata (status);
"""

# GIN-indekser: lyn-hurtig JSONB-søgning ('cyberpunk' = ANY(keywords) → ms)
_CREATE_TMDB_KEYWORDS_GIN = """
CREATE INDEX IF NOT EXISTS idx_tmdb_metadata_keywords
    ON tmdb_metadata USING GIN (keywords);
"""

_CREATE_TMDB_GENRES_GIN = """
CREATE INDEX IF NOT EXISTS idx_tmdb_metadata_genres
    ON tmdb_metadata USING GIN (tmdb_genres);
"""


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════════

async def setup_db() -> None:
    global _pool
    logger.info("Connecting to PostgreSQL …")
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL, min_size=2, max_size=10, command_timeout=30,
    )
    async with _pool.acquire() as conn:
        await conn.execute(_CREATE_USERS_TABLE)
        await conn.execute(_MIGRATE_ONBOARDING_STATE)
        await conn.execute(_MIGRATE_PERSONA_ID)
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


# ══════════════════════════════════════════════════════════════════════════════
# Persona
# ══════════════════════════════════════════════════════════════════════════════

async def get_persona(telegram_id: int) -> str:
    """Returnér brugerens valgte persona_id. Falder tilbage til 'buddy'."""
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


# ══════════════════════════════════════════════════════════════════════════════
# Interaction log
# ══════════════════════════════════════════════════════════════════════════════

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


async def setup_pending_requests() -> None:
    """Create pending_requests table if it doesn't exist."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(_CREATE_PENDING_REQUESTS_TABLE)
        await conn.execute(_CREATE_PENDING_INDEX)


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
# TMDB metadata cache (NY v0.10.6)
# ══════════════════════════════════════════════════════════════════════════════

async def setup_tmdb_metadata_table() -> None:
    """
    Opret tmdb_metadata tabellen + indekser.
    Idempotent — kan køres ved hver opstart uden problemer.
    Kaldes fra on_startup() i main.py.
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(_CREATE_TMDB_METADATA_TABLE)
        await conn.execute(_CREATE_TMDB_STATUS_INDEX)
        await conn.execute(_CREATE_TMDB_KEYWORDS_GIN)
        await conn.execute(_CREATE_TMDB_GENRES_GIN)
    logger.info("tmdb_metadata table + indexes ready.")


async def seed_tmdb_metadata(items: list[dict]) -> dict:
    """
    Seed (eller "støvsug") tmdb_metadata med pending records fra Plex-scanning.

    Args:
      items: liste af dicts med format:
        [{"tmdb_id": 27205, "media_type": "movie", "title": "Inception", "year": 2010}, ...]

    Idempotent: ON CONFLICT DO NOTHING betyder at eksisterende records (med ANY status)
    bliver IKKE rørt — kun nye records oprettes som 'pending'.
    Det betyder at /seed_metadata kan køres flere gange uden at "nulstille" allerede
    fetchede records.

    Returns:
      {"inserted": N, "skipped": M, "total_input": K}
    """
    if not items:
        return {"inserted": 0, "skipped": 0, "total_input": 0}

    # Bulk-insert via executemany med ON CONFLICT DO NOTHING for idempotency
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
                # asyncpg returnerer "INSERT 0 1" ved success, "INSERT 0 0" ved skip
                if result.endswith(" 1"):
                    inserted += 1

    skipped = len(items) - inserted
    logger.info("seed_tmdb_metadata: %d inserted, %d skipped (allerede i DB)", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped, "total_input": len(items)}


async def get_metadata_status() -> dict:
    """
    Returnér optælling af records pr. status — bruges af /metadata_status.

    Returns:
      {
        "total":     7785,
        "pending":   0,
        "fetched":   7642,
        "error":     53,
        "not_found": 90,
        "by_media_type": {
          "movie": {"total": 6636, "pending": 0, "fetched": 6543, "error": 50, "not_found": 43},
          "tv":    {"total": 1149, "pending": 0, "fetched": 1099, "error": 3,  "not_found": 47},
        }
      }
    """
    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT media_type, status, COUNT(*) AS cnt
            FROM tmdb_metadata
            GROUP BY media_type, status
            """
        )

    result = {
        "total":     0,
        "pending":   0,
        "fetched":   0,
        "error":     0,
        "not_found": 0,
        "by_media_type": {
            "movie": {"total": 0, "pending": 0, "fetched": 0, "error": 0, "not_found": 0},
            "tv":    {"total": 0, "pending": 0, "fetched": 0, "error": 0, "not_found": 0},
        },
    }

    for row in rows:
        media_type = row["media_type"]
        status     = row["status"]
        cnt        = row["cnt"]

        result["total"] += cnt
        result[status] = result.get(status, 0) + cnt

        if media_type in result["by_media_type"]:
            result["by_media_type"][media_type]["total"] += cnt
            result["by_media_type"][media_type][status]   = (
                result["by_media_type"][media_type].get(status, 0) + cnt
            )

    return result


async def get_pending_metadata(limit: int = 100, include_errors: bool = False) -> list[dict]:
    """
    Hent næste batch af records der skal fetches fra TMDB.

    Args:
      limit:          max antal records at hente (default 100)
      include_errors: hvis True, inkluderes også 'error' records (retry mode)

    Returns:
      [{"tmdb_id": 27205, "media_type": "movie", "title": "Inception", "year": 2010}, ...]
    """
    if include_errors:
        status_filter = "status IN ('pending', 'error')"
    else:
        status_filter = "status = 'pending'"

    sql = f"""
        SELECT tmdb_id, media_type, title, year
        FROM tmdb_metadata
        WHERE {status_filter}
        ORDER BY created_at ASC
        LIMIT $1
    """

    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(sql, limit)

    return [dict(row) for row in rows]


async def update_metadata_success(
    tmdb_id: int,
    media_type: str,
    tmdb_genres: list[str],
    keywords: list[str],
    title: str | None = None,
    year: int | None = None,
) -> None:
    """
    Marker en record som 'fetched' og gem TMDB-data.

    INGEN whitelist-filtrering — keywords gemmes præcist som TMDB returnerede dem.
    Filtreringen sker senere i Python-laget (find_unwatched_v2 + SUBGENRE_KEYWORDS).
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE tmdb_metadata
            SET status        = 'fetched',
                tmdb_genres   = $3::jsonb,
                keywords      = $4::jsonb,
                title         = COALESCE($5, title),
                year          = COALESCE($6, year),
                error_message = NULL,
                fetched_at    = NOW()
            WHERE tmdb_id = $1 AND media_type = $2
            """,
            tmdb_id,
            media_type,
            json.dumps(tmdb_genres),
            json.dumps(keywords),
            title,
            year,
        )


async def update_metadata_error(
    tmdb_id: int,
    media_type: str,
    error_message: str,
    is_not_found: bool = False,
) -> None:
    """
    Marker en record som 'error' eller 'not_found' (separat status så vi
    kan skelne mellem midlertidige fejl og permanent missing).
    """
    status = "not_found" if is_not_found else "error"
    async with _pool_ref().acquire() as conn:
        await conn.execute(
            """
            UPDATE tmdb_metadata
            SET status        = $3,
                error_message = $4,
                fetched_at    = NOW()
            WHERE tmdb_id = $1 AND media_type = $2
            """,
            tmdb_id, media_type, status, error_message,
        )


async def get_top_keywords(media_type: str | None = None, limit: int = 50) -> list[dict]:
    """
    Det DETEKTIV-VÆRKTØJ vi diskuterede — find de mest brugte keywords i din samling.
    Lader DATAEN fortælle hvilke subgenrer du faktisk ejer.

    Args:
      media_type: 'movie', 'tv' eller None (begge)
      limit:      antal top-keywords (default 50)

    Returns:
      [
        {"keyword": "based on novel",   "count": 1240},
        {"keyword": "woman director",   "count": 980},
        {"keyword": "high school",      "count": 450},
        {"keyword": "cyberpunk",        "count": 87},
        ...
      ]

    Bruger PostgreSQL's jsonb_array_elements_text() til at "splitte" keywords-arrayet
    så vi kan COUNT(*) GROUP BY pr. enkelt keyword.
    """
    where_clause = ""
    params: list = [limit]

    if media_type in ("movie", "tv"):
        where_clause = "WHERE media_type = $2 AND status = 'fetched'"
        params.append(media_type)
    else:
        where_clause = "WHERE status = 'fetched'"

    sql = f"""
        SELECT keyword, COUNT(*) AS cnt
        FROM tmdb_metadata,
             jsonb_array_elements_text(keywords) AS keyword
        {where_clause}
        GROUP BY keyword
        ORDER BY cnt DESC
        LIMIT $1
    """

    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(sql, *params)

    return [{"keyword": row["keyword"], "count": row["cnt"]} for row in rows]