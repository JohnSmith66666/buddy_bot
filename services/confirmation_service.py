"""
services/confirmation_service.py - Inline keyboard bekræftelsesflow.

CHANGES vs previous version:
  - show_confirmation() er fundamentalt omskrevet til et "Netflix-look":
    * Sletter den gamle tekstbesked og sender i stedet send_photo() med
      TMDB poster som billede og rig caption med emojis.
    * Tjekker check_library for at vise den rette knap:
      - PÅ PLEX:    [▶️ Se på Plex] (deep-link via ratingKey + machineIdentifier)
      - IKKE PLEX:  [➕ Tilføj til Plex] (confirm:token flow)
    * Altid: [📌 Tilføj til Watchlist] + [🎬 Se Trailer] (hvis trailer_url).
  - Ny info_handler() til /info_movie_<id> og /info_tv_<id> kommandoer.
    Opretter pending token og kalder show_confirmation direkte.
  - execute_order() sender nu telegram_id til Radarr/Sonarr (uændret).
"""

import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

import database
from services.plex_service import check_library, STATUS_FOUND
from services.radarr_service import add_movie
from services.sonarr_service import add_series
from services.tmdb_service import get_media_details, search_media

logger = logging.getLogger(__name__)

_TMDB_POSTER = "https://image.tmdb.org/t/p/w500"


def _make_token() -> str:
    return secrets.token_hex(8)


def _star_rating(score: float | None) -> str:
    """Konverter TMDB vote_average (0-10) til stjerner (0-5)."""
    if not score:
        return "–"
    stars = round(score / 2)
    return "⭐" * stars + "☆" * (5 - stars)


def _build_caption(details: dict) -> str:
    """Byg rig MarkdownV2-caption til send_photo."""
    title       = details.get("title", "Ukendt")
    year        = (details.get("release_date") or details.get("first_air_date") or "")[:4]
    genres      = details.get("genres", [])[:3]
    rating      = details.get("vote_average")
    overview    = details.get("overview") or "Ingen beskrivelse."
    cast        = details.get("cast", [])
    runtime     = details.get("runtime_minutes")
    seasons     = details.get("number_of_seasons")
    tagline     = details.get("tagline") or ""

    def esc(text: str) -> str:
        """Escape MarkdownV2 specialtegn."""
        for ch in r"\_*[]()~`>#+-=|{}.!":
            text = text.replace(ch, f"\\{ch}")
        return text

    lines = []
    # Titel + årstal
    title_line = f"*{esc(title)}*"
    if year:
        title_line += f" \\({esc(year)}\\)"
    lines.append(title_line)

    # Tagline
    if tagline:
        lines.append(f"_{esc(tagline)}_")

    lines.append("")  # blank linje

    # Genrer + rating
    genre_str = " · ".join(genres) if genres else ""
    rating_str = _star_rating(rating)
    if genre_str:
        lines.append(f"🎭 {esc(genre_str)}  {rating_str}")
    else:
        lines.append(rating_str)

    # Varighed / sæsoner
    if runtime:
        lines.append(f"⏱ {runtime} min")
    elif seasons:
        lines.append(f"📺 {seasons} sæson{'er' if seasons != 1 else ''}")

    # Cast
    if cast:
        lines.append(f"🎬 {esc(', '.join(cast))}")

    lines.append("")

    # Beskrivelse
    desc = overview[:250]
    if len(overview) > 250:
        desc += "…"
    lines.append(esc(desc))

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
    query_callback,
    token: str,
    plex_username: str | None,
) -> None:
    """
    Hent fuld detaljer og vis Netflix-look med plakat, cast og interaktive knapper.

    Knap-logik:
      - PÅ PLEX:   [▶️ Se på Plex]    (deep-link URL)
      - IKKE PLEX: [➕ Tilføj til Plex] (confirm:token)
      - ALTID:     [📌 Tilføj til Watchlist] (watchlist:token)
      - HVIS URL:  [🎬 Se Trailer]    (URL)
    """
    pending = await database.get_pending_request(token)
    if not pending:
        await query_callback.edit_message_text("Sessionen er udløbet — prøv igen.")
        return

    tmdb_id    = pending["tmdb_id"]
    media_type = pending["media_type"]

    details = await get_media_details(tmdb_id, media_type)
    if not details:
        await query_callback.edit_message_text("Kunne ikke hente detaljer — prøv igen.")
        return

    title       = details.get("title") or pending["title"]
    year        = details.get("release_date", details.get("first_air_date", ""))[:4]
    genres      = details.get("genres", [])
    orig_lang   = details.get("original_language", "en")
    tvdb_id     = details.get("tvdb_id")
    seasons     = details.get("season_numbers", [])
    trailer_url = details.get("trailer_url")
    poster_url  = details.get("poster_url")

    # Gem fuld data inkl. telegram_id til execute_order
    new_token = _make_token()
    await database.save_pending_request(new_token, query_callback.from_user.id, {
        "media_type":        media_type,
        "tmdb_id":           tmdb_id,
        "tvdb_id":           tvdb_id,
        "title":             title,
        "year":              int(year) if year else None,
        "genres":            genres,
        "original_language": orig_lang,
        "season_numbers":    seasons,
        "trailer_url":       trailer_url,
        "telegram_id":       query_callback.from_user.id,
    })

    # ── Tjek om titlen er på Plex ─────────────────────────────────────────────
    plex_check = await check_library(
        title, int(year) if year else None, media_type, plex_username
    )
    on_plex       = plex_check.get("status") == STATUS_FOUND
    rating_key    = plex_check.get("ratingKey")
    machine_id    = plex_check.get("machineIdentifier")

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

    # ── Send som foto eller tekst (fallback) ──────────────────────────────────
    try:
        await query_callback.message.delete()
    except Exception:
        pass  # Besked allerede slettet eller ingen adgang

    try:
        if poster_url:
            await query_callback.get_bot().send_photo(
                chat_id=query_callback.from_user.id,
                photo=poster_url,
                caption=caption,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        else:
            # Fallback: ingen plakat — send som tekst
            await query_callback.get_bot().send_message(
                chat_id=query_callback.from_user.id,
                text=caption,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
    except Exception as e:
        logger.error("show_confirmation send fejl: %s", e)
        # Absolut fallback uden formattering
        try:
            await query_callback.get_bot().send_message(
                chat_id=query_callback.from_user.id,
                text=f"{title} ({year})\n\n{details.get('overview', '')}",
                reply_markup=keyboard,
            )
        except Exception as e2:
            logger.error("show_confirmation absolut fallback fejl: %s", e2)


# ── /info_movie_<id> og /info_tv_<id> handler ────────────────────────────────

async def handle_info_command(
    update,
    media_type: str,
    tmdb_id: int,
    plex_username: str | None,
) -> None:
    """
    Håndterer /info_movie_<tmdb_id> og /info_tv_<tmdb_id> kommandoer.
    Opretter en pending token og kalder show_confirmation.
    """
    # Vi har ingen query_callback her — vi bruger en adapter
    details = await get_media_details(tmdb_id, media_type)
    if not details:
        await update.message.reply_text("Kunne ikke hente info — prøv igen.")
        return

    title = details.get("title") or "Ukendt"
    year  = details.get("release_date", details.get("first_air_date", ""))[:4]

    token = _make_token()
    await database.save_pending_request(token, update.effective_user.id, {
        "media_type": media_type,
        "tmdb_id":    tmdb_id,
        "title":      title,
        "year":       int(year) if year else None,
        "step":       "picked",
    })

    # Vi sender en midlertidig besked og bruger den som query_callback-adapter
    msg = await update.message.reply_text("⏳ Henter info…")

    class _MsgAdapter:
        """Minimal adapter så show_confirmation kan bruges med en Message."""
        def __init__(self, message, user):
            self.message   = message
            self.from_user = user
            self._bot      = message.get_bot()

        async def edit_message_text(self, text, **kwargs):
            await self.message.edit_text(text, **kwargs)

        async def get_bot(self):
            return self._bot

    adapter = _MsgAdapter(msg, update.effective_user)
    await show_confirmation(adapter, token, plex_username)


# ── Step 3: Udfør bestilling ──────────────────────────────────────────────────

async def execute_order(
    query_callback,
    token: str,
    plex_username: str | None,
) -> None:
    """
    Execute Radarr/Sonarr bestilling. Sender telegram_id som tag.
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