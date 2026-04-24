"""
services/confirmation_service.py - Inline keyboard bekræftelsesflow.

CHANGES vs previous version:
  - show_confirmation() refaktoreret fundamentalt:
    * Signaturen er nu (trigger, context, token, plex_username) hvor
      trigger er enten en CallbackQuery ELLER en Message.
    * chat_id og user_id udtrækkes via duck-typing på trigger-objektet.
    * Besked slettes KUN hvis trigger er en CallbackQuery.
    * Sender via context.bot.send_photo() — ingen _MsgAdapter, ingen hack.
  - handle_info_command() er fjernet — main.py kalder show_confirmation direkte.
  - execute_order() sender telegram_id som tag til Radarr/Sonarr — uændret.
"""

import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import ContextTypes

import database
from services.plex_service import check_library, STATUS_FOUND
from services.radarr_service import add_movie
from services.sonarr_service import add_series
from services.tmdb_service import get_media_details, search_media

logger = logging.getLogger(__name__)


def _make_token() -> str:
    return secrets.token_hex(8)


def _star_rating(score: float | None) -> str:
    """Konverter TMDB vote_average (0-10) til stjerner (0-5)."""
    if not score:
        return "–"
    stars = round(score / 2)
    return "⭐" * stars + "☆" * (5 - stars)


def _esc(text: str) -> str:
    """Escape MarkdownV2 specialtegn."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_caption(details: dict) -> str:
    """Byg rig MarkdownV2-caption til send_photo."""
    title    = details.get("title", "Ukendt")
    year     = (details.get("release_date") or details.get("first_air_date") or "")[:4]
    genres   = details.get("genres", [])[:3]
    rating   = details.get("vote_average")
    overview = details.get("overview") or "Ingen beskrivelse."
    cast     = details.get("cast", [])
    runtime  = details.get("runtime_minutes")
    seasons  = details.get("number_of_seasons")
    tagline  = details.get("tagline") or ""

    lines = []

    # Titel + årstal
    title_line = f"*{_esc(title)}*"
    if year:
        title_line += f" \\({_esc(year)}\\)"
    lines.append(title_line)

    # Tagline
    if tagline:
        lines.append(f"_{_esc(tagline)}_")

    lines.append("")

    # Genrer + rating
    genre_str  = " · ".join(genres) if genres else ""
    rating_str = _star_rating(rating)
    if genre_str:
        lines.append(f"🎭 {_esc(genre_str)}  {rating_str}")
    else:
        lines.append(rating_str)

    # Varighed / sæsoner
    if runtime:
        lines.append(f"⏱ {runtime} min")
    elif seasons:
        lines.append(f"📺 {seasons} sæson{'er' if seasons != 1 else ''}")

    # Cast
    if cast:
        lines.append(f"🎬 {_esc(', '.join(cast))}")

    lines.append("")

    # Beskrivelse
    desc = overview[:250]
    if len(overview) > 250:
        desc += "…"
    lines.append(_esc(desc))

    return "\n".join(lines)


# ── Step 1: Vis søgeresultater som knapper ────────────────────────────────────

async def show_search_results(
    message: Message,
    query: str,
    media_type: str = "both",
) -> bool:
    """Search TMDB og præsenter top resultater som inline buttons."""
    results = await search_media(query, media_type)
    if not results:
        await message.reply_text(f"Jeg kunne ikke finde noget for '{query}' 🤔")
        return False

    top = results[:5]
    buttons = []
    for item in top:
        title   = item.get("title") or "Ukendt"
        year    = (item.get("release_date") or item.get("first_air_date") or "")[:4]
        mtype   = item.get("media_type", media_type if media_type != "both" else "movie")
        tmdb_id = item.get("id")
        label   = f"{title} ({year})" if year else title

        token = _make_token()
        await database.save_pending_request(token, message.chat_id, {
            "media_type": mtype,
            "tmdb_id":    tmdb_id,
            "title":      title,
            "year":       int(year) if year else None,
            "step":       "picked",
        })
        buttons.append([InlineKeyboardButton(label, callback_data=f"pick:{token}")])

    buttons.append([InlineKeyboardButton("❌ Annuller", callback_data="cancel:none")])

    await message.reply_text(
        f"Jeg fandt disse resultater for *{query}* — hvilken mener du?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return True


# ── Step 2: Netflix-look visning med plakat ───────────────────────────────────

async def show_confirmation(
    trigger,
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
    plex_username: str | None,
) -> None:
    """
    Hent fuld detaljer og vis Netflix-look med plakat og interaktive knapper.

    trigger er enten en CallbackQuery eller en Message:
      - CallbackQuery: trigger.message.chat.id / trigger.from_user.id
      - Message:       trigger.chat.id          / trigger.from_user.id

    Besked slettes KUN ved CallbackQuery (vi kan ikke slette brugerens besked).
    Sender altid via context.bot — ingen adapters, ingen hacks.
    """
    from telegram import CallbackQuery

    pending = await database.get_pending_request(token)
    if not pending:
        if isinstance(trigger, CallbackQuery):
            await trigger.edit_message_text("Sessionen er udløbet — prøv igen.")
        else:
            await trigger.reply_text("Sessionen er udløbet — prøv igen.")
        return

    tmdb_id    = pending["tmdb_id"]
    media_type = pending["media_type"]

    details = await get_media_details(tmdb_id, media_type)
    if not details:
        if isinstance(trigger, CallbackQuery):
            await trigger.edit_message_text("Kunne ikke hente detaljer — prøv igen.")
        else:
            await trigger.reply_text("Kunne ikke hente detaljer — prøv igen.")
        return

    title       = details.get("title") or pending.get("title", "Ukendt")
    year        = details.get("release_date", details.get("first_air_date", ""))[:4]
    genres      = details.get("genres", [])
    orig_lang   = details.get("original_language", "en")
    tvdb_id     = details.get("tvdb_id")
    seasons     = details.get("season_numbers", [])
    trailer_url = details.get("trailer_url")
    poster_url  = details.get("poster_url")

    # ── Udled chat_id og user_id fra trigger ──────────────────────────────────
    if isinstance(trigger, CallbackQuery):
        chat_id = trigger.message.chat.id
        user_id = trigger.from_user.id
    else:
        # trigger er en Message
        chat_id = trigger.chat.id
        user_id = trigger.from_user.id

    # ── Gem fuld data til execute_order ───────────────────────────────────────
    new_token = _make_token()
    await database.save_pending_request(new_token, user_id, {
        "media_type":        media_type,
        "tmdb_id":           tmdb_id,
        "tvdb_id":           tvdb_id,
        "title":             title,
        "year":              int(year) if year else None,
        "genres":            genres,
        "original_language": orig_lang,
        "season_numbers":    seasons,
        "trailer_url":       trailer_url,
        "telegram_id":       user_id,
    })

    # ── Tjek om titlen er på Plex ─────────────────────────────────────────────
    plex_check = await check_library(
        title, int(year) if year else None, media_type, plex_username
    )
    on_plex    = plex_check.get("status") == STATUS_FOUND
    rating_key = plex_check.get("ratingKey")
    machine_id = plex_check.get("machineIdentifier")

    # ── Byg knap-rækker ───────────────────────────────────────────────────────
    button_rows = []

    if on_plex and rating_key and machine_id:
        plex_url = (
            f"https://app.plex.tv/desktop/#!/server/{machine_id}"
            f"/details?key=/library/metadata/{rating_key}"
        )
        button_rows.append([InlineKeyboardButton("▶️ Se på Plex", url=plex_url)])
    else:
        button_rows.append([
            InlineKeyboardButton("➕ Tilføj til Plex", callback_data=f"confirm:{new_token}")
        ])

    button_rows.append([
        InlineKeyboardButton("📌 Tilføj til Watchlist", callback_data=f"watchlist:{new_token}")
    ])

    if trailer_url:
        button_rows.append([InlineKeyboardButton("🎬 Se Trailer", url=trailer_url)])

    keyboard = InlineKeyboardMarkup(button_rows)
    caption  = _build_caption(details)

    # ── Slet trigger-besked KUN ved CallbackQuery ─────────────────────────────
    if isinstance(trigger, CallbackQuery):
        try:
            await trigger.message.delete()
        except Exception:
            pass  # Allerede slettet eller ingen adgang

    # ── Send infokort via context.bot ─────────────────────────────────────────
    try:
        if poster_url:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=poster_url,
                caption=caption,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
    except Exception as e:
        logger.error("show_confirmation send fejl: %s", e)
        # Absolut fallback uden MarkdownV2
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{title} ({year})\n\n{details.get('overview', '')}",
                reply_markup=keyboard,
            )
        except Exception as e2:
            logger.error("show_confirmation absolut fallback fejl: %s", e2)


# ── Step 3: Udfør bestilling ──────────────────────────────────────────────────

async def execute_order(
    query_callback,
    token: str,
    plex_username: str | None,
) -> None:
    """
    Execute Radarr/Sonarr bestilling. Sender telegram_id som tg_-tag.
    """
    pending = await database.get_pending_request(token)
    if not pending:
        await query_callback.edit_message_text("Sessionen er udløbet — prøv igen.")
        return

    media_type  = pending["media_type"]
    title       = pending["title"]
    telegram_id = pending.get("telegram_id") or query_callback.from_user.id

    await query_callback.edit_message_text(
        f"⏳ Bestiller *{title}* — vent et øjeblik…",
        parse_mode="Markdown",
    )

    if media_type == "movie":
        result = await add_movie(
            tmdb_id=pending["tmdb_id"],
            title=title,
            year=pending["year"] or 0,
            genres=pending["genres"],
            telegram_id=telegram_id,
        )
    else:
        result = await add_series(
            tvdb_id=pending["tvdb_id"],
            title=title,
            year=pending["year"] or 0,
            original_language=pending["original_language"],
            season_numbers=pending["season_numbers"],
            telegram_id=telegram_id,
        )

    if result.get("success"):
        await query_callback.edit_message_text(
            f"✅ *{title}* er bestilt og søges nu\\! "
            f"Du får besked når den er klar på Plex 🍿",
            parse_mode="MarkdownV2",
        )
    else:
        status = result.get("status", "")
        if status == "already_exists":
            msg = f"*{title}* er allerede i biblioteket\\!"
        else:
            msg = f"Noget gik galt med bestillingen af *{title}*\\. Prøv igen lidt senere\\."
        await query_callback.edit_message_text(msg, parse_mode="MarkdownV2")