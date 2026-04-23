"""
services/webhook_service.py - Webhook handler for Radarr og Sonarr notifikationer.

Når Radarr eller Sonarr sender en webhook, finder vi alle brugere
med et matchende tag og sender dem en Telegram-besked.

Setup i Radarr/Sonarr:
  Settings → Connect → Webhook
  URL: https://<din-railway-url>/webhook/radarr  (eller /webhook/sonarr)
  Triggers: On Import, On Download

VIGTIGT: Fjern "Test" fra _ACCEPTED_RADARR og _ACCEPTED_SONARR når
test er bekræftet at virke — ellers sender botten besked ved hver test-klik.
"""

import logging

from telegram import Bot

from config import TELEGRAM_BOT_TOKEN
import database

logger = logging.getLogger(__name__)

_bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Accepterede event types — fjern "Test" efter verifikation
_ACCEPTED_RADARR = {"Download", "MovieAdded", "Test"}
_ACCEPTED_SONARR = {"Download", "EpisodeFileImported", "Test"}


async def handle_radarr_webhook(payload: dict) -> None:
    """Process an incoming Radarr webhook."""
    event_type = payload.get("eventType", "")
    if event_type not in _ACCEPTED_RADARR:
        logger.debug("Radarr webhook ignored: eventType=%s", event_type)
        return

    movie = payload.get("movie", {}) or {}
    title = movie.get("title", "Testfilm")
    year  = movie.get("year", "")
    tags  = movie.get("tags", [])

    logger.info("Radarr webhook: eventType=%s title='%s' tags=%s", event_type, title, tags)

    message = f"🍿 *{title}*"
    if year:
        message += f" \\({year}\\)"
    message += " er nu klar på Plex\\! Rigtig god fornøjelse\\!"

    await _notify_users(tags=tags, message=message, send_to_all_on_empty=True)


async def handle_sonarr_webhook(payload: dict) -> None:
    """Process an incoming Sonarr webhook."""
    event_type = payload.get("eventType", "")
    if event_type not in _ACCEPTED_SONARR:
        logger.debug("Sonarr webhook ignored: eventType=%s", event_type)
        return

    series   = payload.get("series", {}) or {}
    episodes = payload.get("episodes", [{}])
    episode  = episodes[0] if episodes else {}

    title    = series.get("title", "Testserie")
    season   = episode.get("seasonNumber", "?")
    ep_num   = episode.get("episodeNumber", "?")
    ep_title = episode.get("title", "")
    tags     = series.get("tags", [])

    logger.info("Sonarr webhook: eventType=%s title='%s' tags=%s", event_type, title, tags)

    try:
        ep_str = f"S{int(season):02d}E{int(ep_num):02d}"
    except (ValueError, TypeError):
        ep_str = f"S{season}E{ep_num}"

    message = f"📺 *{title}* — {ep_str}"
    if ep_title:
        safe_ep = ep_title.replace("!", "\\!").replace(".", "\\.").replace("-", "\\-")
        message += f" _{safe_ep}_"
    message += " er nu klar på Plex\\! Rigtig god fornøjelse\\! 🍿"

    await _notify_users(tags=tags, message=message, send_to_all_on_empty=True)


async def _notify_users(
    tags: list,
    message: str,
    send_to_all_on_empty: bool = False,
) -> None:
    """
    Send Telegram notification to users whose plex_username matches a tag.

    If tags is empty and send_to_all_on_empty is True, notify all
    whitelisted users (used for test events where Radarr sends no tags).
    """
    all_users = await database.get_all_whitelisted_users()

    if not tags and send_to_all_on_empty:
        logger.info("Webhook: ingen tags — sender til alle %d whitelisted brugere", len(all_users))
        recipients = all_users
    else:
        tag_labels = {str(t).lower() for t in tags}
        recipients = [
            u for u in all_users
            if (u.get("plex_username") or "").lower() in tag_labels
        ]

    if not recipients:
        logger.info("Webhook: ingen matchende brugere fundet for tags=%s", tags)
        return

    notified = 0
    for user in recipients:
        try:
            await _bot.send_message(
                chat_id=user["telegram_id"],
                text=message,
                parse_mode="MarkdownV2",
            )
            notified += 1
            logger.info(
                "Webhook notification sent to telegram_id=%s (plex='%s')",
                user["telegram_id"], user.get("plex_username"),
            )
        except Exception as e:
            logger.error(
                "Failed to notify telegram_id=%s: %s",
                user["telegram_id"], e,
            )

    logger.info("Webhook: notified %d user(s)", notified)