"""
services/confirmation_service.py - Bestillingsflow og Netflix-look infokort.

CHANGES vs previous version (v1.0.3 — infokort layout fix):
  - _build_caption(): Rettet tre layout-fejl der fik serier til at se
    anderledes ud end film:
    1. Genre-emoji: ændret fra 🎭 → 🎬 (matcher Design B / billede 1).
    2. Skillelinje: ændret fra "─" * 32 (U+2500 tynde) → "━" * 16 (U+2501
       tykke) — matcher den godkendte Design B.
    3. Sæson-info for serier: tilføjet "📺 X sæson(er)" på samme linje som
       rating, adskilt af " · " — matcher billede 2's forventede layout.
    Ingen andre ændringer i filen.

UNCHANGED (v0.9.8 — Annuller-knap på infokort):
  - show_confirmation(): Tilføjet ❌ Annuller-knap nederst i infokortets keyboard.

UNCHANGED (v0.9.7 — søgeresultater UX-fix):
  - show_search_results(): årstal-fallback, duplikat-skelnen, tilbage-knap.

UNCHANGED (v0.9.6 — execute_order foto-fix):
  - _edit_or_caption(), execute_order() — uændrede.
"""

import logging
import secrets

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

import database
from services.radarr_service import (
    add_movie,
    check_radarr_library,
)
from services.sonarr_service import (
    add_series,
    check_sonarr_library,
)
from services.tmdb_service import get_media_details, search_media
from services.plex_service import check_library

logger = logging.getLogger(__name__)

STATUS_FOUND = "found"


def _make_token() -> str:
    return secrets.token_hex(8)


def _build_caption(details: dict, rating=None) -> str:
    """
    Byg Netflix-look caption til infokort (Design B).

    Layout:
      *Titel* (År)

      🎬 Genre · Genre · Genre
      👥 Skuespiller, Skuespiller
      ⭐️ 9.1/10 · 🕐 152 min        ← film (rating + spilletid)
      ⭐️ 9.1/10 · 📺 2 sæsoner      ← serier (rating + sæsoner)
      ━━━━━━━━━━━━━━━━

      _Resumé i kursiv_

    Rating vises øverst — mellem cast og skillelinjen.
    Én enkelt ⭐️ (ikke stjernespam via round()).
    Spilletid kun for film (runtime_minutes fra TMDB).
    Serier viser antal sæsoner i stedet for spilletid.
    Bruger Plex IMDb-rating hvis tilgængeligt, ellers TMDB vote_average.
    parse_mode="Markdown" — ikke MarkdownV2.
    """
    title           = details.get("title") or "Ukendt"
    year            = (details.get("release_date") or details.get("first_air_date") or "")[:4]
    genres          = details.get("genres") or []
    _overview_raw   = details.get("overview") or "Ingen beskrivelse."
    overview        = _overview_raw[:800] + "2026" if len(_overview_raw) > 800 else _overview_raw
    cast            = details.get("cast") or []
    vote            = rating if rating is not None else details.get("vote_average")
    media_type      = details.get("media_type", "movie")
    runtime_minutes = details.get("runtime_minutes")
    num_seasons     = details.get("number_of_seasons")

    genre_str = " · ".join(g if isinstance(g, str) else g.get("name", "") for g in genres[:3])
    cast_str  = ", ".join(cast[:3]) if cast else ""

    lines = [f"*{title}* ({year})", ""]

    if genre_str:
        lines.append(f"🎬 {genre_str}")
    if cast_str:
        lines.append(f"👥 {cast_str}")

    # ── Rating + spilletid/sæsoner — øverst, før skillelinjen ────────────────
    meta_parts = []
    if vote:
        try:
            meta_parts.append(f"⭐️ {float(vote):.1f}/10")
        except (ValueError, TypeError):
            pass

    if media_type == "movie" and runtime_minutes:
        try:
            mins = int(runtime_minutes)
            if mins > 0:
                meta_parts.append(f"🕐 {mins} min")
        except (ValueError, TypeError):
            pass
    elif media_type == "tv" and num_seasons:
        try:
            n = int(num_seasons)
            season_label = "sæson" if n == 1 else "sæsoner"
            meta_parts.append(f"📺 {n} {season_label}")
        except (ValueError, TypeError):
            pass

    if meta_parts:
        lines.append(" · ".join(meta_parts))

    # ── Skillelinje + resumé ──────────────────────────────────────────────────
    lines += ["", "━" * 16, ""]
    lines.append(f"_{overview}_")

    return "\n".join(lines)


# ── Hjælpefunktion: rediger tekst ELLER caption ───────────────────────────────

async def _edit_or_caption(
    query_callback,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup=None,
) -> None:
    """
    Telegram skelner mellem tekst-beskeder og foto-beskeder:
      - Tekst-besked → edit_message_text()
      - Foto-besked  → edit_message_caption()

    Infokort sendes som sendPhoto() — derfor crasher edit_message_text()
    med "There is no text in the message to edit".

    Denne funktion detekterer besked-typen og bruger det rigtige kald.
    Falder altid tilbage til en ny send_message() hvis alt andet fejler.
    """
    msg = query_callback.message
    is_photo = bool(getattr(msg, "photo", None))

    kwargs = {"parse_mode": parse_mode}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup

    try:
        if is_photo:
            await query_callback.edit_message_caption(caption=text, **kwargs)
        else:
            await query_callback.edit_message_text(text=text, **kwargs)
    except Exception as e:
        logger.warning("_edit_or_caption fejlede (%s) — forsøger send_message fallback", e)
        try:
            await query_callback.message.reply_text(text, parse_mode=parse_mode)
        except Exception as e2:
            logger.error("_edit_or_caption absolut fallback fejlede: %s", e2)


# ── Step 1: Vis søgeresultater ────────────────────────────────────────────────

async def show_search_results(
    message,
    query: str,
    media_type: str,
) -> bool:
    """
    Søg TMDB og vis top-5 resultater som inline-knapper.
    Returnerer True hvis resultater blev fundet.
    """
    results = await search_media(query, media_type)
    if not results:
        await message.reply_text(f"Jeg kunne ikke finde noget for '{query}' 🤔")
        return False

    top = results[:5]
    buttons = []

    seen_labels: set[str] = set()

    for item in top:
        title   = item.get("title") or "Ukendt"
        year    = (item.get("release_date") or item.get("first_air_date") or "")[:4]
        mtype   = item.get("media_type", media_type if media_type != "both" else "movie")
        tmdb_id = item.get("id")

        year_str = year if year else "?"
        label    = f"{title} ({year_str})"

        if label in seen_labels:
            type_label = "Film" if mtype == "movie" else "Serie"
            label = f"{title} ({year_str}) · {type_label}"

        seen_labels.add(label)

        token = _make_token()
        await database.save_pending_request(token, message.chat_id, {
            "media_type": mtype,
            "tmdb_id":    tmdb_id,
            "title":      title,
            "year":       int(year) if year else None,
            "step":       "picked",
        })
        buttons.append([InlineKeyboardButton(label, callback_data=f"pick:{token}")])

    # Tilbage-knap: gemmer søgeterm og media_type til back:-handleren
    back_token = _make_token()
    await database.save_pending_request(back_token, message.chat_id, {
        "media_type": media_type,
        "tmdb_id":    0,
        "title":      query,
        "year":       None,
        "step":       "back",
    })

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
    loading_msg=None,
) -> None:
    """
    Hent fuld detaljer og vis Netflix-look infokort med plakat.

    Bruger parse_mode="Markdown" (ikke MarkdownV2) for at undgå
    crash ved specialtegn i filmtitler og beskrivelser.

    Knap-logik:
      - PÅ PLEX → [▶️ Se på Plex]
      - IKKE PÅ PLEX → [➕ Tilføj til Plex]
      - ALTID → [📌 Tilføj til Watchlist]
      - HVIS trailer → [🎬 Se Trailer]
      - ALTID → [❌ Annuller]
    """
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
    plex_check  = await check_library(
        title, int(year) if year else None, media_type, plex_username,
        tmdb_id=tmdb_id,
    )
    on_plex     = plex_check.get("status") == STATUS_FOUND
    machine_id  = plex_check.get("machineIdentifier", "")
    rating_key  = plex_check.get("ratingKey", "")
    plex_rating = plex_check.get("rating")

    logger.info(
        "Rating — Plex: %s, TMDB: %s, bruger: %s",
        plex_rating,
        details.get("vote_average"),
        "Plex" if plex_rating is not None else "TMDB (fallback)",
    )

    # ── Byg knap-rækker ───────────────────────────────────────────────────────
    button_rows = []

    if on_plex:
        from services.plex_service import get_plex_watch_url
        watch_url = await get_plex_watch_url(tmdb_id, media_type)
        if watch_url:
            plex_url = watch_url
        else:
            from urllib.parse import quote
            plex_url = f"https://watch.plex.tv/search?q={quote(title)}"
        logger.info("Plex URL — titel=%r url=%s", title, plex_url)
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

    button_rows.append([InlineKeyboardButton("❌ Annuller", callback_data="cancel:none")])

    keyboard = InlineKeyboardMarkup(button_rows)
    caption  = _build_caption(details, rating=plex_rating)

    # ── Slet trigger-besked KUN ved CallbackQuery ─────────────────────────────
    if isinstance(trigger, CallbackQuery):
        try:
            await trigger.message.delete()
        except Exception:
            pass

    # ── Send infokort ──────────────────────────────────────────────────────────
    if loading_msg:
        try:
            await loading_msg.delete()
        except Exception:
            pass

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
    """
    Execute Radarr/Sonarr bestilling.

    Flow:
      1. Hent pending request fra DB.
      2. Tjek om filmen/serien allerede er i Radarr/Sonarr (monitored_only).
      3. Send til Radarr/Sonarr.
      4. Opdater beskeden med result via _edit_or_caption().
    """
    pending = await database.get_pending_request(token)
    if not pending:
        await _edit_or_caption(query_callback, "Sessionen er udløbet — prøv igen.")
        return

    media_type  = pending["media_type"]
    title       = pending["title"]
    tmdb_id     = pending.get("tmdb_id")
    tvdb_id     = pending.get("tvdb_id")
    telegram_id = pending.get("telegram_id") or query_callback.from_user.id

    # ── Trin 1: Tjek om allerede i Radarr/Sonarr ──────────────────────────────
    try:
        if media_type == "movie" and tmdb_id:
            existing = await check_radarr_library(tmdb_id)
            if existing.get("status") in ("found", "monitored_only"):
                status_txt = (
                    "er allerede på Plex! 🎬"
                    if existing.get("status") == "found"
                    else "er allerede anmodet og søges nu — du får besked når den er klar! 🍿"
                )
                await _edit_or_caption(
                    query_callback,
                    f"*{title}* {status_txt}",
                    parse_mode="Markdown",
                )
                return
        elif media_type == "tv" and tvdb_id:
            existing = await check_sonarr_library(tvdb_id)
            if existing.get("status") in ("found", "monitored_only"):
                status_txt = (
                    "er allerede på Plex! 📺"
                    if existing.get("status") == "found"
                    else "er allerede anmodet og søges nu — du får besked når den er klar! 🍿"
                )
                await _edit_or_caption(
                    query_callback,
                    f"*{title}* {status_txt}",
                    parse_mode="Markdown",
                )
                return
    except Exception as e:
        logger.warning("Pre-check mod Radarr/Sonarr fejlede for '%s': %s", title, e)

    # ── Trin 2: Vis loading-besked ─────────────────────────────────────────────
    await _edit_or_caption(
        query_callback,
        f"⏳ Bestiller *{title}* — vent et øjeblik…",
        parse_mode="Markdown",
    )

    # ── Trin 3: Send til Radarr/Sonarr ────────────────────────────────────────
    if media_type == "movie":
        result = await add_movie(
            tmdb_id=tmdb_id,
            title=title,
            year=pending["year"] or 0,
            genres=pending["genres"],
            telegram_id=telegram_id,
        )
    else:
        result = await add_series(
            tvdb_id=tvdb_id,
            title=title,
            year=pending["year"] or 0,
            original_language=pending["original_language"],
            season_numbers=pending["season_numbers"],
            telegram_id=telegram_id,
        )

    # ── Trin 4: Vis resultat ───────────────────────────────────────────────────
    if result.get("success"):
        await _edit_or_caption(
            query_callback,
            f"✅ *{title}* er bestilt og søges nu!\nDu får besked når den er klar på Plex 🍿",
            parse_mode="Markdown",
        )
    else:
        status = result.get("status", "")
        if status == "already_exists":
            msg = f"*{title}* er allerede anmodet — du får besked når den er klar! 🍿"
        else:
            msg = f"Noget gik galt med bestillingen af *{title}*. Prøv igen lidt senere."
        await _edit_or_caption(query_callback, msg, parse_mode="Markdown")