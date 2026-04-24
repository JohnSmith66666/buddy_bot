"""
services/webhook_service.py - Webhook handler for Radarr og Sonarr.

CHANGES vs previous version:
  - Tag-strategi ændret fra plex_username til telegram_id-baserede tags.
    Radarr/Sonarr sender nu integer tag-IDs i payloadet (f.eks. [3]).
    _resolve_telegram_ids() slår disse IDs op mod Radarr/Sonarr's tag-liste
    og udtrækker telegram_id fra labels med præfikset "tg_" (f.eks. "tg_123456789").
  - Broadcast-logikken er FJERNET fra _notify_users() og Sonarr-handleren.
    Notifikationer sendes KUN til den bruger der bestilte via tg_-tagget.
  - _notify_users() tager nu direkte en liste af telegram_ids i stedet for
    at slå op i databasen.
  - Sonarr-handleren bruger samme _resolve_telegram_ids() som Radarr.
  - Debounce-logik og poster-fetch er uændret.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
from telegram import Bot

from config import TELEGRAM_BOT_TOKEN, TMDB_API_KEY
import database
from services.radarr_service import get_all_tags as radarr_get_all_tags
from services.sonarr_service import get_all_tags as sonarr_get_all_tags

logger = logging.getLogger(__name__)

_bot = Bot(token=TELEGRAM_BOT_TOKEN)

_ACCEPTED_RADARR = {"Download", "MovieAdded", "Test"}
_ACCEPTED_SONARR = {"Download", "EpisodeFileImported", "Test"}

_TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w500"
_DEBOUNCE_SECONDS = 90


# ── Debounce buffer ───────────────────────────────────────────────────────────

@dataclass
class _SeriesBatch:
    title:         str
    episode_count: int = 0
    season_set:    set = field(default_factory=set)
    poster_url:    str | None = None
    overview:      str | None = None
    timer_task:    asyncio.Task | None = None


_batches: dict[tuple, _SeriesBatch] = {}


# ── Tag → telegram_id resolver ────────────────────────────────────────────────

async def _resolve_telegram_ids(tag_ids: list, source: str) -> list[int]:
    """
    Slår integer tag-IDs op mod Radarr/Sonarr's tag-liste og returnerer
    en liste af telegram_ids for tags med præfikset "tg_".

    Eksempel: tag_ids=[3], Radarr har tag {3: "tg_123456789"}
              → returnerer [123456789]

    Args:
        tag_ids: Liste af integer tag-IDs fra webhook-payloadet.
        source:  "radarr" eller "sonarr" — bestemmer hvilken service der spørges.
    """
    if not tag_ids:
        return []

    if source == "radarr":
        tag_map = await radarr_get_all_tags()
    else:
        tag_map = await sonarr_get_all_tags()

    telegram_ids = []
    for tid in tag_ids:
        label = tag_map.get(tid, "")
        if label.startswith("tg_"):
            try:
                telegram_id = int(label[3:])
                telegram_ids.append(telegram_id)
                logger.debug("Tag %s ('%s') → telegram_id=%s", tid, label, telegram_id)
            except ValueError:
                logger.warning("Ugyldigt tg_-tag format: '%s'", label)

    return telegram_ids


# ── TMDB poster lookup ────────────────────────────────────────────────────────

async def _fetch_poster_url(series_title: str) -> tuple[str | None, str | None]:
    """Search TMDB for the series and return (poster_url, overview)."""
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


# ── Send notification to specific users ──────────────────────────────────────

async def _notify_users(telegram_ids: list[int], message: str) -> None:
    """Send Telegram MarkdownV2 notification to a specific list of telegram_ids."""
    if not telegram_ids:
        logger.info("_notify_users: ingen modtagere — notifikation ikke sendt")
        return

    for telegram_id in telegram_ids:
        try:
            await _bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="MarkdownV2",
            )
            logger.info("Notification sent to telegram_id=%s", telegram_id)
        except Exception as e:
            logger.error("Failed to notify telegram_id=%s: %s", telegram_id, e)


# ── MarkdownV2 escape helper ──────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)


# ── Radarr handler ────────────────────────────────────────────────────────────

async def handle_radarr_webhook(payload: dict) -> None:
    """
    Process Radarr webhook — send notification only to the user who ordered.

    Tag-IDs fra payloadet slås op mod Radarr's tag-liste.
    Kun brugere med tg_<telegram_id>-tags modtager notifikationen.
    """
    event_type = payload.get("eventType", "")
    if event_type not in _ACCEPTED_RADARR:
        logger.debug("Radarr webhook ignored: eventType=%s", event_type)
        return

    movie   = payload.get("movie", {}) or {}
    title   = movie.get("title", "Testfilm")
    year    = movie.get("year", "")
    tag_ids = movie.get("tags", [])

    logger.info("Radarr webhook: eventType=%s title='%s' tag_ids=%s", event_type, title, tag_ids)

    telegram_ids = await _resolve_telegram_ids(tag_ids, source="radarr")

    if not telegram_ids:
        logger.info("Radarr webhook: ingen tg_-tags fundet for '%s', tag_ids=%s", title, tag_ids)
        return

    safe_title = _escape_md(title)
    safe_year  = _escape_md(str(year)) if year else ""

    text = f"🎬 *{safe_title}*"
    if safe_year:
        text += f" \\({safe_year}\\)"
    text += " er nu klar på Plex\\! Rigtig god fornøjelse\\! 🍿"

    await _notify_users(telegram_ids, text)


# ── Sonarr handler ────────────────────────────────────────────────────────────

async def handle_sonarr_webhook(payload: dict) -> None:
    """
    Process Sonarr webhook — batch episodes og send notifikation
    kun til den bruger der bestilte via tg_-tagget.
    """
    event_type = payload.get("eventType", "")
    if event_type not in _ACCEPTED_SONARR:
        logger.debug("Sonarr webhook ignored: eventType=%s", event_type)
        return

    series   = payload.get("series", {}) or {}
    episodes = payload.get("episodes", [{}])
    episode  = episodes[0] if episodes else {}

    title   = series.get("title", "Testserie")
    season  = episode.get("seasonNumber")
    tag_ids = series.get("tags", [])

    logger.info(
        "Sonarr webhook: eventType=%s title='%s' season=%s tag_ids=%s",
        event_type, title, season, tag_ids,
    )

    telegram_ids = await _resolve_telegram_ids(tag_ids, source="sonarr")

    if not telegram_ids:
        logger.info("Sonarr webhook: ingen tg_-tags fundet for '%s', tag_ids=%s", title, tag_ids)
        return

    for telegram_id in telegram_ids:
        key = (title.lower(), telegram_id)

        if key not in _batches:
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

        if batch.timer_task and not batch.timer_task.done():
            batch.timer_task.cancel()

        batch.timer_task = asyncio.create_task(
            _schedule_flush(key, telegram_id)
        )

        logger.info(
            "Sonarr batch updated for '%s' user=%s: %d episodes, timer reset",
            title, telegram_id, batch.episode_count,
        )


# ── Send batched Sonarr notification ─────────────────────────────────────────

async def _flush_batch(key: tuple, telegram_id: int) -> None:
    """Send the accumulated notification for a series batch."""
    batch = _batches.pop(key, None)
    if not batch:
        return

    title         = batch.title
    episode_count = batch.episode_count
    seasons       = sorted(batch.season_set)
    season_str    = ", ".join(f"sæson {s}" for s in seasons) if seasons else ""

    ep_text = "1 afsnit" if episode_count == 1 else f"{episode_count} afsnit"

    text = f"*{_escape_md(title)}*\n\n"
    text += f"Jeg har nu gjort {ep_text} klar til dig på Plex\\!"
    if season_str:
        text += f"\n_{_escape_md(season_str)}_"
    text += "\n\nRigtig god fornøjelse\\! 🍿"

    logger.info(
        "Flushing batch for '%s': %d episodes, seasons=%s → telegram_id=%s",
        title, episode_count, seasons, telegram_id,
    )

    try:
        if batch.poster_url:
            await _bot.send_photo(
                chat_id=telegram_id,
                photo=batch.poster_url,
                caption=text,
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