"""
database.py - PostgreSQL connection pool, table management,
user whitelist, Plex username storage, onboarding state, interaktionshistorik,
pending requests, TMDB metadata-cache OG feedback-system.

CHANGES (v0.14.1 — first-time tester detection):
  - NY: is_first_time_feedback(telegram_id) → bool
    Returnerer True hvis brugeren ALDRIG har sendt feedback før.
    Bruges af main.py til at tagge admin-notifikationen med "🆕 NY TESTER"
    så Jesper instant ved at det er en førstegangs-bruger.

UNCHANGED (v0.14.0 — feedback system):
  - NY: feedback tabel + indekser til opsamling af bruger-feedback.
    Tabellen gemmer kategoriseret feedback (idea/bug/question/praise),
    Telegram screenshot file_ids som JSONB array, og admin-svar med
    timestamp. Status-felt sporer livscyklus: new → seen → replied → resolved.
  - NY: setup_feedback_table() kaldt fra setup_db() ved opstart.
  - NY: submit_feedback() — bruges af Buddy til at gemme ny feedback.
  - NY: list_feedback(status_filter, type_filter, limit) — admin-bot listing.
  - NY: get_feedback(feedback_id) — fuld detalje + screenshot file_ids.
  - NY: update_feedback_status(id, status) — admin markerer som
    seen/replied/resolved.
  - NY: add_admin_reply(id, reply_text) — gemmer admin-svar atomisk
    sammen med status='replied'.
  - NY: count_feedback_by_status() — bruges til at bygge stats hvis ønsket.
  - DELT TABEL: Buddy SKRIVER til feedback, admin-bot LÆSER + opdaterer.
    Begge bots peger på samme MAIN-database (admin lytter ikke til dev).

UNCHANGED (v0.13.0 — media-aware subgenre lookup):
  - find_titles_by_subgenre(subgenre_id, media_type, limit) → list[dict]
  - find_films_by_subgenre legacy wrapper bevares.
  - count_titles_by_subgenre(subgenre_id, media_type) → int

UNCHANGED (v0.11.0 — P0/P1 performance pakke):
  - log_message statistical DELETE (10% kald → 50% mindre DB-load).
  - CTE-baseret subgenre query (30-50ms hurtigere).

UNCHANGED (v0.10.8 — Etape 1 af subgenre-projekt):
  - GIN-index på keywords-kolonnen til O(ms) lookup
  - Smart blanding på Python-side (nye vs klassikere)

UNCHANGED (v0.10.6 — TMDB metadata cache):
  - Ny tabel tmdb_metadata + GIN-indekser
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
# Schema — Feedback (NY i v0.14.0)
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
# TMDB metadata cache (Etape 0 + 1)
# ══════════════════════════════════════════════════════════════════════════════

async def setup_tmdb_metadata_table() -> None:
    """Opret tmdb_metadata tabellen + indekser. Idempotent."""
    async with _pool_ref().acquire() as conn:
        await conn.execute(_CREATE_TMDB_METADATA_TABLE)
        await conn.execute(_CREATE_TMDB_STATUS_INDEX)
        await conn.execute(_CREATE_TMDB_KEYWORDS_GIN)
        await conn.execute(_CREATE_TMDB_GENRES_GIN)
    logger.info("tmdb_metadata table + indexes ready.")


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

    result = {
        "total":     0, "pending":   0, "fetched":   0,
        "error":     0, "not_found": 0,
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
    """Hent næste batch af records der skal fetches fra TMDB."""
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
    """Marker en record som 'fetched' og gem TMDB-data."""
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
            tmdb_id, media_type,
            json.dumps(tmdb_genres), json.dumps(keywords),
            title, year,
        )


async def update_metadata_error(
    tmdb_id: int,
    media_type: str,
    error_message: str,
    is_not_found: bool = False,
) -> None:
    """Marker en record som 'error' eller 'not_found'."""
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


async def get_top_keywords(
    media_type: str | None = None,
    limit: int | None = 50,
    min_count: int = 1,
) -> list[dict]:
    """Find de mest brugte keywords i samlingen."""
    where_parts: list[str] = ["status = 'fetched'"]
    params: list = []
    param_idx = 1

    if media_type in ("movie", "tv"):
        where_parts.append(f"media_type = ${param_idx}")
        params.append(media_type)
        param_idx += 1

    where_clause = " AND ".join(where_parts)

    having_clause = ""
    if min_count > 1:
        having_clause = f"HAVING COUNT(*) >= ${param_idx}"
        params.append(min_count)
        param_idx += 1

    limit_clause = ""
    if limit is not None:
        limit_clause = f"LIMIT ${param_idx}"
        params.append(limit)

    sql = f"""
        SELECT keyword, COUNT(*) AS cnt
        FROM tmdb_metadata,
             jsonb_array_elements_text(keywords) AS keyword
        WHERE {where_clause}
        GROUP BY keyword
        {having_clause}
        ORDER BY cnt DESC
        {limit_clause}
    """

    async with _pool_ref().acquire() as conn:
        rows = await conn.fetch(sql, *params)

    return [{"keyword": row["keyword"], "count": row["cnt"]} for row in rows]


# ══════════════════════════════════════════════════════════════════════════════
# Subgenre lookup — Plex/TMDB genre mapping
# ══════════════════════════════════════════════════════════════════════════════

# Mapping fra dansk Plex-genre til engelske TMDB-genrer.
_PLEX_TO_TMDB_GENRE = {
    "Komedie":      ["Comedy"],
    "Gyser":        ["Horror"],
    "Kriminalitet": ["Crime"],
    "Familie":      ["Family"],
    "Romantik":     ["Romance"],
    "Krig":         ["War"],
    "Mysterium":    ["Mystery"],
    "Musik":        ["Music"],
    "Historie":     ["History"],
    "Action":       ["Action"],
    "Drama":        ["Drama"],
    "Thriller":     ["Thriller"],
    "Adventure":    ["Adventure"],
    "Fantasy":      ["Fantasy"],
    "Sci-fi":       ["Science Fiction"],
    "Animation":    ["Animation"],
    "Documentary":  ["Documentary"],
    "Western":      ["Western"],
    "Biography":    ["Drama"],
    "Musical":      ["Music"],
}


def _plex_genre_to_tmdb(plex_genre: str) -> list[str]:
    """Konverter et Plex-genre-navn til en liste af tilsvarende TMDB-genrer."""
    return _PLEX_TO_TMDB_GENRE.get(plex_genre, [plex_genre])


# ══════════════════════════════════════════════════════════════════════════════
# Subgenre title lookup (NY i v0.13.0 — generaliseret for film + TV)
# ══════════════════════════════════════════════════════════════════════════════

async def find_titles_by_subgenre(
    subgenre_id: str,
    media_type: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """
    Find titler der matcher en subgenre (keywords + valgfri Plex-genre).

    Generaliseret version af find_films_by_subgenre der virker for både
    'movie' og 'tv'.

    Args:
      subgenre_id: ID fra subgenre_service (fx 'horror_slasher' eller 'tv_murder_mystery')
      media_type:  'movie' eller 'tv'. None = auto-detect via subgenre prefix.
      limit:       Max antal titler at returnere (efter smart-blanding).

    Returns:
      Liste af dicts: [{"tmdb_id", "title", "year", "tmdb_genres", "keywords"}, ...]
      Smart-blandet med nye + klassikere.

    SMART-BLANDING:
      Vi henter et bredt udsnit (limit*3 candidates), deler i nye/klassikere
      buckets på Python-side og fletter dem alternativt for at give brugeren
      et mix af friske og ældre titler.

    PERFORMANCE:
      Bruger CTE-baseret query der filtrerer FØRST, derefter randomiserer.
      30-50ms hurtigere på populære subgenrer.
    """
    from services.subgenre_service import get_subgenre, detect_media_type

    # Auto-detect media_type hvis ikke angivet
    if media_type is None:
        media_type = detect_media_type(subgenre_id)
        if media_type is None:
            logger.warning(
                "find_titles_by_subgenre: kunne ikke auto-detect media_type for '%s'",
                subgenre_id,
            )
            return []

    # Validér media_type
    if media_type not in ("movie", "tv"):
        logger.error("find_titles_by_subgenre: ugyldig media_type='%s'", media_type)
        return []

    # Hent subgenre fra det rigtige katalog
    subgenre = get_subgenre(subgenre_id, media_type=media_type)
    if subgenre is None:
        logger.warning(
            "find_titles_by_subgenre: ukendt subgenre_id='%s' for media_type='%s'",
            subgenre_id, media_type,
        )
        return []

    keywords:   list[str]    = subgenre["keywords"]
    plex_genre: str | None   = subgenre["plex_genre"]

    if not keywords:
        logger.warning(
            "find_titles_by_subgenre: subgenre '%s' har ingen keywords", subgenre_id,
        )
        return []

    fetch_limit = limit * 3

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

    sql = f"""
        WITH filtered AS (
            SELECT tmdb_id, title, year, tmdb_genres, keywords
            FROM tmdb_metadata
            WHERE {where_clause}
        )
        SELECT tmdb_id, title, year, tmdb_genres, keywords
        FROM filtered
        ORDER BY RANDOM()
        LIMIT ${len(params) + 1}
    """
    params.append(fetch_limit)

    try:
        async with _pool_ref().acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception as e:
        logger.error(
            "find_titles_by_subgenre SQL-fejl for '%s'/%s: %s",
            subgenre_id, media_type, e,
        )
        return []

    current_year = datetime.now(timezone.utc).year
    cutoff_year  = current_year - 5

    new_items:     list[dict] = []
    classic_items: list[dict] = []

    for row in rows:
        try:
            tmdb_genres = json.loads(row["tmdb_genres"]) if isinstance(row["tmdb_genres"], str) else row["tmdb_genres"]
        except Exception:
            tmdb_genres = []
        try:
            kw_list = json.loads(row["keywords"]) if isinstance(row["keywords"], str) else row["keywords"]
        except Exception:
            kw_list = []

        item_dict = {
            "tmdb_id":     row["tmdb_id"],
            "title":       row["title"] or "Ukendt",
            "year":        row["year"],
            "tmdb_genres": tmdb_genres or [],
            "keywords":    kw_list or [],
        }

        if row["year"] and row["year"] >= cutoff_year:
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

    NY KODE BØR BRUGE: find_titles_by_subgenre(subgenre_id, media_type='movie' eller 'tv')
    """
    return await find_titles_by_subgenre(
        subgenre_id=subgenre_id,
        media_type="movie",
        limit=limit,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Subgenre coverage audit (uændret fra v0.12.0 — bruges af /audit_tv_subgenres)
# ══════════════════════════════════════════════════════════════════════════════

async def count_titles_by_subgenre(
    subgenre_id: str,
    media_type: str,
) -> int:
    """
    Tæl hvor mange titler der matcher en subgenre for et givent media_type.

    Bruges af audit-kommandoen til at finde:
      - Stærke subgenrer (>20 titler)
      - Svage subgenrer (1-20 titler)
      - Tomme subgenrer (0 titler)

    Args:
      subgenre_id: ID fra subgenre_service (fx 'horror_slasher')
      media_type:  'movie' eller 'tv'

    Returns:
      Antal titler i tmdb_metadata der matcher (status='fetched').
      Returnerer 0 hvis subgenre_id er ukendt eller ingen matches.
    """
    from services.subgenre_service import get_subgenre

    subgenre = get_subgenre(subgenre_id, media_type=media_type)
    if subgenre is None:
        logger.warning(
            "count_titles_by_subgenre: ukendt subgenre_id='%s' for media_type='%s'",
            subgenre_id, media_type,
        )
        return 0

    keywords:   list[str]    = subgenre["keywords"]
    plex_genre: str | None   = subgenre["plex_genre"]

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

    sql = f"""
        SELECT COUNT(*) AS cnt
        FROM tmdb_metadata
        WHERE {where_clause}
    """

    try:
        async with _pool_ref().acquire() as conn:
            row = await conn.fetchrow(sql, *params)
        return row["cnt"] if row else 0
    except Exception as e:
        logger.error("count_titles_by_subgenre SQL-fejl for '%s'/%s: %s",
                     subgenre_id, media_type, e)
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Feedback system (NY i v0.14.0)
# ══════════════════════════════════════════════════════════════════════════════

async def setup_feedback_table() -> None:
    """
    Opret feedback tabellen + indekser. Idempotent.

    Tabellen deles mellem Buddy (skriver via submit_feedback) og admin-bot
    (læser via list_feedback/get_feedback, opdaterer via update_feedback_status
    og add_admin_reply). Begge bots peger på samme MAIN-database.
    """
    async with _pool_ref().acquire() as conn:
        await conn.execute(_CREATE_FEEDBACK_TABLE)
        await conn.execute(_CREATE_FEEDBACK_STATUS_INDEX)
        await conn.execute(_CREATE_FEEDBACK_TYPE_INDEX)
        await conn.execute(_CREATE_FEEDBACK_USER_INDEX)
    logger.info("feedback table + indexes ready.")


async def submit_feedback(
    telegram_id: int,
    feedback_type: str,
    message: str,
    screenshot_file_ids: list[str] | None = None,
    telegram_username: str | None = None,
    telegram_name: str | None = None,
) -> int:
    """
    Gem en ny feedback-record. Returnerer feedback-ID til notifikation.

    Args:
      telegram_id:         Telegram bruger-ID
      feedback_type:       'idea' | 'bug' | 'question' | 'praise'
      message:             Brugerens tekst-besked
      screenshot_file_ids: Liste af Telegram file_ids (kan være tom)
      telegram_username:   Brugerens @username (kan være None)
      telegram_name:       Brugerens first_name (kan være None)

    Returns:
      ID på den nye feedback-record (bruges af admin-notifikation).
    """
    if feedback_type not in ("idea", "bug", "question", "praise"):
        raise ValueError(f"Ugyldig feedback_type: '{feedback_type}'")

    file_ids = screenshot_file_ids or []

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
            telegram_id,
            telegram_username,
            telegram_name,
            feedback_type,
            message,
            json.dumps(file_ids),
        )

    feedback_id = row["id"] if row else 0
    logger.info(
        "submit_feedback: id=%d type=%s telegram_id=%s screenshots=%d",
        feedback_id, feedback_type, telegram_id, len(file_ids),
    )
    return feedback_id


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

    # asyncpg returnerer "UPDATE 1" eller "UPDATE 0"
    updated = result.endswith(" 1")
    if updated:
        logger.info("update_feedback_status: id=%d → '%s'", feedback_id, status)
    return updated


async def add_admin_reply(feedback_id: int, reply_text: str) -> bool:
    """
    Gem admin-svar og marker feedback som 'replied'.

    Atomisk operation: opdaterer admin_reply, admin_replied_at og status
    i én transaction.

    Args:
      feedback_id: ID på record
      reply_text:  Admins svar-tekst

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

    Bruges til admin-bot statistik (Phase 2).

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


async def is_first_time_feedback(telegram_id: int) -> bool:
    """
    Tjek om en bruger sender feedback for FØRSTE gang.

    Skal kaldes FØR submit_feedback() — efter submit findes der allerede
    mindst én record for brugeren, så funktionen ville altid returnere False.

    Args:
      telegram_id: Telegram bruger-ID

    Returns:
      True hvis brugeren har 0 feedback-records, False ellers.
    """
    async with _pool_ref().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT EXISTS (
                SELECT 1 FROM feedback WHERE telegram_id = $1
            ) AS has_feedback
            """,
            telegram_id,
        )

    has_feedback = bool(row and row["has_feedback"])
    return not has_feedback