"""
services/webhook_service.py - Webhook handler for Radarr og Sonarr.

CHANGES vs previous version (v0.9.8 — Sonarr tag-format fix):
  - KRITISK FIX: _resolve_telegram_ids() håndterer nu BEGGE tag-formater:
    * Radarr sender integer tag-IDs i payloadet: tags=[3]
      → slås op mod tag-map: {3: "tg_123456789"} → telegram_id=123456789
    * Sonarr sender string labels direkte i payloadet: tags=["tg_6465421173"]
      → parses direkte uden API-opslag
    Logs viste: tag_ids=['tg_6465421173'] → "ingen tg_-tags fundet"
    fordi tag_map.get('tg_6465421173', '') matcher ikke integer-keys.
    Notifikationer for Sonarr-downloads virkede aldrig — nu er det fixet.

UNCHANGED:
  - Debounce-logik (90 sekunder), _SeriesBatch, _schedule_flush — uændret.
  - _notify_users(), _fetch_poster_url(), _escape_md() — uændret.
  - handle_radarr_webhook(), handle_sonarr_webhook() — uændret.
  - Broadcast-logik er stadig FJERNET — kun bestiller modtager notifikation.
"""

import asyncio
import logging
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
    Resolver tag-IDs fra Radarr/Sonarr webhook-payloads til telegram_ids.

    Radarr og Sonarr sender tag-data i forskellige formater:
      - Radarr: integer tag-IDs → tags=[3, 7]
        Disse slås op mod Radarr's tag-liste: {3: "tg_123456789"} → 123456789
      - Sonarr: string labels direkte → tags=["tg_6465421173"]
        Disse parses direkte uden API-opslag (label starter med "tg_")

    Funktionen håndterer begge formater i samme løkke:
      1. Hvis elementet er en string der starter med "tg_" → parse direkte
      2. Hvis elementet er et integer → slå op i tag-map fra API
    """
    if not tag_ids:
        return []

    # Lazy-load tag-map — kun nødvendigt hvis der er integer IDs
    _tag_map_cache: dict[int, str] | None = None

    async def _get_tag_map() -> dict[int, str]:
        nonlocal _tag_map_cache
        if _tag_map_cache is None:
            if source == "radarr":
                _tag_map_cache = await radarr_get_all_tags()
            else:
                _tag_map_cache = await sonarr_get_all_tags()
        return _tag_map_cache

    telegram_ids = []

    for tid in tag_ids:
        # ── Format A: string label direkte (Sonarr) ───────────────────────────
        # Sonarr sender labels som strings: ["tg_6465421173"]
        if isinstance(tid, str):
            if tid.startswith("tg_"):
                try:
                    telegram_id = int(tid[3:])
                    telegram_ids.append(telegram_id)
                    logger.debug("Tag string '%s' → telegram_id=%s", tid, telegram_id)
                except ValueError:
                    logger.warning("Ugyldigt tg_-tag format (string): '%s'", tid)
            else:
                logger.debug("String tag '%s' er ikke et tg_-tag — ignorerer", tid)
            continue

        # ── Format B: integer tag-ID (Radarr) ────────────────────────────────
        # Radarr sender integer IDs: [3] → slå op: {3: "tg_123456789"}
        if isinstance(tid, int):
            tag_map = await _get_tag_map()
            label   = tag_map.get(tid, "")
            if label.startswith("tg_"):
                try:
                    telegram_id = int(label[3:])
                    telegram_ids.append(telegram_id)
                    logger.debug("Tag int %s ('%s') → telegram_id=%s", tid, label, telegram_id)
                except ValueError:
                    logger.warning("Ugyldigt tg_-tag format (int-lookup): '%s'", label)
            else:
                logger.debug("Tag int %s label='%s' er ikke et tg_-tag — ignorerer", tid, label)
            continue

        logger.warning("Ukendt tag-type: %r (%s)", tid, type(tid).__name__)

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