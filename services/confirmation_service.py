"""
services/confirmation_service.py - Inline keyboard bekræftelsesflow.

CHANGES vs previous version:
  - IMDb-rating hentes nu direkte fra Plex (item.rating) når filmen er på serveren.
    Fallback til TMDB vote_average hvis Plex ikke har en rating (f.eks. ny titel).
    Formatering: 🎬 IMDb: {score}/10 — vises i caption som hidtil.
  - _build_caption() tager nu et valgfrit rating-argument.
  - handle_watchlist_callback keyboard-opdatering — uændret.
  - parse_mode="Markdown" overalt — uændret.
"""

import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

import database
from services.plex_service import check_library, STATUS_FOUND
from services.radarr_service import add_movie
from services.sonarr_service import add_series
from services.tmdb_service import get_media_details, search_media

logger = logging.getLogger(__name__)

_MAX_CAPTION = 1024  # Telegrams max caption-længde for billeder


def _make_token() -> str:
    return secrets.token_hex(8)


def _build_caption(details: dict, rating: float | None = None) -> str:
    """
    Byg simpel Markdown-caption (ikke MarkdownV2) til send_photo.

    rating: Plex IMDb-score (foretrukket) eller TMDB vote_average (fallback).
    Vises som: 🎬 IMDb: {score}/10

    Overview trimmes til Telegrams max caption-grænse på 1024 tegn.
    """
    title    = details.get("title") or "Ukendt"
    year     = (details.get("release_date") or details.get("first_air_date") or "")[:4]
    genres   = details.get("genres", [])[:3]
    score    = rating if rating is not None else details.get("vote_average")
    overview = (details.get("overview") or "Ingen beskrivelse.").strip()
    cast     = details.get("cast") or []
    runtime  = details.get("runtime_minutes")
    seasons  = details.get("number_of_seasons")

    lines = []

    # Titel + årstal
    header = f"*{title}*"
    if year:
        header += f" ({year})"
    lines.append(header)
    lines.append("")

    # Genrer
    if genres:
        lines.append(f"🎬 {', '.join(genres)}")

    # Score — Plex IMDb foretrukket, TMDB som fallback
    if score:
        lines.append(f"🎬 IMDb: {score}/10")

    # Varighed / sæsoner
    if runtime:
        lines.append(f"⏱ {runtime} min")
    elif seasons:
        lines.append(f"📺 {seasons} sæson{'er' if seasons != 1 else ''}")

    # Skuespillere
    if cast:
        cast_str = cast if isinstance(cast, str) else ", ".join(cast)
        lines.append(f"🎭 {cast_str}")

    lines.append("")

    header_text = "\n".join(lines)

    # Overview — trim så caption ikke overstiger 1024 tegn
    available = _MAX_CAPTION - len(header_text) - 3
    if available <= 0:
        return header_text.strip()
    if len(overview) > available:
        overview = overview[:available] + "…"

    return header_text + overview


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
    Hent fuld detaljer og vis Netflix-look infokort med plakat.

    Bruger parse_mode="Markdown" (ikke MarkdownV2) for at undgå
    crash ved specialtegn i filmtitler og beskrivelser.

    Knap-logik:
      - PÅ PLEX med keys → [▶️ Se på Plex] (deep-link URL-encoded)
      - PÅ PLEX uden keys → [▶️ Se på Plex] (generisk)
      - IKKE PÅ PLEX → [➕ Tilføj til Plex]
      - ALTID → [📌 Tilføj til Watchlist]
      - HVIS trailer → [🎬 Se Trailer]
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
    machine_id = plex_check.get("machineIdentifier", "")
    rating_key = plex_check.get("ratingKey", "")
    plex_rating = plex_check.get("rating")  # IMDb-score fra Plex, None hvis ikke på serveren

    logger.info(
        "Rating — Plex: %s, TMDB: %s, bruger: %s",
        plex_rating,
        details.get("vote_average"),
        "Plex" if plex_rating is not None else "TMDB (fallback)",
    )

    # ── Byg knap-rækker ───────────────────────────────────────────────────────
    button_rows = []

    if on_plex:
        plex_url = (
            f"https://app.plex.tv/desktop/#!/server/{machine_id}"
            f"/details?key=/library/metadata/{rating_key}"
        )
        logger.info(
            "Plex URL genereret — machineId=%r ratingKey=%r url=%s",
            machine_id, rating_key, plex_url,
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
    caption  = _build_caption(details, rating=plex_rating)

    # ── Slet trigger-besked KUN ved CallbackQuery ─────────────────────────────
    if isinstance(trigger, CallbackQuery):
        try:
            await trigger.message.delete()
        except Exception:
            pass

    # ── Send infokort via context.bot (Markdown — ikke MarkdownV2) ───────────
    try:
        if poster_url:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=poster_url,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
    except Exception as e:
        logger.error("show_confirmation send fejl: %s", e)
        # Absolut fallback — ingen parse_mode
        try:
            plain = f"{title} ({year})\n\n{details.get('overview', '')}"
            await context.bot.send_message(
                chat_id=chat_id,
                text=plain,
                reply_markup=keyboard,
            )
        except Exception as e2:
            logger.error("show_confirmation absolut fallback fejl: %s", e2)


# ── Watchlist callback ────────────────────────────────────────────────────────

async def handle_watchlist_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Håndterer 📌 Tilføj til Watchlist-knappen.
    Ved success: redigerer keyboardet så knappen skifter til ✅ Tilføjet til Watchlist.
    Ved fejl: viser en fejlbesked via show_alert.
    """
    from services.plex_service import add_to_watchlist

    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1]
    pending = await database.get_pending_request(token)
    if not pending:
        await query.answer("Sessionen er udløbet — prøv igen.", show_alert=True)
        return

    title         = pending["title"]
    plex_username = await database.get_plex_username(query.from_user.id)

    try:
        success = await add_to_watchlist(title, plex_username)
        if success:
            # ── Opdater keyboard: skift watchlist-knappen til grøn bekræftelse ──
            if current_keyboard := query.message.reply_markup:
                new_keyboard = []
                for row in current_keyboard.inline_keyboard:
                    new_row = []
                    for btn in row:
                        if btn.callback_data and btn.callback_data.startswith("watchlist:"):
                            new_row.append(InlineKeyboardButton(
                                "✅ Tilføjet til Watchlist",
                                callback_data=btn.callback_data,
                            ))
                        else:
                            new_row.append(btn)
                    new_keyboard.append(new_row)
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup(new_keyboard)
                    )
                except Exception as e:
                    logger.warning("Kunne ikke opdatere keyboard: %s", e)

            await query.answer("Filmen er gemt på din Watchlist! 🍿")
        else:
            await query.answer(
                f"❌ Kunne ikke finde '{title}' i Plex Discover.", show_alert=True
            )
    except Exception as e:
        logger.error("handle_watchlist_callback fejl: %s", e)
        await query.answer("❌ Noget gik galt — prøv igen.", show_alert=True)


# ── Step 3: Udfør bestilling ──────────────────────────────────────────────────

async def execute_order(
    query_callback,
    token: str,
    plex_username: str | None,
) -> None:
    """Execute Radarr/Sonarr bestilling. Sender telegram_id som tg_-tag."""
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
            f"✅ *{title}* er bestilt og søges nu! "
            f"Du får besked når den er klar på Plex 🍿",
            parse_mode="Markdown",
        )
    else:
        status = result.get("status", "")
        if status == "already_exists":
            msg = f"*{title}* er allerede i biblioteket!"
        else:
            msg = f"Noget gik galt med bestillingen af *{title}*. Prøv igen lidt senere."
        await query_callback.edit_message_text(msg, parse_mode="Markdown")