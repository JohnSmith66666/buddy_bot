"""
services/confirmation_service.py - Inline keyboard bekræftelsesflow.

Håndterer:
1. Præsentation af søgeresultater som Inline Buttons
2. Visning af medie-detaljer med Bekræft/Annuller knapper
3. Selve bestillingen til Radarr eller Sonarr ved bekræftelse

Callback data format (max 64 bytes):
  "pick:<token>"     — bruger valgte et søgeresultat
  "confirm:<token>"  — bruger bekræftede bestilling
  "cancel:<token>"   — bruger annullerede
"""

import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

import database
from services.radarr_service import add_movie
from services.sonarr_service import add_series
from services.tmdb_service import get_media_details, search_media

logger = logging.getLogger(__name__)

_TMDB_POSTER = "https://image.tmdb.org/t/p/w500"


def _make_token() -> str:
    """Generate a short unique token for callback_data (fits in 64 bytes)."""
    return secrets.token_hex(8)  # 16 chars — plenty of room


# ── Step 1: Vis søgeresultater som knapper ────────────────────────────────────

async def show_search_results(
    message: Message,
    query: str,
    media_type: str = "both",
) -> bool:
    """
    Search TMDB and present top results as inline buttons.
    Returns True if results were found, False otherwise.
    """
    results = await search_media(query, media_type)

    if not results:
        await message.reply_text(f"Jeg kunne ikke finde noget for '{query}' 🤔")
        return False

    # Begræns til 5 resultater og lav knapper
    top = results[:5]
    buttons = []
    for item in top:
        title = item.get("title") or "Ukendt"
        year  = (item.get("release_date") or item.get("first_air_date") or "")[:4]
        mtype = item.get("media_type", media_type if media_type != "both" else "movie")
        tmdb_id = item.get("id")

        label = f"{title} ({year})" if year else title

        # Gem en lille token i callback_data: "pick:<token>"
        token = _make_token()
        await database.save_pending_request(token, message.chat_id, {
            "media_type": mtype,
            "tmdb_id": tmdb_id,
            "title": title,
            "year": int(year) if year else None,
            "step": "picked",
        })

        buttons.append([InlineKeyboardButton(label, callback_data=f"pick:{token}")])

    buttons.append([InlineKeyboardButton("❌ Annuller", callback_data="cancel:none")])

    await message.reply_text(
        f"Jeg fandt disse resultater for *{query}* — hvilken mener du?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return True


# ── Step 2: Vis detaljer + Bekræft/Annuller ───────────────────────────────────

async def show_confirmation(
    query_callback,  # telegram CallbackQuery object
    token: str,
    plex_username: str | None,
) -> None:
    """
    Fetch full details for the picked title and show confirm/cancel buttons.
    """
    # Hent den gemte partial data
    pending = await database.get_pending_request(token)
    if not pending:
        await query_callback.edit_message_text("Sessionen er udløbet — prøv igen.")
        return

    tmdb_id    = pending["tmdb_id"]
    media_type = pending["media_type"]

    # Hent fuld info fra TMDB
    details = await get_media_details(tmdb_id, media_type)
    if not details:
        await query_callback.edit_message_text("Kunne ikke hente detaljer — prøv igen.")
        return

    title    = details.get("title") or pending["title"]
    year     = details.get("release_date", details.get("first_air_date", ""))[:4]
    overview = details.get("overview") or "Ingen beskrivelse."
    genres   = details.get("genres", [])
    orig_lang = details.get("original_language", "en")
    tvdb_id  = details.get("tvdb_id")
    seasons  = details.get("season_numbers", [])

    # Gem fuld data til bekræftelse
    new_token = _make_token()
    await database.save_pending_request(new_token, query_callback.from_user.id, {
        "media_type":         media_type,
        "tmdb_id":            tmdb_id,
        "tvdb_id":            tvdb_id,
        "title":              title,
        "year":               int(year) if year else None,
        "genres":             genres,
        "original_language":  orig_lang,
        "season_numbers":     seasons,
    })

    # Byg beskeden
    genre_str = ", ".join(genres[:3]) if genres else ""
    text = f"*{title}*"
    if year:
        text += f" ({year})"
    if genre_str:
        text += f"\n_{genre_str}_"
    text += f"\n\n{overview[:300]}"
    if len(overview) > 300:
        text += "…"

    if media_type == "tv" and seasons:
        text += f"\n\n📺 {len(seasons)} sæson{'er' if len(seasons) != 1 else ''}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Bestil", callback_data=f"confirm:{new_token}"),
            InlineKeyboardButton("❌ Annuller", callback_data=f"cancel:{new_token}"),
        ]
    ])

    await query_callback.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ── Step 3: Udfør bestilling ──────────────────────────────────────────────────

async def execute_order(
    query_callback,
    token: str,
    plex_username: str | None,
) -> None:
    """
    Execute the actual Radarr or Sonarr request upon user confirmation.
    """
    pending = await database.get_pending_request(token)
    if not pending:
        await query_callback.edit_message_text("Sessionen er udløbet — prøv igen.")
        return

    media_type = pending["media_type"]
    title      = pending["title"]

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
            plex_username=plex_username,
        )
    else:
        result = await add_series(
            tvdb_id=pending["tvdb_id"],
            title=title,
            year=pending["year"] or 0,
            original_language=pending["original_language"],
            season_numbers=pending["season_numbers"],
            plex_username=plex_username,
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