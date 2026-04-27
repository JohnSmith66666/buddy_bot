"""
main.py - Buddy bot entry point.

CHANGES vs previous version (v0.11.0 — Etape 3 af subgenre-projekt):
  - 🎉 STOR REFAKTORERING: Hele 'Hvad skal jeg se?' flow erstattet.
    Det gamle AI-prompt-baserede flow med 17 stemninger er fjernet.
    Det nye flow er datadrevet via subgenre_service + find_unwatched_v2.

  - NYT 4-TRINS UI:
    * Trin 1: Bundknap '🍿 Hvad skal jeg se?' (uændret reply keyboard)
    * Trin 2: 9 kategori-knapper (1 pr. række) + Overrask + Afbryd
    * Trin 3: 3-6 subgenre-knapper (1 pr. række) + Overrask + Tilbage + Afbryd
    * Trin 4: 5 film-resultater (titel + ⭐ rating + /info_movie_X) +
              4 actions (🔄 nye / ⏭️ næste subgenre / ⬅️ tilbage / ❌ færdig)

  - KALDER find_unwatched_v2 DIREKTE — ingen AI-prompt construction.
    Hurtigere (~2 sek vs 8-10 sek), billigere ($0 vs $0.01 per kald),
    præcisere (datadrevet keyword-matching i stedet for genre-tag).

  - SLETTET (~250 linjer død kode):
    * WATCH_MOODS dictionary (17 stemninger med dansk/engelsk genre-mapping)
    * WATCH_PAGES paginering (3 sider × 5-6 stemninger)
    * _build_type_selection_keyboard, _build_mood_keyboard, _build_mood_message_text
    * _build_watch_prompt (AI-prompt construction)
    * handle_watch_type_callback, handle_watch_mood_callback, handle_watch_page_callback
    * handle_watch_search_callback, handle_watch_surprise_callback (gammel version)
    * _execute_ai_handoff (kæmpe AI-flow)

  - BEVARET (al ikke-watch-flow funktionalitet):
    * Bundknap-tekst '🍿 Hvad skal jeg se?' (brugervante)
    * Alle admin-kommandoer (/test_v2, /seed_metadata, /fetch_metadata, etc.)
    * Bestillingsflow (pick / confirm / cancel / back / watchlist)
    * Info-links (/info_movie_X, /info_tv_X)
    * AI-handler for fri-tekst (Claude med tools)
    * Webhook-server (Radarr/Sonarr)
    * Onboarding (Plex-username verification)

  - VERSION CHECK opdateret til v0.11.0-beta.

UNCHANGED (v0.10.8 — /test_v2 admin DEBUG).
UNCHANGED (v0.10.7 — fuld keyword-eksport).
UNCHANGED (v0.10.6 — Step 2 af subgenre-projekt).
UNCHANGED (v0.10.5 — /test_metadata kommando).
UNCHANGED (v0.10.4 — /test_enrich DRY-RUN).
UNCHANGED (v0.10.3 — /dump_genres admin-kommando).
UNCHANGED (v0.10.2 — /genres admin-kommando).
"""

import asyncio
import csv
import io
import json
import logging
import os
import random
import re
import sys
import tempfile
import traceback
from aiohttp import web
from collections import Counter
from datetime import datetime
from itertools import combinations

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database
from admin_handlers import handle_approve_callback, notify_admin_new_user
from ai_handler import INFO_SIGNAL, SEARCH_SIGNAL, TRAILER_SIGNAL, check_session_timeout, clear_history, get_ai_response
from personas import get_persona
from services.confirmation_service import (
    execute_order,
    handle_watchlist_callback,
    show_confirmation,
    show_search_results,
)
from services.plex_service import validate_plex_user
from services.tmdb_service import get_media_details
from services.tmdb_keywords_service import (
    fetch_metadata_batch,
    fetch_movie_metadata,
    fetch_tv_metadata,
    search_tmdb_by_title,
)
from services.subgenre_service import (
    SUBGENRE_CATEGORIES,
    SUBGENRES,
    get_all_categories,
    get_category,
    get_category_for_subgenre,
    get_subgenre,
    list_subgenre_ids,
    validate_subgenre_id,
)
from services.v2_service import find_unwatched_v2
from services.webhook_service import handle_radarr_webhook, handle_sonarr_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"(https?://[^\s)\]>\"]+)")


# ══════════════════════════════════════════════════════════════════════════════
# WATCH FLOW (NY v0.11.0) — Konstanter og helpers
# ══════════════════════════════════════════════════════════════════════════════

# Bundknap-tekst (uændret for at bevare brugervante)
WATCH_FLOW_TRIGGER = "🍿 Hvad skal jeg se?"

# Header-tekster i de forskellige trin
TRIN2_HEADER = "🍿 *Find en film*\n\nVælg en stemning 👇"
TRIN3_HEADER_TPL = "{label}\n\nVælg en undergenre 👇"


def _build_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bundknap — vises i bunden af chatten."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(WATCH_FLOW_TRIGGER)]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Watch Flow keyboards — bygges fra subgenre_service data
# ══════════════════════════════════════════════════════════════════════════════

def _build_categories_keyboard() -> InlineKeyboardMarkup:
    """
    Trin 2: 9 hovedkategorier (1 knap pr. række) + Overrask + Afbryd.
    Callback-data: 'sg_cat:<category_id>' eller 'sg_cat:random'
    """
    rows: list[list[InlineKeyboardButton]] = []

    for cat in get_all_categories():
        rows.append([
            InlineKeyboardButton(cat["label"], callback_data=f"sg_cat:{cat['id']}")
        ])

    # Bundrække: Overrask + Afbryd
    rows.append([
        InlineKeyboardButton("🎲 Overrask mig", callback_data="sg_cat:random"),
    ])
    rows.append([
        InlineKeyboardButton("❌ Afbryd", callback_data="sg_cancel"),
    ])

    return InlineKeyboardMarkup(rows)


def _build_subgenres_keyboard(category_id: str) -> InlineKeyboardMarkup:
    """
    Trin 3: 3-6 subgenrer i denne kategori (1 knap pr. række)
    + Overrask (random subgenre i samme kasse) + Tilbage + Afbryd.
    """
    cat = get_category(category_id)
    rows: list[list[InlineKeyboardButton]] = []

    if cat:
        for sub in cat["subgenres"]:
            rows.append([
                InlineKeyboardButton(sub["label"], callback_data=f"sg_pick:{sub['id']}")
            ])

    # Bundrække
    rows.append([
        InlineKeyboardButton(
            "🎲 Overrask mig (i denne kategori)",
            callback_data=f"sg_random:{category_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Tilbage", callback_data="sg_back:cats"),
        InlineKeyboardButton("❌ Afbryd",   callback_data="sg_cancel"),
    ])

    return InlineKeyboardMarkup(rows)


def _build_results_keyboard(subgenre_id: str, has_results: bool) -> InlineKeyboardMarkup:
    """
    Trin 4: Actions efter resultater.

    Hvis der er resultater:
      🔄 5 nye forslag / ⏭️ Næste subgenre / ⬅️ Tilbage / ❌ Færdig

    Hvis der INGEN resultater er (alle set):
      ⏭️ Næste subgenre / ⬅️ Tilbage / ❌ Færdig
    """
    cat_id = get_category_for_subgenre(subgenre_id) or "comedy"
    rows: list[list[InlineKeyboardButton]] = []

    if has_results:
        rows.append([
            InlineKeyboardButton(
                "🔄 5 nye forslag",
                callback_data=f"sg_refresh:{subgenre_id}",
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            "⏭️ Næste subgenre",
            callback_data=f"sg_next:{cat_id}",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ Tilbage til kategorier",
            callback_data="sg_back:cats",
        ),
    ])
    rows.append([
        InlineKeyboardButton("❌ Færdig", callback_data="sg_cancel"),
    ])

    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Result formatting — fra find_unwatched_v2 dict til Markdown-besked
# ══════════════════════════════════════════════════════════════════════════════

def _format_results_message(result: dict) -> tuple[str, bool]:
    """
    Format find_unwatched_v2 resultat som Markdown-besked.

    Returns:
      (message_text, has_results)

    Edge cases:
      - status='error': fejl-besked
      - status='missing' med stats.unwatched=0: 'godt gået'-besked
      - status='ok' med <5 forslag: 'kun X forslag'-advarsel
      - status='ok' med 5 forslag: normal liste
    """
    status         = result.get("status")
    subgenre_label = result.get("subgenre_label", "?")
    stats          = result.get("stats", {}) or {}
    unwatched      = stats.get("unwatched", 0)

    # ── Error case ─────────────────────────────────────────────────────────────
    if status == "error":
        return (
            f"❌ Hov, noget gik galt!\n\n"
            f"`{result.get('message', 'Ukendt fejl')}`\n\n"
            f"Prøv en anden subgenre.",
            False,
        )

    # ── Missing case (alle set ELLER ingen kandidater) ─────────────────────────
    if status == "missing":
        if stats.get("in_plex", 0) == 0:
            # Ingen film i Plex matcher — det er sjældent men mulige edge case
            return (
                f"😕 Hmm, jeg kunne ikke finde nogen film der matcher "
                f"*{subgenre_label}* i dit bibliotek.\n\n"
                f"_Prøv en anden subgenre._",
                False,
            )
        # Alle film i denne subgenre er allerede set
        return (
            f"🎉 *Du har set ALT i {subgenre_label} — godt gået!*\n\n"
            f"_Prøv en anden subgenre._",
            False,
        )

    # ── Success case ───────────────────────────────────────────────────────────
    results = result.get("results", []) or []
    if not results:
        return (
            f"🎉 *Du har set ALT i {subgenre_label} — godt gået!*\n\n"
            f"_Prøv en anden subgenre._",
            False,
        )

    lines = [f"*{subgenre_label}*", ""]

    # Advarsel hvis færre end 5 forslag
    if len(results) < 5:
        lines.append(
            f"⚠️ _Du har set det meste — kun {len(results)} forslag her_"
        )
        lines.append("")

    for film in results:
        title   = film.get("title") or "Ukendt"
        year    = film.get("year")
        rating  = film.get("rating")
        tmdb_id = film.get("tmdb_id")

        year_str   = f" ({year})" if year else ""
        rating_str = f" ⭐ {rating:.1f}" if rating else ""
        info_link  = f"\n   /info_movie_{tmdb_id}" if tmdb_id else ""

        lines.append(f"🟢 *{title}*{year_str}{rating_str}{info_link}")
        lines.append("")  # tom linje mellem film for læsbarhed

    # Fjern sidste tomme linje før footer
    if lines and lines[-1] == "":
        lines.pop()

    # Diskret stats nederst
    lines.append("")
    lines.append(f"_{unwatched} usete i denne kategori_")

    return ("\n".join(lines), True)


# ══════════════════════════════════════════════════════════════════════════════
# Watch Flow trigger + handlers (NY v0.11.0)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_watch_flow_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Trin 1 → Trin 2: Bruger trykkede '🍿 Hvad skal jeg se?'
    Vis kategori-keyboardet.
    """
    if not await _guard(update):
        return

    user = update.effective_user
    await database.log_message(user.id, "incoming", WATCH_FLOW_TRIGGER)

    if await _needs_plex_setup(update):
        return

    await update.message.reply_text(
        TRIN2_HEADER,
        parse_mode="Markdown",
        reply_markup=_build_categories_keyboard(),
    )
    logger.info("Watch flow startet for telegram_id=%s", user.id)


async def handle_subgenre_category_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Trin 2 → Trin 3: Bruger valgte en kategori.
    Vis subgenre-keyboardet for den valgte kategori.

    Specialcase: 'sg_cat:random' → vælg random kategori og vis dens subgenrer.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    payload = query.data.split(":", 1)[1]

    # 🎲 Overrask mig på kategori-niveau
    if payload == "random":
        category_id = random.choice(list(SUBGENRE_CATEGORIES.keys()))
        logger.info("Watch flow: random kategori valgt → '%s'", category_id)
    else:
        category_id = payload

    cat = get_category(category_id)
    if cat is None:
        logger.warning("handle_subgenre_category_callback: ukendt category_id='%s'", category_id)
        await query.answer("Den kategori findes ikke", show_alert=True)
        return

    try:
        await query.edit_message_text(
            text=TRIN3_HEADER_TPL.format(label=f"*{cat['label']}*"),
            parse_mode="Markdown",
            reply_markup=_build_subgenres_keyboard(category_id),
        )
    except Exception as e:
        logger.warning("handle_subgenre_category_callback edit fejl: %s", e)


async def handle_subgenre_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Trin 3 → Trin 4: Bruger valgte en specifik subgenre.
    Kald find_unwatched_v2 og vis 5 forslag.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    subgenre_id = query.data.split(":", 1)[1]

    if not validate_subgenre_id(subgenre_id):
        logger.warning("handle_subgenre_pick_callback: ukendt subgenre_id='%s'", subgenre_id)
        await query.answer("Den subgenre findes ikke", show_alert=True)
        return

    await _execute_subgenre_search(update, context, subgenre_id, edit_message=True)


async def handle_subgenre_random_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Trin 3 special: Bruger trykkede '🎲 Overrask mig (i denne kategori)'.
    Vælg random subgenre i kategorien og vis forslag.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    category_id = query.data.split(":", 1)[1]
    cat = get_category(category_id)
    if cat is None or not cat["subgenres"]:
        await query.answer("Den kategori er tom", show_alert=True)
        return

    subgenre_id = random.choice([s["id"] for s in cat["subgenres"]])
    logger.info(
        "Watch flow: random subgenre i kategori '%s' → '%s'",
        category_id, subgenre_id,
    )

    await _execute_subgenre_search(update, context, subgenre_id, edit_message=True)


async def handle_subgenre_refresh_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Trin 4 action: '🔄 5 nye forslag' — kald find_unwatched_v2 igen
    for samme subgenre. Smart-blanding sikrer nye film hver gang.
    """
    query = update.callback_query
    await query.answer("🔄 Henter nye forslag...")

    if not await _guard(update):
        return

    subgenre_id = query.data.split(":", 1)[1]

    if not validate_subgenre_id(subgenre_id):
        await query.answer("Den subgenre findes ikke", show_alert=True)
        return

    await _execute_subgenre_search(update, context, subgenre_id, edit_message=True)


async def handle_subgenre_next_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Trin 4 action: '⏭️ Næste subgenre' — vælg en ANDEN random subgenre
    i samme kategori. Vis kategori-keyboardet hvis kun én subgenre.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    category_id = query.data.split(":", 1)[1]
    cat = get_category(category_id)

    if cat is None or len(cat["subgenres"]) < 2:
        # Kun én subgenre i kategorien — gå tilbage til kategorier
        await query.edit_message_text(
            text=TRIN2_HEADER,
            parse_mode="Markdown",
            reply_markup=_build_categories_keyboard(),
        )
        return

    # Vi vil gerne vise kategorien igen så brugeren ser alle subgenrerne
    # — det giver mere kontrol end at hoppe direkte til en random.
    try:
        await query.edit_message_text(
            text=TRIN3_HEADER_TPL.format(label=f"*{cat['label']}*"),
            parse_mode="Markdown",
            reply_markup=_build_subgenres_keyboard(category_id),
        )
    except Exception as e:
        logger.warning("handle_subgenre_next_callback edit fejl: %s", e)


async def handle_subgenre_back_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Trin 3/4 action: '⬅️ Tilbage til kategorier' — vis kategori-keyboard.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    try:
        await query.edit_message_text(
            text=TRIN2_HEADER,
            parse_mode="Markdown",
            reply_markup=_build_categories_keyboard(),
        )
    except Exception as e:
        logger.warning("handle_subgenre_back_callback edit fejl: %s", e)


async def handle_subgenre_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    '❌ Afbryd' eller '❌ Færdig' — luk flowet.
    """
    query = update.callback_query
    await query.answer()

    try:
        await query.edit_message_text(
            "Tryk på 🍿-knappen igen når du vil have hjælp. 👍"
        )
    except Exception as e:
        logger.warning("handle_subgenre_cancel_callback edit fejl: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# Watch Flow shared logic — kald find_unwatched_v2 og render
# ══════════════════════════════════════════════════════════════════════════════

async def _execute_subgenre_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    subgenre_id: str,
    edit_message: bool = True,
) -> None:
    """
    Fælles logik for alle veje der ender i et v2-kald:
      - handle_subgenre_pick_callback
      - handle_subgenre_random_callback
      - handle_subgenre_refresh_callback

    Kalder find_unwatched_v2, formaterer resultatet og opdaterer beskeden.
    Viser loading-tekst undervejs.
    """
    query = update.callback_query
    user  = query.from_user
    chat  = query.message.chat

    # Loading-state — opdater beskeden med "henter..."
    sub = get_subgenre(subgenre_id)
    sub_label = sub["label"] if sub else subgenre_id

    try:
        await query.edit_message_text(
            f"{sub_label}\n\n🤖 Beregner svar med lynets hast... næsten...",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    # Hent plex_username
    plex_username = await database.get_plex_username(user.id)

    # Kald v2-funktionen
    try:
        result = await find_unwatched_v2(
            subgenre_id=subgenre_id,
            plex_username=plex_username,
            limit=5,
        )
    except Exception as e:
        logger.error("_execute_subgenre_search fejl for '%s': %s", subgenre_id, e)
        try:
            await query.edit_message_text(
                f"❌ Hov, noget gik galt: `{e}`\n\nPrøv en anden subgenre.",
                parse_mode="Markdown",
                reply_markup=_build_results_keyboard(subgenre_id, has_results=False),
            )
        except Exception:
            pass
        return

    # Format resultat
    message_text, has_results = _format_results_message(result)
    keyboard = _build_results_keyboard(subgenre_id, has_results=has_results)

    # Log brugeraktivitet
    await database.log_message(
        user.id,
        "incoming",
        f"[watch_flow] subgenre={subgenre_id}",
    )
    stats = result.get("stats", {}) or {}
    await database.log_message(
        user.id,
        "outgoing",
        f"[watch_flow] {subgenre_id} → "
        f"{stats.get('returned', 0)} forslag (af {stats.get('unwatched', 0)} usete)",
    )

    # Opdater beskeden med resultater
    try:
        # Markdown kan fejle ved specielle tegn — escape underscores i URL'er
        safe_text = escape_markdown(message_text)
        await query.edit_message_text(
            text=safe_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning("_execute_subgenre_search edit fejl: %s — sender plain", e)
        try:
            plain = message_text.replace("*", "").replace("_", "").replace("`", "")
            await query.edit_message_text(
                text=plain,
                reply_markup=keyboard,
            )
        except Exception as e2:
            logger.error("_execute_subgenre_search fallback fejl: %s", e2)


# ══════════════════════════════════════════════════════════════════════════════
# /genres + /dump_genres — Engangs admin-kommandoer (uændret)
# ══════════════════════════════════════════════════════════════════════════════

def _scan_plex_genres_sync() -> dict:
    from services.plex_service import _connect, _sections, _MOVIE_TYPE, _TV_TYPE

    plex = _connect(None)
    if isinstance(plex, dict):
        return {"error": plex.get("message", "Plex-forbindelse fejlede")}

    movie_genres: Counter = Counter()
    tv_genres:    Counter = Counter()
    movie_pairs:  Counter = Counter()
    tv_pairs:     Counter = Counter()
    movie_total = 0
    tv_total    = 0

    for section in _sections(plex, _MOVIE_TYPE):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning("genres scan: section.all() fejl '%s': %s", section.title, e)
            continue
        for item in all_items:
            movie_total += 1
            tags = sorted({g.tag for g in getattr(item, "genres", []) if g.tag})
            for tag in tags:
                movie_genres[tag] += 1
            for pair in combinations(tags, 2):
                movie_pairs[pair] += 1

    for section in _sections(plex, _TV_TYPE):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning("genres scan: section.all() fejl '%s': %s", section.title, e)
            continue
        for item in all_items:
            tv_total += 1
            tags = sorted({g.tag for g in getattr(item, "genres", []) if g.tag})
            for tag in tags:
                tv_genres[tag] += 1
            for pair in combinations(tags, 2):
                tv_pairs[pair] += 1

    return {
        "movie_total":  movie_total,
        "tv_total":     tv_total,
        "movie_genres": movie_genres.most_common(),
        "tv_genres":    tv_genres.most_common(),
        "movie_pairs":  movie_pairs.most_common(20),
        "tv_pairs":     tv_pairs.most_common(20),
    }


def _format_genres_report(data: dict) -> list[str]:
    if data.get("error"):
        return [f"❌ Fejl ved Plex-scan: {data['error']}"]

    messages: list[str] = []
    header = (
        "📊 *PLEX GENRE-RAPPORT*\n"
        "═══════════════════════\n\n"
        f"🎬 Film-bibliotek: *{data['movie_total']}* titler\n"
        f"📺 TV-bibliotek: *{data['tv_total']}* serier\n"
    )

    movie_lines = ["🎬 *FILM-GENRER*", "─────────────"]
    for tag, count in data["movie_genres"]:
        movie_lines.append(f"`{count:>5}` × {tag}")

    tv_lines = ["📺 *TV-GENRER*", "─────────────"]
    for tag, count in data["tv_genres"]:
        tv_lines.append(f"`{count:>5}` × {tag}")

    movie_pair_lines = ["🔗 *TOP 20 FILM-KOMBINATIONER*", "─────────────────────────"]
    for (g1, g2), count in data["movie_pairs"]:
        movie_pair_lines.append(f"`{count:>5}` × {g1} + {g2}")

    tv_pair_lines = ["🔗 *TOP 20 TV-KOMBINATIONER*", "──────────────────────────"]
    for (g1, g2), count in data["tv_pairs"]:
        tv_pair_lines.append(f"`{count:>5}` × {g1} + {g2}")

    messages.append(header)
    messages.append("\n".join(movie_lines))
    messages.append("\n".join(tv_lines))
    messages.append("\n".join(movie_pair_lines))
    messages.append("\n".join(tv_pair_lines))

    return messages


async def cmd_genres(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    await update.message.chat.send_action("typing")
    loading_msg = await update.message.reply_text(
        "🔍 Scanner Plex-bibliotek for genre-tags...\nDette kan tage 10-30 sekunder."
    )

    try:
        data = await asyncio.to_thread(_scan_plex_genres_sync)
        messages = _format_genres_report(data)
    except Exception as e:
        logger.error("cmd_genres fejl: %s", e)
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(f"❌ Scan fejlede: {e}")
        return

    try:
        await loading_msg.delete()
    except Exception:
        pass

    for msg_text in messages:
        try:
            await update.message.reply_text(msg_text, parse_mode="Markdown")
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning("cmd_genres send fejl: %s", e)
            try:
                await update.message.reply_text(msg_text)
            except Exception:
                pass

    logger.info("Genre-rapport sendt til admin telegram_id=%s", user.id)


def _dump_plex_data_sync() -> dict:
    from services.plex_service import (
        _connect, _sections, _MOVIE_TYPE, _TV_TYPE,
        _extract_tmdb_id_from_guids,
    )

    plex = _connect(None)
    if isinstance(plex, dict):
        return {"error": plex.get("message", "Plex-forbindelse fejlede")}

    movies: list[dict] = []
    tv:     list[dict] = []

    for section in _sections(plex, _MOVIE_TYPE):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning("dump: section.all() fejl '%s': %s", section.title, e)
            continue

        for item in all_items:
            try:
                genres = sorted({g.tag for g in getattr(item, "genres", []) if g.tag})
                movies.append({
                    "title":   getattr(item, "title", "Ukendt") or "Ukendt",
                    "year":    getattr(item, "year", None),
                    "tmdb_id": _extract_tmdb_id_from_guids(item),
                    "rating":  getattr(item, "audienceRating", None) or getattr(item, "rating", None),
                    "genres":  genres,
                })
            except Exception as e:
                logger.warning("dump: item parse-fejl: %s", e)

    for section in _sections(plex, _TV_TYPE):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning("dump: section.all() fejl '%s': %s", section.title, e)
            continue

        for item in all_items:
            try:
                genres = sorted({g.tag for g in getattr(item, "genres", []) if g.tag})
                tv.append({
                    "title":   getattr(item, "title", "Ukendt") or "Ukendt",
                    "year":    getattr(item, "year", None),
                    "tmdb_id": _extract_tmdb_id_from_guids(item),
                    "rating":  getattr(item, "audienceRating", None) or getattr(item, "rating", None),
                    "genres":  genres,
                })
            except Exception as e:
                logger.warning("dump: item parse-fejl: %s", e)

    return {
        "metadata": {
            "scanned_at":      datetime.utcnow().isoformat() + "Z",
            "movie_count":     len(movies),
            "tv_count":        len(tv),
            "schema_version":  1,
            "description":     "Komplet dump af Plex-bibliotek.",
        },
        "movies": movies,
        "tv":     tv,
    }


async def cmd_dump_genres(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    await update.message.chat.send_action("typing")
    loading_msg = await update.message.reply_text(
        "📦 Dumper hele Plex-biblioteket til JSON...\n"
        "Dette kan tage 30-60 sekunder for store biblioteker."
    )

    try:
        data = await asyncio.to_thread(_dump_plex_data_sync)
    except Exception as e:
        logger.error("cmd_dump_genres scan-fejl: %s", e)
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(f"❌ Scan fejlede: {e}")
        return

    if data.get("error"):
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(f"❌ {data['error']}")
        return

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename  = f"plex_dump_{timestamp}.json"
    filepath  = os.path.join(tempfile.gettempdir(), filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        size_kb = os.path.getsize(filepath) / 1024
    except Exception as e:
        logger.error("cmd_dump_genres skriv-fejl: %s", e)
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(f"❌ Kunne ikke skrive fil: {e}")
        return

    logger.info("dump_genres: %d film + %d serier dumpet (%.1f KB) → %s",
                data["metadata"]["movie_count"], data["metadata"]["tv_count"], size_kb, filename)

    try:
        await loading_msg.delete()
    except Exception:
        pass

    summary = (
        f"📦 *Plex-dump færdig!*\n\n"
        f"🎬 Film:   *{data['metadata']['movie_count']}*\n"
        f"📺 Serier: *{data['metadata']['tv_count']}*\n"
        f"📊 Filstr: *{size_kb:.1f} KB*\n\n"
        f"_Send filen til Claude i chatten for analyse._"
    )

    try:
        with open(filepath, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(f, filename=filename),
                caption=summary,
                parse_mode="Markdown",
            )
        logger.info("dump_genres: fil sendt til admin telegram_id=%s", user.id)
    except Exception as e:
        logger.error("cmd_dump_genres send-fejl: %s", e)
        await update.message.reply_text(f"❌ Kunne ikke sende fil: {e}")
    finally:
        try:
            os.remove(filepath)
        except Exception as e:
            logger.warning("dump_genres: kunne ikke slette %s: %s", filepath, e)


# ══════════════════════════════════════════════════════════════════════════════
# /test_metadata — uændret
# ══════════════════════════════════════════════════════════════════════════════

def _format_metadata_report(meta: dict) -> str:
    if meta.get("status") == "error":
        return (
            f"❌ *TMDB Fejl*\n"
            f"`{meta.get('error_message', 'Ukendt fejl')}`\n"
            f"tmdb_id: `{meta.get('tmdb_id')}`"
        )

    if meta.get("status") == "not_found":
        return (
            f"⚠️ *Ikke fundet på TMDB*\n"
            f"tmdb_id: `{meta.get('tmdb_id')}`"
        )

    title       = meta["title"]
    year        = meta.get("year") or "?"
    media_type  = meta["media_type"]
    media_emoji = "🎬" if media_type == "movie" else "📺"
    genres      = meta["genres"]
    keywords    = meta["keywords"]
    g_count     = meta["genre_count"]
    k_count     = meta["keyword_count"]

    lines = [
        f"{media_emoji} *{title}* ({year})",
        f"_TMDB ID: `{meta['tmdb_id']}`_",
        "",
        f"🎭 *TMDB Genrer* ({g_count})",
        "─────────────────",
    ]
    if genres:
        for g in genres:
            lines.append(f"  • {g}")
    else:
        lines.append("  _(ingen genrer)_")

    lines.extend(["", f"🏷️ *TMDB Keywords* ({k_count})", "─────────────────"])
    if keywords:
        for kw in keywords:
            lines.append(f"  • {kw}")
    else:
        lines.append("  _(ingen keywords)_")

    if meta.get("keyword_warning"):
        lines.extend(["", f"⚠️ _{meta['keyword_warning']}_"])

    return "\n".join(lines)


def _format_search_results(query: str, results: list[dict]) -> str:
    lines = [
        f"🔍 *TMDB-søgeresultater for:* `{query}`",
        "─────────────────",
        "",
        "Klik på en linje for at hente fuld metadata:",
        "",
    ]
    for r in results:
        emoji  = "🎬" if r["media_type"] == "movie" else "📺"
        year   = r.get("year") or "?"
        cmd    = f"/test_metadata {r['media_type']} {r['tmdb_id']}"
        lines.append(f"{emoji} *{r['title']}* ({year})")
        lines.append(f"   `{cmd}`")
        lines.append("")
    return "\n".join(lines)


async def cmd_test_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "📖 *Brug:*\n"
            "`/test_metadata <tmdb_id>`\n"
            "`/test_metadata movie <tmdb_id>`\n"
            "`/test_metadata tv <tmdb_id>`\n"
            "`/test_metadata <titel>`",
            parse_mode="Markdown",
        )
        return

    await update.message.chat.send_action("typing")

    if len(args) == 2 and args[0].lower() in ("movie", "tv") and args[1].isdigit():
        media_type = args[0].lower()
        tmdb_id    = int(args[1])
        loading = await update.message.reply_text(
            f"🔍 Henter TMDB-metadata for {media_type} ID `{tmdb_id}`...",
            parse_mode="Markdown",
        )
        try:
            if media_type == "movie":
                meta = await fetch_movie_metadata(tmdb_id)
            else:
                meta = await fetch_tv_metadata(tmdb_id)
        except Exception as e:
            logger.error("test_metadata fetch-fejl: %s", e)
            await loading.edit_text(f"❌ Fejl: {e}")
            return
        report = _format_metadata_report(meta)
        try:
            await loading.delete()
        except Exception:
            pass
        await update.message.reply_text(report, parse_mode="Markdown")
        return

    if len(args) == 1 and args[0].isdigit():
        tmdb_id = int(args[0])
        loading = await update.message.reply_text(
            f"🔍 Henter TMDB-metadata for movie ID `{tmdb_id}` (default)...",
            parse_mode="Markdown",
        )
        try:
            meta = await fetch_movie_metadata(tmdb_id)
        except Exception as e:
            logger.error("test_metadata fetch-fejl: %s", e)
            await loading.edit_text(f"❌ Fejl: {e}")
            return

        if meta.get("status") == "not_found":
            try:
                meta = await fetch_tv_metadata(tmdb_id)
            except Exception:
                pass

        report = _format_metadata_report(meta)
        try:
            await loading.delete()
        except Exception:
            pass
        await update.message.reply_text(report, parse_mode="Markdown")
        return

    query = " ".join(args)
    loading = await update.message.reply_text(
        f"🔍 Søger TMDB efter: `{query}`...",
        parse_mode="Markdown",
    )
    try:
        results = await search_tmdb_by_title(query, media_type="both")
    except Exception as e:
        logger.error("test_metadata search-fejl: %s", e)
        await loading.edit_text(f"❌ Søgning fejlede: {e}")
        return

    try:
        await loading.delete()
    except Exception:
        pass

    if not results:
        await update.message.reply_text(
            f"⚠️ Ingen TMDB-resultater for `{query}`.",
            parse_mode="Markdown",
        )
        return

    if len(results) == 1:
        r = results[0]
        if r["media_type"] == "movie":
            meta = await fetch_movie_metadata(r["tmdb_id"])
        else:
            meta = await fetch_tv_metadata(r["tmdb_id"])
        report = _format_metadata_report(meta)
        await update.message.reply_text(report, parse_mode="Markdown")
        return

    report = _format_search_results(query, results)
    await update.message.reply_text(report, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 admin-kommandoer (uændret)
# ══════════════════════════════════════════════════════════════════════════════

def _scan_plex_for_seeding_sync() -> list[dict]:
    from services.plex_service import (
        _connect, _sections, _MOVIE_TYPE, _TV_TYPE,
        _extract_tmdb_id_from_guids,
    )

    plex = _connect(None)
    if isinstance(plex, dict):
        return []

    items: list[dict] = []

    for section in _sections(plex, _MOVIE_TYPE):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning("seed scan (movie): section.all() fejl '%s': %s", section.title, e)
            continue
        for item in all_items:
            tmdb_id = _extract_tmdb_id_from_guids(item)
            if not tmdb_id:
                continue
            items.append({
                "tmdb_id":    tmdb_id,
                "media_type": "movie",
                "title":      getattr(item, "title", None),
                "year":       getattr(item, "year", None),
            })

    for section in _sections(plex, _TV_TYPE):
        try:
            all_items = section.all()
        except Exception as e:
            logger.warning("seed scan (tv): section.all() fejl '%s': %s", section.title, e)
            continue
        for item in all_items:
            tmdb_id = _extract_tmdb_id_from_guids(item)
            if not tmdb_id:
                continue
            items.append({
                "tmdb_id":    tmdb_id,
                "media_type": "tv",
                "title":      getattr(item, "title", None),
                "year":       getattr(item, "year", None),
            })

    return items


async def cmd_seed_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    await update.message.chat.send_action("typing")
    loading = await update.message.reply_text(
        "🌱 *Seed Metadata*\n\n"
        "1. Scanner Plex for alle film + serier med TMDB ID...",
        parse_mode="Markdown",
    )

    try:
        items = await asyncio.to_thread(_scan_plex_for_seeding_sync)
    except Exception as e:
        logger.error("cmd_seed_metadata Plex-fejl: %s", e)
        await loading.edit_text(f"❌ Plex-scan fejlede: {e}")
        return

    if not items:
        await loading.edit_text("⚠️ Ingen Plex-items med TMDB ID fundet.")
        return

    movie_count = sum(1 for i in items if i["media_type"] == "movie")
    tv_count    = sum(1 for i in items if i["media_type"] == "tv")

    try:
        await loading.edit_text(
            f"🌱 *Seed Metadata*\n\n"
            f"✅ Scannet Plex: *{movie_count}* film + *{tv_count}* serier\n"
            f"📥 Indsætter pending records i database...",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    try:
        result = await database.seed_tmdb_metadata(items)
    except Exception as e:
        logger.error("cmd_seed_metadata DB-fejl: %s", e)
        await loading.edit_text(f"❌ Database-seed fejlede: {e}")
        return

    try:
        status = await database.get_metadata_status()
    except Exception as e:
        logger.warning("cmd_seed_metadata status-fejl: %s", e)
        status = None

    summary = (
        f"🌱 *Seed Metadata — Færdig!*\n\n"
        f"📥 *Plex-scan:*\n"
        f"  • Film: *{movie_count}*\n"
        f"  • Serier: *{tv_count}*\n"
        f"  • Total: *{len(items)}*\n\n"
        f"💾 *Database-seed:*\n"
        f"  • Nye records oprettet: *{result['inserted']}*\n"
        f"  • Allerede i DB (skipped): *{result['skipped']}*\n"
    )

    if status:
        summary += (
            f"\n📊 *Aktuel status:*\n"
            f"  • Pending: *{status['pending']}*\n"
            f"  • Fetched: *{status['fetched']}*\n"
            f"  • Error: *{status['error']}*\n"
            f"  • Not found: *{status['not_found']}*\n"
            f"  • Total: *{status['total']}*\n"
        )

    summary += (
        f"\n🚀 *Næste skridt:*\n"
        f"Kør `/fetch_metadata` flere gange (100 per batch) "
        f"indtil pending = 0."
    )

    try:
        await loading.delete()
    except Exception:
        pass
    await update.message.reply_text(summary, parse_mode="Markdown")
    logger.info("seed_metadata: %d items scanned, %d inserted, %d skipped",
                len(items), result["inserted"], result["skipped"])


async def cmd_fetch_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    args = context.args
    include_errors = bool(args and args[0].lower() == "retry")
    batch_size     = 100

    await update.message.chat.send_action("typing")
    mode_label = "med retry af errors" if include_errors else "kun pending"
    loading = await update.message.reply_text(
        f"📡 *Fetch Metadata* ({mode_label})\n\n"
        f"Henter næste batch på *{batch_size}* records fra TMDB...",
        parse_mode="Markdown",
    )

    try:
        pending = await database.get_pending_metadata(
            limit=batch_size, include_errors=include_errors,
        )
    except Exception as e:
        logger.error("cmd_fetch_metadata DB-fejl: %s", e)
        await loading.edit_text(f"❌ DB-fejl: {e}")
        return

    if not pending:
        try:
            await loading.delete()
        except Exception:
            pass
        await update.message.reply_text(
            "🎉 *Ingen pending records!*\n\n"
            "Hele cachen er færdigfetched. Kør `/metadata_status` for fuld oversigt.",
            parse_mode="Markdown",
        )
        return

    try:
        batch_result = await fetch_metadata_batch(pending)
    except Exception as e:
        logger.error("cmd_fetch_metadata TMDB-fejl: %s", e)
        await loading.edit_text(f"❌ TMDB-fejl: {e}")
        return

    write_errors = 0
    for r in batch_result["results"]:
        tmdb_id    = r.get("tmdb_id")
        media_type = r.get("media_type")
        if not tmdb_id or not media_type:
            continue

        try:
            if r["status"] == "ok":
                await database.update_metadata_success(
                    tmdb_id     = tmdb_id,
                    media_type  = media_type,
                    tmdb_genres = r.get("genres", []),
                    keywords    = r.get("keywords", []),
                    title       = r.get("title"),
                    year        = r.get("year"),
                )
            elif r["status"] == "not_found":
                await database.update_metadata_error(
                    tmdb_id       = tmdb_id,
                    media_type    = media_type,
                    error_message = r.get("message", "Ikke fundet på TMDB"),
                    is_not_found  = True,
                )
            else:
                await database.update_metadata_error(
                    tmdb_id       = tmdb_id,
                    media_type    = media_type,
                    error_message = r.get("error_message", "Ukendt fejl"),
                    is_not_found  = False,
                )
        except Exception as e:
            write_errors += 1
            logger.warning("update_metadata write-fejl for %s/%s: %s",
                           media_type, tmdb_id, e)

    summary = batch_result["summary"]
    duration = batch_result["duration_seconds"]

    try:
        status = await database.get_metadata_status()
    except Exception:
        status = None

    msg = (
        f"📡 *Fetch Metadata — Batch færdig*\n"
        f"_({mode_label})_\n\n"
        f"⏱️ *Varighed:* {duration:.1f} sek\n\n"
        f"📦 *Denne batch ({summary['total']} items):*\n"
        f"  ✅ OK: *{summary['ok']}*\n"
        f"  ⚠️ Not found: *{summary['not_found']}*\n"
        f"  ❌ Error: *{summary['error']}*\n"
    )
    if write_errors:
        msg += f"  💾 DB-write errors: *{write_errors}*\n"

    if status:
        pct = (status["fetched"] / status["total"] * 100) if status["total"] else 0
        msg += (
            f"\n📊 *Total status:*\n"
            f"  • Fetched: *{status['fetched']}* / *{status['total']}* ({pct:.1f}%)\n"
            f"  • Pending: *{status['pending']}*\n"
            f"  • Error:   *{status['error']}*\n"
            f"  • Not found: *{status['not_found']}*\n"
        )
        if status["pending"] > 0:
            batches_left = (status["pending"] + batch_size - 1) // batch_size
            msg += f"\n🚀 *Næste skridt:* `/fetch_metadata` ({batches_left} batches tilbage)"
        elif status["error"] > 0 and not include_errors:
            msg += f"\n🔁 *Retry errors:* `/fetch_metadata retry`"
        else:
            msg += "\n🎉 *Alle records er færdigbehandlet!*"

    try:
        await loading.delete()
    except Exception:
        pass
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_metadata_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    try:
        status = await database.get_metadata_status()
    except Exception as e:
        logger.error("cmd_metadata_status fejl: %s", e)
        await update.message.reply_text(f"❌ DB-fejl: {e}")
        return

    if status["total"] == 0:
        await update.message.reply_text(
            "📊 *Metadata Status*\n\n"
            "Ingen records i tmdb_metadata endnu.\n"
            "Kør `/seed_metadata` for at starte.",
            parse_mode="Markdown",
        )
        return

    pct_total = (status["fetched"] / status["total"] * 100) if status["total"] else 0
    movie     = status["by_media_type"]["movie"]
    tv        = status["by_media_type"]["tv"]

    movie_pct = (movie["fetched"] / movie["total"] * 100) if movie["total"] else 0
    tv_pct    = (tv["fetched"]    / tv["total"]    * 100) if tv["total"]    else 0

    msg = (
        f"📊 *Metadata Status*\n"
        f"═══════════════════════\n\n"
        f"🌍 *Total:* {status['fetched']:,} / {status['total']:,} ({pct_total:.1f}%)\n"
        f"  • Pending: *{status['pending']:,}*\n"
        f"  • Fetched: *{status['fetched']:,}*\n"
        f"  • Error: *{status['error']:,}*\n"
        f"  • Not found: *{status['not_found']:,}*\n\n"
        f"🎬 *Film:* {movie['fetched']:,} / {movie['total']:,} ({movie_pct:.1f}%)\n"
        f"  • Pending: {movie['pending']:,}\n"
        f"  • Error: {movie['error']:,}\n"
        f"  • Not found: {movie['not_found']:,}\n\n"
        f"📺 *Serier:* {tv['fetched']:,} / {tv['total']:,} ({tv_pct:.1f}%)\n"
        f"  • Pending: {tv['pending']:,}\n"
        f"  • Error: {tv['error']:,}\n"
        f"  • Not found: {tv['not_found']:,}\n"
    )

    if status["pending"] > 0:
        msg += f"\n🚀 *Næste:* `/fetch_metadata`"
    elif status["error"] > 0:
        msg += f"\n🔁 *Retry errors:* `/fetch_metadata retry`"
    else:
        msg += f"\n🎉 *Alle records er færdige!* Prøv `/top_keywords movie 50`"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /top_keywords — uændret
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_top_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    args = context.args
    media_type: str | None = None
    mode: str = "top"
    limit: int = 50
    min_count: int = 1

    for arg in args:
        arg_lower = arg.lower()
        if arg_lower in ("movie", "tv"):
            media_type = arg_lower
        elif arg_lower == "all":
            mode = "all"
        elif arg.isdigit():
            num = int(arg)
            if mode == "all":
                min_count = max(1, num)
            else:
                limit = max(1, min(num, 200))

    if mode == "top":
        await _cmd_top_keywords_chat(update, media_type, limit)
        return

    await _cmd_top_keywords_dump(update, context, media_type, min_count)


async def _cmd_top_keywords_chat(
    update: Update,
    media_type: str | None,
    limit: int,
) -> None:
    await update.message.chat.send_action("typing")
    loading = await update.message.reply_text(
        f"🔬 *Top Keywords*\n\n"
        f"Analyserer database...\n"
        f"_(media_type: {media_type or 'alle'}, limit: {limit})_",
        parse_mode="Markdown",
    )

    try:
        keywords = await database.get_top_keywords(
            media_type=media_type, limit=limit, min_count=1,
        )
    except Exception as e:
        logger.error("cmd_top_keywords (chat) fejl: %s", e)
        await loading.edit_text(f"❌ DB-fejl: {e}")
        return

    if not keywords:
        await loading.edit_text(
            "⚠️ *Ingen keywords i databasen endnu.*\n\n"
            "Kør først `/seed_metadata` og derefter `/fetch_metadata`.",
            parse_mode="Markdown",
        )
        return

    type_label = (
        "🎬 FILM" if media_type == "movie"
        else "📺 SERIER" if media_type == "tv"
        else "🌍 ALLE"
    )

    header = (
        f"🔬 *TOP {limit} KEYWORDS — {type_label}*\n"
        f"═══════════════════════════\n\n"
        f"_Lad dataen fortælle hvilke subgenrer du faktisk ejer:_\n"
    )

    lines = []
    for kw in keywords:
        lines.append(f"`{kw['count']:>5}` × {kw['keyword']}")

    chunk_size = 50
    chunks = [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]

    try:
        await loading.delete()
    except Exception:
        pass

    for idx, chunk in enumerate(chunks):
        if idx == 0:
            text = header + "\n".join(chunk)
        else:
            text = f"_(fortsat — del {idx + 1}/{len(chunks)})_\n\n" + "\n".join(chunk)

        try:
            await update.message.reply_text(text, parse_mode="Markdown")
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning("top_keywords send-fejl: %s — sender uden Markdown", e)
            try:
                await update.message.reply_text(text)
            except Exception:
                pass

    footer = (
        "─────────────\n"
        "💡 *Tip:* For at få ALLE keywords som JSON+CSV-fil, kør:\n"
        "`/top_keywords movie all 5` (min 5 film)"
    )
    try:
        await update.message.reply_text(footer, parse_mode="Markdown")
    except Exception:
        pass


async def _cmd_top_keywords_dump(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    media_type: str | None,
    min_count: int,
) -> None:
    user = update.effective_user

    await update.message.chat.send_action("typing")
    type_label = (
        "🎬 film" if media_type == "movie"
        else "📺 serier" if media_type == "tv"
        else "🌍 alle media"
    )
    loading = await update.message.reply_text(
        f"📦 *Keywords-dump*\n\n"
        f"Henter ALLE keywords for *{type_label}* "
        f"med minimum *{min_count}* film...\n"
        f"_Dette kan tage 5-15 sekunder._",
        parse_mode="Markdown",
    )

    try:
        keywords = await database.get_top_keywords(
            media_type=media_type, limit=None, min_count=min_count,
        )
    except Exception as e:
        logger.error("cmd_top_keywords (dump) DB-fejl: %s", e)
        await loading.edit_text(f"❌ DB-fejl: {e}")
        return

    if not keywords:
        await loading.edit_text(
            f"⚠️ *Ingen keywords fundet*\n\n"
            f"Med min_count={min_count} blev der ikke fundet nogen keywords.\n"
            f"Prøv et lavere tal eller kør `/fetch_metadata` flere gange.",
            parse_mode="Markdown",
        )
        return

    try:
        status = await database.get_metadata_status()
        if media_type == "movie":
            total_items = status["by_media_type"]["movie"]["fetched"]
        elif media_type == "tv":
            total_items = status["by_media_type"]["tv"]["fetched"]
        else:
            total_items = status["fetched"]
    except Exception:
        total_items = 0

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    type_suffix = media_type if media_type else "all"
    base_filename = f"keywords_{type_suffix}_min{min_count}_{timestamp}"

    json_path = os.path.join(tempfile.gettempdir(), f"{base_filename}.json")
    csv_path  = os.path.join(tempfile.gettempdir(), f"{base_filename}.csv")

    json_data = {
        "metadata": {
            "scanned_at":            datetime.utcnow().isoformat() + "Z",
            "media_type":            media_type or "all",
            "min_count_filter":      min_count,
            "total_unique_keywords": len(keywords),
            "items_in_db":           total_items,
        },
        "keywords": [
            {
                "rank":       i + 1,
                "keyword":    kw["keyword"],
                "film_count": kw["count"],
                "percentage": round(kw["count"] / total_items * 100, 2) if total_items else 0.0,
            }
            for i, kw in enumerate(keywords)
        ],
    }

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        json_size_kb = os.path.getsize(json_path) / 1024
    except Exception as e:
        logger.error("cmd_top_keywords JSON skriv-fejl: %s", e)
        await loading.edit_text(f"❌ Kunne ikke skrive JSON: {e}")
        return

    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["rank", "keyword", "film_count", "percentage"])
            for i, kw in enumerate(keywords):
                pct = round(kw["count"] / total_items * 100, 2) if total_items else 0.0
                writer.writerow([i + 1, kw["keyword"], kw["count"], f"{pct}%"])
        csv_size_kb = os.path.getsize(csv_path) / 1024
    except Exception as e:
        logger.error("cmd_top_keywords CSV skriv-fejl: %s", e)
        try:
            os.remove(json_path)
        except Exception:
            pass
        await loading.edit_text(f"❌ Kunne ikke skrive CSV: {e}")
        return

    logger.info(
        "top_keywords dump: %d keywords (media=%s, min=%d) — JSON %.1f KB, CSV %.1f KB",
        len(keywords), media_type or "all", min_count, json_size_kb, csv_size_kb,
    )

    try:
        await loading.delete()
    except Exception:
        pass

    summary = (
        f"📦 *Keywords-dump færdig!*\n\n"
        f"🎯 *Filter:* {type_label}, min {min_count} film\n"
        f"📊 *Resultat:*\n"
        f"  • Unikke keywords: *{len(keywords):,}*\n"
        f"  • Total items i DB: *{total_items:,}*\n"
        f"  • Mest brugte: *{keywords[0]['keyword']}* ({keywords[0]['count']} film)\n"
        f"  • Mindst brugte: *{keywords[-1]['keyword']}* ({keywords[-1]['count']} film)\n\n"
        f"📁 *Filer:*\n"
        f"  • JSON: {json_size_kb:.1f} KB\n"
        f"  • CSV: {csv_size_kb:.1f} KB\n\n"
        f"_Send filerne til Claude i chatten for analyse._\n"
        f"_CSV kan åbnes i Google Sheets/Excel._"
    )

    try:
        with open(json_path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(f, filename=os.path.basename(json_path)),
                caption=summary,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error("cmd_top_keywords JSON send-fejl: %s", e)
        await update.message.reply_text(f"❌ Kunne ikke sende JSON: {e}")
        try:
            os.remove(json_path)
            os.remove(csv_path)
        except Exception:
            pass
        return

    try:
        with open(csv_path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(f, filename=os.path.basename(csv_path)),
                caption="📊 *CSV — åbn i Google Sheets/Excel*",
                parse_mode="Markdown",
            )
        logger.info("top_keywords dump: filer sendt til admin telegram_id=%s", user.id)
    except Exception as e:
        logger.error("cmd_top_keywords CSV send-fejl: %s", e)
        await update.message.reply_text(f"❌ Kunne ikke sende CSV: {e}")
    finally:
        for path in (json_path, csv_path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning("top_keywords cleanup-fejl for %s: %s", path, e)


# ══════════════════════════════════════════════════════════════════════════════
# /test_v2 — admin DEBUG-kommando (uændret fra v0.10.8)
# ══════════════════════════════════════════════════════════════════════════════

def _format_subgenre_list() -> str:
    lines = [
        "📋 *Alle subgenre-IDs*",
        "═══════════════════════",
        "",
        "_Brug `/test_v2 <subgenre_id>` for at teste én._",
        "",
    ]
    for cat in get_all_categories():
        lines.append(f"*{cat['label']}*")
        for sub in cat["subgenres"]:
            lines.append(f"  • `{sub['id']}` — {sub['label']}")
        lines.append("")
    return "\n".join(lines)


def _format_v2_result(result: dict) -> str:
    status = result.get("status")

    if status == "error":
        return (
            f"❌ *Fejl*\n\n"
            f"`{result.get('message', 'Ukendt fejl')}`"
        )

    if status == "missing":
        label = result.get("subgenre_label", "?")
        stats = result.get("stats", {})
        return (
            f"😔 *{label}*\n"
            f"_Subgenre: `{result.get('subgenre', '?')}`_\n\n"
            f"⚠️ *Ingen usete film fundet*\n\n"
            f"📊 *Stats:*\n"
            f"  • DB-kandidater: *{stats.get('db_candidates', 0)}*\n"
            f"  • I Plex: *{stats.get('in_plex', 0)}*\n"
            f"  • Usete: *{stats.get('unwatched', 0)}*\n"
            f"  • Returneret: *{stats.get('returned', 0)}*\n\n"
            f"_Mulige årsager:_\n"
            f"  • Subgenren har ingen kandidater i din samling\n"
            f"  • Du har set alle film der matcher\n"
            f"  • TMDB cachen mangler endnu"
        )

    label    = result.get("subgenre_label", "?")
    sub_id   = result.get("subgenre", "?")
    stats    = result.get("stats", {})
    results  = result.get("results", [])

    lines = [
        f"🎯 *{label}*",
        f"_Subgenre: `{sub_id}`_",
        "",
        f"📊 *Stats:*",
        f"  • DB-kandidater: *{stats.get('db_candidates', 0)}*",
        f"  • I Plex: *{stats.get('in_plex', 0)}*",
        f"  • Usete: *{stats.get('unwatched', 0)}*",
        f"  • Returneret: *{stats.get('returned', 0)}*",
        "",
        f"✨ *Forslag ({len(results)}):*",
    ]

    for film in results:
        title  = film.get("title", "Ukendt")
        year   = film.get("year") or "?"
        rating = film.get("rating")
        rating_str = f" — {rating:.1f}/10" if rating else ""
        lines.append(f"  🎬 *{title}* ({year}){rating_str}")

    lines.extend([
        "",
        "─────────────",
        "_Test resultater:_",
        f"  ✅ Funktionen virker hvis stats viser realistiske tal",
        f"  ✅ Smart-blanding hvis film er en mix af nye + klassikere",
        f"  ⚠️ Hvis 'I Plex' er meget lavere end 'DB-kandidater', "
        f"er TMDB-cachen ude af sync med Plex-biblioteket",
    ])

    return "\n".join(lines)


async def cmd_test_v2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "📖 *Brug:*\n"
            "`/test_v2 list` — vis alle subgenre-IDs\n"
            "`/test_v2 <subgenre_id>` — test specifik\n\n"
            "*Eksempler:*\n"
            "`/test_v2 horror_slasher`\n"
            "`/test_v2 comedy_romcom`\n"
            "`/test_v2 special_revenge`",
            parse_mode="Markdown",
        )
        return

    if args[0].lower() == "list":
        report = _format_subgenre_list()
        if len(report) > 3500:
            mid = len(report) // 2
            split_at = report.rfind("\n", 0, mid)
            await update.message.reply_text(report[:split_at], parse_mode="Markdown")
            await asyncio.sleep(0.3)
            await update.message.reply_text(report[split_at:], parse_mode="Markdown")
        else:
            await update.message.reply_text(report, parse_mode="Markdown")
        return

    subgenre_id = args[0].lower().strip()

    if not validate_subgenre_id(subgenre_id):
        all_ids = list_subgenre_ids()
        suggestions = [sid for sid in all_ids if subgenre_id in sid or sid.startswith(subgenre_id[:4])][:5]
        suggestion_text = ""
        if suggestions:
            suggestion_text = "\n\n*Mente du:*\n" + "\n".join(f"  • `{s}`" for s in suggestions)

        await update.message.reply_text(
            f"⚠️ *Ukendt subgenre:* `{subgenre_id}`\n"
            f"{suggestion_text}\n\n"
            f"Kør `/test_v2 list` for fuld oversigt.",
            parse_mode="Markdown",
        )
        return

    plex_username = await database.get_plex_username(user.id)

    await update.message.chat.send_action("typing")
    loading = await update.message.reply_text(
        f"🧪 *Tester find_unwatched_v2*\n\n"
        f"Subgenre: `{subgenre_id}`\n"
        f"Plex-bruger: `{plex_username or 'admin (fallback)'}`\n\n"
        f"_Kører DB-query, Plex-scan og filter..._",
        parse_mode="Markdown",
    )

    try:
        result = await find_unwatched_v2(
            subgenre_id=subgenre_id,
            plex_username=plex_username,
            limit=5,
        )
    except Exception as e:
        logger.error("cmd_test_v2 fejl for '%s': %s", subgenre_id, e)
        await loading.edit_text(f"❌ Uventet fejl: {e}")
        return

    try:
        await loading.delete()
    except Exception:
        pass

    report = _format_v2_result(result)

    try:
        await update.message.reply_text(report, parse_mode="Markdown")
    except Exception as e:
        logger.warning("cmd_test_v2 Markdown-fejl: %s — sender plain", e)
        plain = report.replace("*", "").replace("_", "").replace("`", "")
        await update.message.reply_text(plain)

    logger.info(
        "test_v2: subgenre='%s' → status='%s' (%d returneret)",
        subgenre_id, result.get("status"), len(result.get("results", [])),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Markdown helpers
# ══════════════════════════════════════════════════════════════════════════════

def escape_markdown(text: str) -> str:
    def _escape_url(match: re.Match) -> str:
        return match.group(1).replace("_", r"\_")
    return _URL_RE.sub(_escape_url, text)


# ══════════════════════════════════════════════════════════════════════════════
# Guards (uændret)
# ══════════════════════════════════════════════════════════════════════════════

async def _guard(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    await database.upsert_user(user.id, user.username or user.first_name)
    if not await database.is_whitelisted(user.id):
        await notify_admin_new_user(update)
        return False
    return True


async def _needs_plex_setup(update: Update) -> bool:
    user = update.effective_user
    plex_username = await database.get_plex_username(user.id)
    if plex_username:
        return False
    onboarding_state = await database.get_onboarding_state(user.id)
    if onboarding_state == "awaiting_plex":
        return True
    await database.set_onboarding_state(user.id, "awaiting_plex")
    await update.message.reply_text(
        f"👋 Hej {user.first_name}!\n\n"
        "For at jeg kan give dig personlige svar, skal jeg kende dit "
        "Plex-brugernavn.\n\n"
        "Skriv det herunder - jeg tjekker det med det samme 🎬"
    )
    return True


async def _handle_plex_input(update: Update, raw_input: str) -> None:
    user = update.effective_user
    await update.message.chat.send_action("typing")
    result = await validate_plex_user(raw_input.strip())
    if not result.get("valid"):
        await update.message.reply_text(
            f"❌ Jeg kan ikke finde *{raw_input}* på Plex-serveren.\n\n"
            "Tjek stavningen og prøv igen.",
            parse_mode="Markdown",
        )
        return
    verified = result["username"]
    await database.set_plex_username(user.id, verified)
    await update.message.reply_text(
        f"✅ Perfekt! Du er nu koblet til Plex som *{verified}*.\n\nHvad kan jeg hjælpe dig med? 🚀",
        parse_mode="Markdown",
        reply_markup=_build_main_reply_keyboard(),
    )
    logger.info("Onboarding complete — telegram_id=%s plex='%s'", user.id, verified)


# ══════════════════════════════════════════════════════════════════════════════
# Command handlers (uændret)
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    clear_history(user.id)
    await database.log_message(user.id, "incoming", "/start")
    if await _needs_plex_setup(update):
        return
    reply = (
        f"👋 Hej {user.first_name}!\n\n"
        "Jeg er din personlige medie-assistent. Du kan bl.a. spørge mig om:\n"
        "• 🎬 Film og serier i dit Plex-bibliotek\n"
        "• ➕ Bestilling af ny film eller serie\n"
        "• 📺 Hvad der er på vej\n\n"
        "Eller tryk på 🍿 *Hvad skal jeg se?* for at finde noget at se nu!"
    )
    await update.message.reply_text(
        reply,
        parse_mode="Markdown",
        reply_markup=_build_main_reply_keyboard(),
    )
    await database.log_message(user.id, "outgoing", reply)


async def cmd_skift_plex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    await database.set_onboarding_state(user.id, "awaiting_plex")
    await database.log_message(user.id, "incoming", "/skift_plex")
    await update.message.reply_text(
        "Intet problem! 👌\nSkriv dit nye *Plex-brugernavn* herunder:",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Bestillingsflow callbacks (uændret)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _guard(update):
        return
    token = query.data.split(":", 1)[1]
    plex_username = await database.get_plex_username(query.from_user.id)
    await show_confirmation(query, context, token, plex_username)


async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _guard(update):
        return
    token = query.data.split(":", 1)[1]
    plex_username = await database.get_plex_username(query.from_user.id)
    await execute_order(query, token, plex_username)



async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle ❌ Annuller knap pa infokort og soegeresultater.

    UX (v0.11.1): Sletter hele beskeden (foto + caption + knapper) og viser
    en kort 'Annulleret' toast i toppen af Telegram. Tidligere version
    redigerede kun caption'en, hvilket efterlod plakaten staende uden
    handlingsmuligheder — det forvirrede brugere og rodede chat-historik.

    Toast vs. ny besked: Toast er Telegram-native UX (forsvinder selv efter
    ~3 sek), holder chat-historikken ren, og giver tydelig feedback om at
    handlingen lykkedes.
    """
    query = update.callback_query

    # Vis toast i toppen af Telegram (forsvinder selv)
    await query.answer(text="Annulleret 👍", show_alert=False)

    # Ryd op i pending_requests (samme som foer)
    token = query.data.split(":", 1)[1]
    if token != "none":
        await database.get_pending_request(token)

    # Slet hele beskeden — foto, caption og knapper
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning("handle_cancel_callback delete fejl: %s", e)


async def handle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _guard(update):
        return
    token = query.data.split(":", 1)[1]
    pending = await database.get_pending_request(token)
    if not pending:
        await query.edit_message_text("Sessionen er udløbet — start forfra.")
        return
    search_query = pending["title"]
    media_type   = pending["media_type"]
    try:
        await query.message.delete()
    except Exception:
        pass
    await show_search_results(query.message, search_query, media_type)


# ══════════════════════════════════════════════════════════════════════════════
# /info_movie_<id> og /info_tv_<id> handler (uændret)
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# /info_movie_<id> og /info_tv_<id> handler (v0.12.1 — fix #1 double-fetch)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_info_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bruger trykkede paa et /info_movie_<id> eller /info_tv_<id> link.

    v0.12.1 (fix #1): Skip det indledende get_media_details kald.
    show_confirmation henter alligevel detaljer selv, saa det dobbelte
    kald spildte 150-200ms per infokort + halvt saa mange TMDB-kald.
    Vi bruger 'Slaar op...' som placeholder-titel — show_confirmation
    overskriver den med rigtige detaljer fra TMDB (samme moenster som
    INFO_SIGNAL flowet i _process_ai_reply).
    """
    if not await _guard(update):
        return
    logger.info("HANDLER MODTOG: %s", update.message.text)
    if context.matches:
        match = context.matches[0]
    else:
        text  = (update.message.text or "").strip()
        match = re.match(r"^/info_?(movie|tv)_?(\d+)$", text)
        if not match:
            logger.warning("HANDLER: ingen match paa '%s' — ignorerer", update.message.text)
            return
    media_type    = match.group(1)
    tmdb_id       = int(match.group(2))
    logger.info("Bruger trykkede paa info-link: type=%s, id=%s", media_type, tmdb_id)
    user_id       = update.effective_user.id
    plex_username = await database.get_plex_username(user_id)
    await update.message.chat.send_action("typing")
    loading_msg = await update.message.reply_text(
        "🤖 Beregner svar med lynets hast... næsten...",
        reply_markup=ReplyKeyboardRemove(),
    )

    # v0.12.1: Spring den dobbelte get_media_details over.
    # show_confirmation henter alligevel detaljer fra TMDB — vi gemmer
    # bare en placeholder-titel der overskrives senere.
    import secrets as _sec
    token = _sec.token_hex(8)
    await database.save_pending_request(token, user_id, {
        "media_type": media_type,
        "tmdb_id":    tmdb_id,
        "title":      "Slaar op...",
        "year":       None,
        "step":       "picked",
    })
    try:
        await update.message.delete()
    except Exception:
        pass
    await show_confirmation(update.message, context, token, plex_username,
                            loading_msg=loading_msg)


# ══════════════════════════════════════════════════════════════════════════════
# Message handler (uændret)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    text = (update.message.text or "").strip()
    if text == WATCH_FLOW_TRIGGER:
        await handle_watch_flow_trigger(update, context)
        return
    await database.log_message(user.id, "incoming", text)
    onboarding_state = await database.get_onboarding_state(user.id)
    if onboarding_state == "awaiting_plex":
        await _handle_plex_input(update, text)
        return
    if await _needs_plex_setup(update):
        return
    if check_session_timeout(user.id):
        await cmd_start(update, context)
        return
    await update.message.chat.send_action("typing")
    plex_username = await database.get_plex_username(user.id)
    persona_id    = await database.get_persona(user.id)
    loading_msg = await update.message.reply_text(
        "🤖 Beregner svar med lynets hast... næsten...",
    )
    reply = await get_ai_response(
        telegram_id=user.id,
        user_message=text,
        plex_username=plex_username,
        persona_id=persona_id,
        user_first_name=user.first_name,
    )
    try:
        await loading_msg.delete()
    except Exception:
        pass
    await _process_ai_reply(update, context, update.message.chat, user, plex_username, reply)


async def _process_ai_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat,
    user,
    plex_username: str | None,
    reply: str,
) -> None:
    clean_reply = reply.replace("`", "").strip()

    def _find_signal(signal: str) -> str | None:
        for line in clean_reply.splitlines():
            line = line.strip()
            if line.startswith(signal):
                return line
        return None

    signal_line = _find_signal(SEARCH_SIGNAL)
    if signal_line:
        parts = signal_line[len(SEARCH_SIGNAL):].split(":", 1)
        query_term = parts[0].strip()
        media_type = parts[1].strip() if len(parts) > 1 else "both"
        target_message = update.message if update.message else None
        if target_message is None:
            target_message = await chat.send_message("…")
        await show_search_results(target_message, query_term, media_type)
        return

    signal_line = _find_signal(INFO_SIGNAL)
    if signal_line:
        payload = signal_line[len(INFO_SIGNAL):].strip()
        parts   = payload.split(":")
        if len(parts) >= 2:
            tmdb_id_str = parts[0].strip()
            media_type  = parts[1].strip()
            try:
                from services.confirmation_service import _make_token
                token = _make_token()
                await database.save_pending_request(token, user.id, {
                    "media_type": media_type,
                    "tmdb_id":    int(tmdb_id_str),
                    "title":      "Slår op...",
                    "step":       "picked",
                })
                trigger_msg = update.message if update.message else await chat.send_message("…")
                await show_confirmation(trigger_msg, context, token, plex_username)
                return
            except Exception as e:
                logger.error("Fejl ved håndtering af SHOW_INFO: %s", e)
        else:
            logger.warning("SHOW_INFO signal kunne ikke parses: %r", reply)

    signal_line = _find_signal(TRAILER_SIGNAL)
    if signal_line:
        payload  = signal_line[len(TRAILER_SIGNAL):]
        pipe_idx = payload.rfind("|")
        if pipe_idx != -1:
            besked_tekst = payload[:pipe_idx].strip()
            trailer_url  = payload[pipe_idx + 1:].strip()
            safe_reply = escape_markdown(besked_tekst)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Se Trailer", url=trailer_url)
            ]])
            await chat.send_message(
                safe_reply,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            logger.info("Trailer-knap sendt: %s", trailer_url)
            await database.log_message(user.id, "outgoing", besked_tekst)
            return

    safe_reply = escape_markdown(reply)
    await chat.send_message(safe_reply, parse_mode="Markdown")
    await database.log_message(user.id, "outgoing", reply)


# ══════════════════════════════════════════════════════════════════════════════
# Webhook HTTP server (uændret)
# ══════════════════════════════════════════════════════════════════════════════

async def _webhook_radarr(request: web.Request) -> web.Response:
    if config.WEBHOOK_SECRET:
        token = request.rel_url.query.get("token", "")
        if token != config.WEBHOOK_SECRET:
            logger.warning("Radarr webhook: uautoriseret request fra %s", request.remote)
            return web.Response(status=401, text="Unauthorized")
    try:
        payload = await request.json()
        logger.info("Radarr webhook received: eventType=%s", payload.get("eventType"))
        asyncio.create_task(handle_radarr_webhook(payload))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error("Radarr webhook error: %s", e)
        return web.Response(status=400, text=str(e))


async def _webhook_sonarr(request: web.Request) -> web.Response:
    if config.WEBHOOK_SECRET:
        token = request.rel_url.query.get("token", "")
        if token != config.WEBHOOK_SECRET:
            logger.warning("Sonarr webhook: uautoriseret request fra %s", request.remote)
            return web.Response(status=401, text="Unauthorized")
    try:
        payload = await request.json()
        logger.info("Sonarr webhook received: eventType=%s", payload.get("eventType"))
        asyncio.create_task(handle_sonarr_webhook(payload))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error("Sonarr webhook error: %s", e)
        return web.Response(status=400, text=str(e))


async def _start_webhook_server() -> None:
    app = web.Application()
    app.router.add_post("/webhook/radarr", _webhook_radarr)
    app.router.add_post("/webhook/sonarr", _webhook_sonarr)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Webhook server started on port 8080")


# ══════════════════════════════════════════════════════════════════════════════
# Global error handler (uændret)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "Uventet fejl ved håndtering af update:\n%s",
        "".join(traceback.format_exception(
            type(context.error), context.error, context.error.__traceback__
        )),
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Hov, jeg fik vist popcorn galt i halsen! 🍿\n"
                "Noget gik uventet galt i maskinrummet. Prøv igen om lidt."
            )
        except Exception as e:
            logger.error("Kunne ikke sende fejlbesked til bruger: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════════

async def on_startup(application: Application) -> None:
    await database.setup_db()
    await database.setup_pending_requests()
    await database.setup_tmdb_metadata_table()
    await _start_webhook_server()
    if not config.WEBHOOK_SECRET:
        logger.warning(
            "WEBHOOK_SECRET er ikke sat — webhooks accepteres uden token-tjek!"
        )
    logger.info("Buddy started in '%s' environment.", config.ENVIRONMENT)
    logger.info(
        "VERSION CHECK — v0.11.0-beta | "
        "subgenre-watch-flow: JA | tmdb-metadata-cache: JA | "
        "find-unwatched-v2: JA | test-v2-cmd: JA | "
        "top-keywords-dump: JA | gammel-watch-moods: SLETTET"
    )


async def on_shutdown(application: Application) -> None:
    await database.close_db()
    logger.info("Buddy shut down cleanly.")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",           cmd_start))
    app.add_handler(CommandHandler("skift_plex",      cmd_skift_plex))

    # Engangs admin-kommandoer (slettes når subgenre-projektet er færdigt)
    app.add_handler(CommandHandler("genres",          cmd_genres))
    app.add_handler(CommandHandler("dump_genres",     cmd_dump_genres))
    app.add_handler(CommandHandler("test_metadata",   cmd_test_metadata))

    # TMDB metadata-cache admin-kommandoer
    app.add_handler(CommandHandler("seed_metadata",   cmd_seed_metadata))
    app.add_handler(CommandHandler("fetch_metadata",  cmd_fetch_metadata))
    app.add_handler(CommandHandler("metadata_status", cmd_metadata_status))
    app.add_handler(CommandHandler("top_keywords",    cmd_top_keywords))

    # Etape 2 debug-kommando
    app.add_handler(CommandHandler("test_v2",         cmd_test_v2))

    # Admin approval
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve_user:\d+$"))

    # ── NYT Watch Flow (Etape 3 — v0.11.0) ──────────────────────────────────
    app.add_handler(CallbackQueryHandler(handle_subgenre_category_callback, pattern=r"^sg_cat:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_pick_callback,     pattern=r"^sg_pick:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_random_callback,   pattern=r"^sg_random:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_refresh_callback,  pattern=r"^sg_refresh:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_next_callback,     pattern=r"^sg_next:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_back_callback,     pattern=r"^sg_back:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_cancel_callback,   pattern=r"^sg_cancel$"))

    # Bestillingsflow
    app.add_handler(CallbackQueryHandler(handle_pick_callback,      pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(handle_confirm_callback,   pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_callback,    pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(handle_watchlist_callback, pattern=r"^watchlist:"))
    app.add_handler(CallbackQueryHandler(handle_back_callback,      pattern=r"^back:"))

    # Info-links
    app.add_handler(MessageHandler(
        (filters.COMMAND | filters.TEXT) & filters.Regex(r"^/info_?(movie|tv)_?(\d+)$"),
        handle_info_link,
    ))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(handle_error)

    logger.info("Starting polling …")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()