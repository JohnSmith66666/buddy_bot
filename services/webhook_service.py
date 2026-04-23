"""
services/webhook_service.py - Webhook handler for Radarr og Sonarr.

CHANGES vs previous version:
  - Sonarr-notifikationer er nu batched via en debouncer.
  - Flere afsnit af samme serie inden for 90 sekunder samles til ÉN besked.
  - Radarr-notifikationer sendes stadig individuelt (én film = én besked).
  - Seriens poster hentes fra TMDB og sendes med i den samlede besked.

Debouncer-logik:
  1. Sonarr sender webhook for hvert afsnit.
  2. Vi akkumulerer afsnit pr. (serie_titel, telegram_id) i en buffer.
  3. Første webhook starter en 90-sekunders timer.
  4. Efterfølgende webhooks for samme serie nulstiller timeren.
  5. Når timeren udløber sendes ÉN samlet besked.

VIGTIGT: Fjern "Test" fra _ACCEPTED_RADARR og _ACCEPTED_SONARR
         når test er bekræftet at virke.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
from telegram import Bot

from config import TELEGRAM_BOT_TOKEN, TMDB_API_KEY
import database

logger = logging.getLogger(__name__)

_bot = Bot(token=TELEGRAM_BOT_TOKEN)

_ACCEPTED_RADARR = {"Download", "MovieAdded", "Test"}
_ACCEPTED_SONARR = {"Download", "EpisodeFileImported", "Test"}

_TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w500"
_DEBOUNCE_SECONDS = 90


# ── Debounce buffer ───────────────────────────────────────────────────────────

@dataclass
class _SeriesBatch:
    title:       str
    episode_count: int = 0
    season_set:  set   = field(default_factory=set)
    poster_url:  str | None = None
    overview:    str | None = None
    timer_task:  asyncio.Task | None = None


# Buffer: (series_title_lower, telegram_id) → _SeriesBatch
_batches: dict[tuple, _SeriesBatch] = {}


# ── TMDB poster lookup ────────────────────────────────────────────────────────

async def _fetch_poster_url(series_title: str) -> tuple[str | None, str | None]:
    """
    Search TMDB for the series and return (poster_url, overview).
    Returns (None, None) on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.themoviedb.org/3/search/tv",
                params={
                    "api_key":  TMDB_API_KEY,
                    "language": "da-DK",
                    "query":    series_title,
                },
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return None, None
            top = results[0]
            poster_path = top.get("poster_path")
            poster_url  = f"{_TMDB_POSTER_BASE}{poster_path}" if poster_path else None
            overview    = (top.get("overview") or "")[:200] or None
            return poster_url, overview
    except Exception as e:
        logger.warning("TMDB poster lookup failed for '%s': %s", series_title, e)
        return None, None


# ── Send batched Sonarr notification ─────────────────────────────────────────

async def _flush_batch(key: tuple, telegram_id: int) -> None:
    """
    Send the accumulated notification for a series batch.
    Called when the debounce timer fires.
    """
    batch = _batches.pop(key, None)
    if not batch:
        return

    title         = batch.title
    episode_count = batch.episode_count
    seasons       = sorted(batch.season_set)
    season_str    = ", ".join(f"sæson {s}" for s in seasons) if seasons else ""

    # Byg beskedtekst
    if episode_count == 1:
        ep_text = "1 afsnit"
    else:
        ep_text = f"{episode_count} afsnit"

    text = f"*{title}*\n\n"
    text += f"Jeg har nu gjort {ep_text} klar til dig på Plex\\!"
    if season_str:
        text += f"\n_{season_str}_"
    text += "\n\nRigtig god fornøjelse\\! 🍿"

    logger.info(
        "Flushing batch for '%s': %d episodes, seasons=%s → telegram_id=%s",
        title, episode_count, seasons, telegram_id,
    )

    try:
        if batch.poster_url:
            # Send som foto med caption
            caption = text
            await _bot.send_photo(
                chat_id=telegram_id,
                photo=batch.poster_url,
                caption=caption,
                parse_mode="MarkdownV2",
            )
        else:
            await _bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode="MarkdownV2",
            )
        logger.info("Batch notification sent to telegram_id=%s", telegram_id)
    except Exception as e:
        logger.error("Failed to send batch notification to %s: %s", telegram_id, e)


async def _schedule_flush(key: tuple, telegram_id: int) -> None:
    """Wait DEBOUNCE_SECONDS then flush the batch."""
    await asyncio.sleep(_DEBOUNCE_SECONDS)
    await _flush_batch(key, telegram_id)


# ── Radarr handler ────────────────────────────────────────────────────────────

async def handle_radarr_webhook(payload: dict) -> None:
    """Process Radarr webhook — send one notification per film immediately."""
    event_type = payload.get("eventType", "")
    if event_type not in _ACCEPTED_RADARR:
        logger.debug("Radarr webhook ignored: eventType=%s", event_type)
        return

    movie = payload.get("movie", {}) or {}
    title = movie.get("title", "Testfilm")
    year  = movie.get("year", "")
    tags  = movie.get("tags", [])

    logger.info("Radarr webhook: eventType=%s title='%s' tags=%s", event_type, title, tags)

    safe_title = _escape_md(title)
    safe_year  = _escape_md(str(year)) if year else ""

    text = f"🎬 *{safe_title}*"
    if safe_year:
        text += f" \\({safe_year}\\)"
    text += " er nu klar på Plex\\! Rigtig god fornøjelse\\! 🍿"

    await _notify_users(tags=tags, message=text, send_to_all_on_empty=True)


# ── Sonarr handler ────────────────────────────────────────────────────────────

async def handle_sonarr_webhook(payload: dict) -> None:
    """
    Process Sonarr webhook — batch episodes and send one notification per series.
    """
    event_type = payload.get("eventType", "")
    if event_type not in _ACCEPTED_SONARR:
        logger.debug("Sonarr webhook ignored: eventType=%s", event_type)
        return

    series   = payload.get("series", {}) or {}
    episodes = payload.get("episodes", [{}])
    episode  = episodes[0] if episodes else {}

    title  = series.get("title", "Testserie")
    season = episode.get("seasonNumber")
    tags   = series.get("tags", [])

    logger.info(
        "Sonarr webhook: eventType=%s title='%s' season=%s tags=%s",
        event_type, title, season, tags,
    )

    # Find alle matchende brugere
    all_users = await database.get_all_whitelisted_users()
    tag_labels = {str(t).lower() for t in tags}

    if not tags or tags == ["test-tag"]:
        recipients = all_users
    else:
        recipients = [
            u for u in all_users
            if (u.get("plex_username") or "").lower() in tag_labels
        ]

    if not recipients:
        logger.info("Sonarr webhook: ingen matchende brugere for tags=%s", tags)
        return

    for user in recipients:
        telegram_id = user["telegram_id"]
        key = (title.lower(), telegram_id)

        if key not in _batches:
            # Første afsnit — hent poster asynkront og opret batch
            poster_url, overview = await _fetch_poster_url(title)
            _batches[key] = _SeriesBatch(
                title=title,
                poster_url=poster_url,
                overview=overview,
            )

        batch = _batches[key]
        batch.episode_count += len(episodes) or 1
        if season is not None:
            batch.season_set.add(season)

        # Nulstil timer
        if batch.timer_task and not batch.timer_task.done():
            batch.timer_task.cancel()

        batch.timer_task = asyncio.create_task(
            _schedule_flush(key, telegram_id)
        )

        logger.info(
            "Sonarr batch updated for '%s' user=%s: %d episodes, timer reset",
            title, telegram_id, batch.episode_count,
        )


# ── Shared notify helper (Radarr) ─────────────────────────────────────────────

async def _notify_users(
    tags: list,
    message: str,
    send_to_all_on_empty: bool = False,
) -> None:
    """Send Telegram notification to matching users."""
    all_users = await database.get_all_whitelisted_users()

    if (not tags or tags == ["test-tag"]) and send_to_all_on_empty:
        recipients = all_users
    else:
        tag_labels = {str(t).lower() for t in tags}
        recipients = [
            u for u in all_users
            if (u.get("plex_username") or "").lower() in tag_labels
        ]

    if not recipients:
        logger.info("Webhook: ingen matchende brugere for tags=%s", tags)
        return

    for user in recipients:
        try:
            await _bot.send_message(
                chat_id=user["telegram_id"],
                text=message,
                parse_mode="MarkdownV2",
            )
            logger.info("Notification sent to telegram_id=%s", user["telegram_id"])
        except Exception as e:
            logger.error("Failed to notify telegram_id=%s: %s", user["telegram_id"], e)


# ── MarkdownV2 escape helper ──────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)