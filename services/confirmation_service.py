"""
services/confirmation_service.py - Inline keyboard bekræftelsesflow.

CHANGES vs previous version:
  - execute_order() sender nu query_callback.from_user.id (telegram_id) til
    add_movie() og add_series() i stedet for plex_username. Dette sikrer at
    tagget "tg_<telegram_id>" oprettes i Radarr/Sonarr, så webhook_service
    kan sende notifikationer præcist til den bruger der bestilte.
  - pending-data gemmer nu telegram_id eksplicit ved show_confirmation().
  - Alle øvrige handlers (show_search_results, show_confirmation) er uændrede.

Håndterer:
1. Præsentation af søgeresultater som Inline Buttons
2. Visning af medie-detaljer med Trailer/Bestil/Annuller knapper
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
    return secrets.token_hex(8)


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

    top = results[:5]
    buttons = []
    for item in top:
        title   = item.get("title") or "Ukendt"
        year    = (item.get("release_date") or item.get("first_air_date") or "")[:4]
        mtype   = item.get("media_type", media_type if media_type != "both" else "movie")
        tmdb_id = item.get("id")

        label = f"{title} ({year})" if year else title

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


# ── Step 2: Vis detaljer + Trailer / Bestil / Annuller ────────────────────────

async def show_confirmation(
    query_callback,
    token: str,
    plex_username: str | None,
) -> None:
    """
    Fetch full details for the picked title and show action buttons.

    Keyboard-rækkefølge:
      [🎬 Se Trailer]          ← kun hvis trailer_url er tilgængelig
      [✅ Bestil]  [❌ Annuller]
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
    overview    = details.get("overview") or "Ingen beskrivelse."
    genres      = details.get("genres", [])
    orig_lang   = details.get("original_language", "en")
    tvdb_id     = details.get("tvdb_id")
    seasons     = details.get("season_numbers", [])
    trailer_url = details.get("trailer_url")

    # Gem fuld data inkl. telegram_id til brug i execute_order
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
        "telegram_id":       query_callback.from_user.id,  # ← til tag i Radarr/Sonarr
    })

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

    button_rows = []
    if trailer_url:
        button_rows.append([InlineKeyboardButton("🎬 Se Trailer", url=trailer_url)])

    button_rows.append([
        InlineKeyboardButton("✅ Bestil",   callback_data=f"confirm:{new_token}"),
        InlineKeyboardButton("❌ Annuller", callback_data=f"cancel:{new_token}"),
    ])

    await query_callback.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(button_rows),
    )


# ── Step 3: Udfør bestilling ──────────────────────────────────────────────────

async def execute_order(
    query_callback,
    token: str,
    plex_username: str | None,
) -> None:
    """
    Execute the actual Radarr or Sonarr request upon user confirmation.

    Sender telegram_id til add_movie/add_series så tagget "tg_<id>"
    oprettes i Radarr/Sonarr. webhook_service bruger dette tag til at
    sende notifikationen præcist til den bruger der bestilte.
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