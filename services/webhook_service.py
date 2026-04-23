"""
services/webhook_service.py - Webhook handler for Radarr og Sonarr notifikationer.

Når Radarr eller Sonarr sender en "On Import" webhook, finder vi alle brugere
med et matchende tag og sender dem en Telegram-besked.

Setup i Radarr/Sonarr:
  Settings → Connect → Webhook
  URL: https://<din-railway-url>/webhook/radarr  (eller /webhook/sonarr)
  Trigger: On Import
"""

import logging

import httpx
from telegram import Bot

from config import TELEGRAM_BOT_TOKEN
import database

logger = logging.getLogger(__name__)

_bot = Bot(token=TELEGRAM_BOT_TOKEN)


async def handle_radarr_webhook(payload: dict) -> None:
    """
    Process an incoming Radarr webhook.
    Sends a Telegram notification to all users tagged in the movie.
    """
    event_type = payload.get("eventType", "")
    if event_type not in ("Download", "MovieAdded"):
        logger.debug("Radarr webhook ignored: eventType=%s", event_type)
        return

    movie = payload.get("movie", {})
    title = movie.get("title", "Ukendt film")
    year  = movie.get("year", "")

    tags = payload.get("movie", {}).get("tags", [])
    logger.info("Radarr webhook: '%s' (%s) — tags=%s", title, year, tags)

    await _notify_tagged_users(
        tags=tags,
        message=f"🍿 *{title}* ({year}) er nu klar på Plex\\! Rigtig god fornøjelse\\!",
    )


async def handle_sonarr_webhook(payload: dict) -> None:
    """
    Process an incoming Sonarr webhook.
    Sends a Telegram notification to all users tagged in the series.
    """
    event_type = payload.get("eventType", "")
    if event_type not in ("Download", "EpisodeFileImported"):
        logger.debug("Sonarr webhook ignored: eventType=%s", event_type)
        return

    series  = payload.get("series", {})
    episode = payload.get("episodes", [{}])[0] if payload.get("episodes") else {}
    title   = series.get("title", "Ukendt serie")
    season  = episode.get("seasonNumber", "?")
    ep_num  = episode.get("episodeNumber", "?")
    ep_title = episode.get("title", "")

    tags = series.get("tags", [])
    logger.info("Sonarr webhook: '%s' S%sE%s — tags=%s", title, season, ep_num, tags)

    ep_str = f"S{season:02d}E{ep_num:02d}" if isinstance(season, int) else f"S{season}E{ep_num}"
    msg = f"📺 *{title}* — {ep_str}"
    if ep_title:
        msg += f" \"{ep_title}\""
    msg += " er nu klar på Plex\\! Rigtig god fornøjelse\\! 🍿"

    await _notify_tagged_users(tags=tags, message=msg)


async def _notify_tagged_users(tags: list, message: str) -> None:
    """
    Look up all whitelisted users whose plex_username matches one of the tags,
    and send them a Telegram message.
    """
    if not tags:
        logger.info("Webhook: ingen tags — sender ingen notifikation")
        return

    # tags kan være enten strenge (labels) eller integers (IDs fra Radarr/Sonarr)
    tag_labels = [str(t).lower() for t in tags]

    all_users = await database.get_all_whitelisted_users()
    notified = 0

    for user in all_users:
        plex_username = user.get("plex_username") or ""
        if plex_username.lower() in tag_labels:
            try:
                await _bot.send_message(
                    chat_id=user["telegram_id"],
                    text=message,
                    parse_mode="MarkdownV2",
                )
                notified += 1
                logger.info(
                    "Webhook notification sent to telegram_id=%s (plex='%s')",
                    user["telegram_id"], plex_username,
                )
            except Exception as e:
                logger.error(
                    "Failed to notify telegram_id=%s: %s",
                    user["telegram_id"], e,
                )

    logger.info("Webhook: notified %d user(s)", notified)