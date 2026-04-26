"""
main.py - Buddy bot entry point.

CHANGES vs previous version (v0.10.0 — 'Hvad skal jeg se?' interaktivt flow):
  - NY FEATURE: Stemnings-baseret browse-flow med pagination.
    * Fast ReplyKeyboard-knap '🍿 Hvad skal jeg se?' tilføjet til cmd_start
      og _handle_plex_input (efter onboarding).
    * Inline keyboard-flow: vælg type (Film/Serie/Overrask mig) → vælg op til
      2 stemninger på tværs af 3 sider → 🚀 Søg nu! eller 🎲 Overrask mig.
    * 17 stemninger mappet til Plex-genrer i WATCH_MOODS-tabellen.
    * State management via context.user_data["watch_flow"] (in-memory, per user).
    * 🚀 Søg nu! er disabled (vises som '🔒 Vælg en stemning') indtil mindst
      1 stemning er valgt.
    * Valgte stemninger markeres med '✅' præfiks.
    * Pagination: side 1 = stemninger 1-6, side 2 = 7-12, side 3 = 13-17.
    * AI handoff: build_watch_prompt() bygger en kontekst-prompt og sender
      til get_ai_response (samme path som handle_text — bevarer signal-parsing
      til SHOW_INFO/TRAILER/SEARCH_RESULTS).
  - VERSION CHECK opdateret til v0.10.0-beta.

UNCHANGED (v0.9.8 — Annuller-knap photo-fix):
  - handle_cancel_callback bruger edit_message_caption() for photo-beskeder.

UNCHANGED (v0.9.7 — søgeresultater UX-fix):
  - handle_back_callback: ⬅️ Tilbage-knap i søgeresultatlisten.

Tidligere ændringer (bevares):
  - v0.9.5: user_first_name sendes til get_ai_response.
  - v0.9.3: persona-rens, SHOW_INFO/TRAILER/SEARCH_RESULTS signal-arkitektur.
  - handle_watchlist_callback importeret fra confirmation_service.
  - escape_markdown for URL-underscores.
  - Webhook server på port 8080 med valgfri token-tjek.
"""

import asyncio
import logging
import re
import sys
import traceback
from aiohttp import web

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
from services.webhook_service import handle_radarr_webhook, handle_sonarr_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Matcher alle http(s)-URL'er — bruges til escape af underscores i tekst-beskeder
_URL_RE = re.compile(r"(https?://[^\s)\]>\"]+)")


# ══════════════════════════════════════════════════════════════════════════════
# 'HVAD SKAL JEG SE?' — Konstanter og helpers
# ══════════════════════════════════════════════════════════════════════════════

# Tekst på fast ReplyKeyboard-knap. Også brugt som trigger i MessageHandler.
WATCH_FLOW_TRIGGER = "🍿 Hvad skal jeg se?"

# Max antal stemninger brugeren må vælge på tværs af alle sider
MAX_MOODS = 2

# Stemnings-katalog. ID 1-17. Hver stemning mapper til 1+ Plex-genrer.
# Genre-navne skal matche Plex-bibliotekets genre-tags (på dansk).
WATCH_MOODS: dict[int, dict] = {
    1:  {"emoji": "🛋️",  "name": "Netflix and Chill",       "genres": ["Komedie", "Romantik"]},
    2:  {"emoji": "😂",  "name": "Grin til jeg græder",     "genres": ["Komedie"]},
    3:  {"emoji": "💥",  "name": "Hjernedød Action",        "genres": ["Action", "Action/Eventyr"]},
    4:  {"emoji": "😱",  "name": "Gem mig bag puden",       "genres": ["Gyser", "Suspense"]},
    5:  {"emoji": "🤯",  "name": "Mindfuck",                 "genres": ["Mysterium", "Sci-fi", "Thriller"]},
    6:  {"emoji": "😭",  "name": "Find lommetørklædet",     "genres": ["Drama"]},
    7:  {"emoji": "🕵️", "name": "Hvem gjorde det?",         "genres": ["Kriminalitet", "Mysterium"]},
    8:  {"emoji": "❤️",  "name": "Kærlighed & Kliché",      "genres": ["Romantik", "Komedie"]},
    9:  {"emoji": "🧸",  "name": "Ungerne styrer",           "genres": ["Familie", "Children", "Animation"]},
    10: {"emoji": "🐉",  "name": "Væk fra virkeligheden",   "genres": ["Fantasy", "Adventure", "Sci-Fi & Fantasy"]},
    11: {"emoji": "🍷",  "name": "Snobbet Mesterværk",       "genres": ["Drama", "Biography"]},
    12: {"emoji": "🧠",  "name": "Gør mig klogere",          "genres": ["Documentary", "Historie"]},
    13: {"emoji": "🎇",  "name": "Visuelt festfyrværkeri",  "genres": ["Action", "Sci-fi", "Fantasy"]},
    14: {"emoji": "🔪",  "name": "Mørkt og sandt",           "genres": ["Crime", "Documentary"]},
    15: {"emoji": "🥷",  "name": "Slå på tæven",             "genres": ["Martial Arts", "Action"]},
    16: {"emoji": "🎸",  "name": "Skru op for anlægget",    "genres": ["Musik", "Musical"]},
    17: {"emoji": "🔫",  "name": "Tilbage til skyttegraven","genres": ["Krig", "Historie", "War & Politics"]},
}

# Pagination: hvilken stemning er på hvilken side
WATCH_PAGES: dict[int, list[int]] = {
    1: [1, 2, 3, 4, 5, 6],
    2: [7, 8, 9, 10, 11, 12],
    3: [13, 14, 15, 16, 17],
}
TOTAL_PAGES = 3


def _build_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Permanent ReplyKeyboard nederst i Telegram-vinduet.
    Vises efter onboarding er færdig og ved /start.
    """
    return ReplyKeyboardMarkup(
        [[KeyboardButton(WATCH_FLOW_TRIGGER)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _get_watch_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """
    Hent (eller initialiser) brugerens 'watch flow' state i context.user_data.
    State er in-memory og lever pr. user_id mellem callbacks.
    """
    if "watch_flow" not in context.user_data:
        context.user_data["watch_flow"] = {
            "media_type":     None,    # 'movie' | 'tv' | None
            "selected_moods": [],      # liste af mood IDs (max 2)
            "page":           1,       # aktuel side (1-3)
        }
    return context.user_data["watch_flow"]


def _clear_watch_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ryd watch flow state — kaldes efter handoff til AI eller ved cancel."""
    context.user_data.pop("watch_flow", None)


def _build_type_selection_keyboard() -> InlineKeyboardMarkup:
    """Trin 2: vis Film / Serie / Overrask mig som 1. valg."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Film",   callback_data="watch_type:movie"),
            InlineKeyboardButton("📺 Serie",  callback_data="watch_type:tv"),
        ],
        [InlineKeyboardButton("🎲 Overrask mig!", callback_data="watch_type:surprise")],
        [InlineKeyboardButton("❌ Afbryd",        callback_data="watch_cancel")],
    ])


def _build_mood_keyboard(state: dict) -> InlineKeyboardMarkup:
    """
    Trin 3: vis stemnings-knapper for aktuel side + navigation + handoff-knapper.

    Layout per side:
      Stemninger 2 pr. række (op til 3 rækker på side 1-2, 2 rækker + 1 enkelt på side 3)
      Navigation:    [⬅️] [➡️]   (kun de relevante)
      Handoff:       [🚀 Søg nu!]  [🎲 Overrask mig!]
      Footer:        [❌ Afbryd]
    """
    page          = state["page"]
    selected      = set(state["selected_moods"])
    media_type    = state["media_type"]   # 'movie' eller 'tv' (aldrig 'surprise' her)
    mood_ids      = WATCH_PAGES[page]

    rows: list[list[InlineKeyboardButton]] = []

    # ── Stemnings-knapper, 2 pr. række ────────────────────────────────────────
    row: list[InlineKeyboardButton] = []
    for mood_id in mood_ids:
        mood   = WATCH_MOODS[mood_id]
        prefix = "✅ " if mood_id in selected else ""
        label  = f"{prefix}{mood['emoji']} {mood['name']}"
        row.append(InlineKeyboardButton(label, callback_data=f"watch_mood:{mood_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:  # ulige antal — sidste knap alene
        rows.append(row)

    # ── Navigation: ⬅️ og/eller ➡️ ────────────────────────────────────────────
    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Forrige", callback_data=f"watch_page:{page-1}"))
    if page < TOTAL_PAGES:
        nav_row.append(InlineKeyboardButton("Næste ➡️",   callback_data=f"watch_page:{page+1}"))
    if nav_row:
        rows.append(nav_row)

    # ── Handoff: 🚀 Søg nu (disabled hvis ingen stemninger) + 🎲 Overrask ─────
    if selected:
        search_btn = InlineKeyboardButton("🚀 Søg nu!", callback_data="watch_search")
    else:
        # 'Disabled' vises som låst tekst — callback_data="watch_noop"
        search_btn = InlineKeyboardButton("🔒 Vælg en stemning", callback_data="watch_noop")
    rows.append([
        search_btn,
        InlineKeyboardButton("🎲 Overrask mig!", callback_data="watch_surprise"),
    ])

    # ── Footer: ❌ Afbryd ─────────────────────────────────────────────────────
    rows.append([InlineKeyboardButton("❌ Afbryd", callback_data="watch_cancel")])

    return InlineKeyboardMarkup(rows)


def _build_mood_message_text(state: dict) -> str:
    """
    Bygger besked-teksten over stemnings-tastaturet.
    Viser type, antal valgte stemninger og side-info.
    """
    media_label = "🎬 *Film*" if state["media_type"] == "movie" else "📺 *Serie*"
    selected    = state["selected_moods"]
    page        = state["page"]

    if not selected:
        status = "_Vælg op til 2 stemninger_"
    else:
        names = [f"{WATCH_MOODS[m]['emoji']} {WATCH_MOODS[m]['name']}" for m in selected]
        status = f"_Valgt:_ {', '.join(names)} ({len(selected)}/{MAX_MOODS})"

    return (
        f"{media_label} — hvilken stemning er du i?\n\n"
        f"{status}\n\n"
        f"_Side {page} af {TOTAL_PAGES}_"
    )


def _build_watch_prompt(state: dict, mode: str) -> str:
    """
    Bygger den prompt der sendes til get_ai_response.

    mode='search'    → genre-baseret søgning (4-5 forslag)
    mode='surprise'  → tilfældig perle (4-5 forslag inden for typen)
    mode='wildcard'  → tilfældig perle uden type-filter (4-5 forslag, fra hovedmenu)
    """
    media_type = state.get("media_type")
    media_word = "film" if media_type == "movie" else ("serier" if media_type == "tv" else "film eller serier")

    if mode == "search":
        # Saml unikke genrer fra de valgte stemninger
        all_genres: list[str] = []
        for mood_id in state["selected_moods"]:
            for g in WATCH_MOODS[mood_id]["genres"]:
                if g not in all_genres:
                    all_genres.append(g)
        genre_str = ", ".join(all_genres)

        return (
            f"Find 4-5 gode {media_word} på serveren inden for genrerne: {genre_str}. "
            f"Brugeren har ikke set dem endnu. Anbefal med kort begrundelse for hver. "
            f"Svar i din persona — kort, præcis, venlig. Brug ✅-listeformat med "
            f"klikbare /info_movie_X eller /info_tv_X links."
        )

    if mode == "surprise":
        return (
            f"Find 4-5 tilfældige, fremragende skjulte perler blandt {media_word} på Plex, "
            f"som brugeren ikke har set endnu. Sælg dem godt med en kort, fængende "
            f"begrundelse. Brug ✅-listeformat med klikbare /info_movie_X eller "
            f"/info_tv_X links."
        )

    # mode == 'wildcard'
    return (
        "Find 4-5 tilfældige, fremragende skjulte perler på Plex (både film og serier "
        "er fint), som brugeren ikke har set endnu. Sælg dem godt med en kort, fængende "
        "begrundelse. Brug ✅-listeformat med klikbare /info_movie_X eller "
        "/info_tv_X links."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Markdown helpers
# ══════════════════════════════════════════════════════════════════════════════

def escape_markdown(text: str) -> str:
    """
    Escaper underscores inde i URL'er saa Telegrams Markdown-parser ikke
    misfortolker dem som kursiv-markoerer og kraesjer med 'Can't parse entities'.

    Kun tegn inde i URL'er beroeres - al anden Markdown-formatering
    (fed, kursiv, inline code osv.) i den omgivende tekst er uaendret.
    Backslash-escapingen sker via raw string replacement internt.
    """
    def _escape_url(match: re.Match) -> str:
        return match.group(1).replace("_", r"\_")

    return _URL_RE.sub(_escape_url, text)


# ══════════════════════════════════════════════════════════════════════════════
# Guards
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


# ══════════════════════════════════════════════════════════════════════════════
# Plex onboarding
# ══════════════════════════════════════════════════════════════════════════════

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
# Command handlers
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    clear_history(user.id)
    _clear_watch_state(context)
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
# 'Hvad skal jeg se?' — Trin 1: trigger fra fast knap
# ══════════════════════════════════════════════════════════════════════════════

async def handle_watch_flow_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Triggered når brugeren trykker på 🍿 Hvad skal jeg se?-knappen.
    Initialiser state og vis type-valget.
    """
    if not await _guard(update):
        return

    user = update.effective_user
    await database.log_message(user.id, "incoming", WATCH_FLOW_TRIGGER)

    if await _needs_plex_setup(update):
        return

    # Reset enhver tidligere watch flow state
    _clear_watch_state(context)

    await update.message.reply_text(
        "🍿 *Hvad skal jeg se?*\n\nVælg hvad du er i humør til:",
        parse_mode="Markdown",
        reply_markup=_build_type_selection_keyboard(),
    )
    logger.info("Watch flow startet for telegram_id=%s", user.id)


# ══════════════════════════════════════════════════════════════════════════════
# 'Hvad skal jeg se?' — Trin 2: type-valg (Film / Serie / Overrask)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_watch_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bruger valgte type i trin 2.
    'movie' / 'tv' → vis stemnings-vælgeren (trin 3, side 1).
    'surprise'     → spring stemninger over, brug 'wildcard' prompt straks.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    media_type = query.data.split(":", 1)[1]   # 'movie', 'tv' eller 'surprise'

    if media_type == "surprise":
        # Helt vild — ingen type, ingen stemning
        state = _get_watch_state(context)
        prompt = _build_watch_prompt(state, mode="wildcard")
        await _execute_ai_handoff(update, context, query, prompt)
        return

    # Init state for stemnings-flow
    state = _get_watch_state(context)
    state["media_type"]     = media_type
    state["selected_moods"] = []
    state["page"]           = 1

    try:
        await query.edit_message_text(
            text=_build_mood_message_text(state),
            parse_mode="Markdown",
            reply_markup=_build_mood_keyboard(state),
        )
    except Exception as e:
        logger.warning("handle_watch_type_callback edit fejl: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 'Hvad skal jeg se?' — Trin 3a: toggle stemning
# ══════════════════════════════════════════════════════════════════════════════

async def handle_watch_mood_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Toggle valgt stemning. Max MAX_MOODS valgte ad gangen.
    Hvis bruger trykker på en allerede valgt → fjern den.
    Hvis bruger trykker på en ny mens MAX_MOODS er nået → vis info-popup.
    """
    query = update.callback_query

    if not await _guard(update):
        await query.answer()
        return

    state = _get_watch_state(context)
    if not state.get("media_type"):
        await query.answer("Sessionen er udløbet — start forfra med 🍿-knappen.", show_alert=True)
        return

    try:
        mood_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer()
        return

    selected: list[int] = state["selected_moods"]

    if mood_id in selected:
        selected.remove(mood_id)
        await query.answer(f"Fjernet {WATCH_MOODS[mood_id]['name']}")
    elif len(selected) >= MAX_MOODS:
        await query.answer(
            f"Du kan kun vælge {MAX_MOODS} stemninger. Fravælg en først.",
            show_alert=True,
        )
        return
    else:
        selected.append(mood_id)
        await query.answer(f"Valgt {WATCH_MOODS[mood_id]['name']}")

    try:
        await query.edit_message_text(
            text=_build_mood_message_text(state),
            parse_mode="Markdown",
            reply_markup=_build_mood_keyboard(state),
        )
    except Exception as e:
        logger.warning("handle_watch_mood_callback edit fejl: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 'Hvad skal jeg se?' — Trin 3b: skift side
# ══════════════════════════════════════════════════════════════════════════════

async def handle_watch_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skift mellem side 1, 2 og 3 i stemnings-vælgeren."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    state = _get_watch_state(context)
    if not state.get("media_type"):
        await query.answer("Sessionen er udløbet — start forfra.", show_alert=True)
        return

    try:
        new_page = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return

    if new_page not in WATCH_PAGES:
        return

    state["page"] = new_page

    try:
        await query.edit_message_text(
            text=_build_mood_message_text(state),
            parse_mode="Markdown",
            reply_markup=_build_mood_keyboard(state),
        )
    except Exception as e:
        logger.warning("handle_watch_page_callback edit fejl: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 'Hvad skal jeg se?' — Trin 4: AI handoff
# ══════════════════════════════════════════════════════════════════════════════

async def handle_watch_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🚀 Søg nu! — byg prompt fra valgte stemninger og send til AI."""
    query = update.callback_query

    if not await _guard(update):
        await query.answer()
        return

    state = _get_watch_state(context)
    if not state.get("media_type") or not state.get("selected_moods"):
        await query.answer("Vælg mindst 1 stemning først.", show_alert=True)
        return

    await query.answer()
    prompt = _build_watch_prompt(state, mode="search")
    await _execute_ai_handoff(update, context, query, prompt)


async def handle_watch_surprise_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🎲 Overrask mig (efter type valgt) — tilfældige forslag inden for type."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    state = _get_watch_state(context)
    if not state.get("media_type"):
        # Hvis bruger på en eller anden måde rammer dette uden type → behandle som wildcard
        prompt = _build_watch_prompt(state, mode="wildcard")
    else:
        prompt = _build_watch_prompt(state, mode="surprise")

    await _execute_ai_handoff(update, context, query, prompt)


async def handle_watch_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """❌ Afbryd — ryd state og fjern keyboard."""
    query = update.callback_query
    await query.answer()

    _clear_watch_state(context)

    try:
        await query.edit_message_text("Aflyst. Tryk på 🍿-knappen igen når du vil have hjælp. 👍")
    except Exception as e:
        logger.warning("handle_watch_cancel_callback edit fejl: %s", e)


async def handle_watch_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔒 Vælg en stemning — disabled-knap, vis bare en hint."""
    query = update.callback_query
    await query.answer("Vælg mindst 1 stemning først 👇", show_alert=False)


async def _execute_ai_handoff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query,
    prompt: str,
) -> None:
    """
    Fælles handoff til AI. Genbruger samme path som handle_text() for at bevare
    signal-parsing til SHOW_INFO/TRAILER/SEARCH_RESULTS, så AI'ens svar kan
    udløse infokort, trailer-knapper og søgelister præcis som ved fritekst-input.
    """
    user        = update.effective_user
    chat        = query.message.chat
    state_snapshot = dict(_get_watch_state(context))   # kopi før clear
    _clear_watch_state(context)

    # Slet det inline keyboard ved at fjerne reply_markup på den gamle besked
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await chat.send_action("typing")

    loading_msg = await chat.send_message(
        "🤖 Beregner svar med lynets hast... næsten...",
    )

    plex_username = await database.get_plex_username(user.id)
    persona_id    = await database.get_persona(user.id)

    await database.log_message(user.id, "incoming", f"[watch_flow] {prompt[:100]}…")

    reply = await get_ai_response(
        telegram_id=user.id,
        user_message=prompt,
        plex_username=plex_username,
        persona_id=persona_id,
        user_first_name=user.first_name,
    )

    try:
        await loading_msg.delete()
    except Exception:
        pass

    # Kør samme signal-parsing som handle_text — så AI kan returnere SHOW_INFO etc.
    await _process_ai_reply(update, context, chat, user, plex_username, reply)


# ══════════════════════════════════════════════════════════════════════════════
# Inline Keyboard callbacks (bestillingsflow — uændret fra v0.9.8)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bruger valgte et søgeresultat — vis Netflix-look infokort."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    token = query.data.split(":", 1)[1]
    plex_username = await database.get_plex_username(query.from_user.id)
    await show_confirmation(query, context, token, plex_username)


async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bruger bekræftede bestilling — send til Radarr/Sonarr."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    token = query.data.split(":", 1)[1]
    plex_username = await database.get_plex_username(query.from_user.id)
    await execute_order(query, token, plex_username)


async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bruger annullerede — ryd op.

    BUG FIX (v0.9.8): edit_message_text() fejler på photo-beskeder (infokort
    med poster). Vi detekterer derfor besked-typen først:
      - Photo (infokort) → edit_message_caption()
      - Tekst (søgeliste) → edit_message_text()
    Robust fallback hvis edit fejler: slet original og send ny besked.
    """
    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1]
    if token != "none":
        await database.get_pending_request(token)  # sletter fra DB

    cancel_text = "Bestillingen blev annulleret. 👍"
    is_photo = bool(getattr(query.message, "photo", None))

    try:
        if is_photo:
            await query.edit_message_caption(caption=cancel_text)
        else:
            await query.edit_message_text(cancel_text)
    except Exception as e:
        logger.warning("handle_cancel_callback edit fejl: %s — sender ny besked", e)
        try:
            await query.message.delete()
        except Exception:
            pass
        try:
            await query.message.chat.send_message(cancel_text)
        except Exception as e2:
            logger.error("handle_cancel_callback fallback fejl: %s", e2)


async def handle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bruger trykkede ⬅️ Tilbage i søgeresultatlisten.
    Henter søgeterm og media_type fra pending_request og viser listen igen.
    title-feltet genbruges til at gemme søgetermen.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    token = query.data.split(":", 1)[1]
    pending = await database.get_pending_request(token)
    if not pending:
        await query.edit_message_text("Sessionen er udløbet — start forfra.")
        return

    search_query = pending["title"]       # title-feltet genbruges som søgeterm
    media_type   = pending["media_type"]

    # Slet den eksisterende besked og vis søgelisten på ny
    try:
        await query.message.delete()
    except Exception:
        pass

    await show_search_results(query.message, search_query, media_type)


# ══════════════════════════════════════════════════════════════════════════════
# /info_movie_<id> og /info_tv_<id> handler
# ══════════════════════════════════════════════════════════════════════════════

async def handle_info_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fanger /info_movie_<tmdb_id> og /info_tv_<tmdb_id> kommandoer.
    Regex er fleksibel: underscores er valgfrie for at fange Buddys fejlskrivninger.
    Logger altid den fulde kommando for at debugge ID-parring fejl.
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
            logger.warning("HANDLER: ingen match på '%s' — ignorerer", update.message.text)
            return

    media_type    = match.group(1)
    tmdb_id       = int(match.group(2))
    logger.info("Bruger trykkede på info-link: type=%s, id=%s", media_type, tmdb_id)

    user_id       = update.effective_user.id
    plex_username = await database.get_plex_username(user_id)

    await update.message.chat.send_action("typing")

    loading_msg = await update.message.reply_text(
        "🤖 Beregner svar med lynets hast... næsten...",
        reply_markup=ReplyKeyboardRemove(),
    )

    details = await get_media_details(tmdb_id, media_type)
    if not details:
        await loading_msg.delete()
        await update.message.reply_text("Kunne ikke hente info — prøv igen.")
        return

    title = details.get("title") or "Ukendt"
    year  = details.get("release_date", details.get("first_air_date", ""))[:4]

    import secrets as _sec
    token = _sec.token_hex(8)
    await database.save_pending_request(token, user_id, {
        "media_type": media_type,
        "tmdb_id":    tmdb_id,
        "title":      title,
        "year":       int(year) if year else None,
        "step":       "picked",
    })

    # Slet KUN brugerens kommando-besked — loading-beskeden lever til infokort er sendt
    try:
        await update.message.delete()
    except Exception:
        pass

    await show_confirmation(update.message, context, token, plex_username,
                            loading_msg=loading_msg)


# ══════════════════════════════════════════════════════════════════════════════
# Message handler (fritekst)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    user = update.effective_user
    text = (update.message.text or "").strip()

    # ── Watch flow trigger ────────────────────────────────────────────────────
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

    # ── Session timeout: kør /start automatisk efter 10 min inaktivitet ──────
    if check_session_timeout(user.id):
        await cmd_start(update, context)
        return

    await update.message.chat.send_action("typing")
    plex_username = await database.get_plex_username(user.id)
    persona_id    = await database.get_persona(user.id)

    # Send loading-besked og slet den når svaret er klar
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

    # Slet loading-beskeden
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
    """
    Fælles signal-parsing for AI-svar. Bruges af både handle_text() (fritekst)
    og _execute_ai_handoff() (watch flow).

    Detekterer SHOW_INFO, SHOW_TRAILER, SEARCH_RESULTS signaler og udløser
    den korrekte handler (infokort, trailer-knap eller søgeliste).
    """
    # Rens backticks fra signaler — Buddy pakker dem nogle gange ind i Markdown
    clean_reply = reply.replace("`", "").strip()

    def _find_signal(signal: str) -> str | None:
        """
        Scan alle linjer i clean_reply for signalet og returner linjen.
        Buddy placerer nogle gange signalet på linje 2 efter en tekstlinje
        — startswith() på hele svaret fanger ikke dette. Vi scanner linje
        for linje og returnerer den første linje der starter med signalet.
        """
        for line in clean_reply.splitlines():
            line = line.strip()
            if line.startswith(signal):
                return line
        return None

    # ── Signal: bestillingsflow ───────────────────────────────────────────────
    signal_line = _find_signal(SEARCH_SIGNAL)
    if signal_line:
        parts = signal_line[len(SEARCH_SIGNAL):].split(":", 1)
        query_term = parts[0].strip()
        media_type = parts[1].strip() if len(parts) > 1 else "both"
        # show_search_results forventer en 'message' med reply_text/chat_id
        target_message = update.message if update.message else None
        if target_message is None:
            # Watch flow path — chat er sat, men ingen message. Lav en besked at svare på.
            target_message = await chat.send_message("…")
        await show_search_results(target_message, query_term, media_type)
        return

    # ── Signal: Netflix-look infokort ─────────────────────────────────────────
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
                # show_confirmation kan kaldes med både Message og CallbackQuery —
                # vi bruger en sendt besked som trigger.
                trigger_msg = update.message if update.message else await chat.send_message("…")
                await show_confirmation(trigger_msg, context, token, plex_username)
                return
            except Exception as e:
                logger.error("Fejl ved håndtering af SHOW_INFO: %s", e)
        else:
            logger.warning("SHOW_INFO signal kunne ikke parses: %r", reply)

    # ── Signal: trailer-knap ──────────────────────────────────────────────────
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

    # ── Normalt svar — brug det originale reply med Markdown intakt ───────────
    safe_reply = escape_markdown(reply)
    await chat.send_message(safe_reply, parse_mode="Markdown")
    await database.log_message(user.id, "outgoing", reply)


# ══════════════════════════════════════════════════════════════════════════════
# Webhook HTTP server
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
# Global error handler
# ══════════════════════════════════════════════════════════════════════════════

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all handler for uventede Telegram-fejl.
    Logger fuld traceback og sender en venlig besked til brugeren hvis muligt.
    """
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
    await _start_webhook_server()
    if not config.WEBHOOK_SECRET:
        logger.warning(
            "WEBHOOK_SECRET er ikke sat — webhooks accepteres uden token-tjek! "
            "Sæt WEBHOOK_SECRET i Railway for at sikre endpointene."
        )
    logger.info("Buddy started in '%s' environment.", config.ENVIRONMENT)
    logger.info(
        "VERSION CHECK — v0.10.0-beta | "
        "søgeresultater-UX: JA | foto-fix: JA | årstal-fallback: JA | "
        "tilbage-knap: JA | already-anmodet-check: JA | user_first_name: JA | "
        "annuller-photo-fix: JA | watch-flow: JA"
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

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("skift_plex", cmd_skift_plex))

    # Admin approval
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve_user:\d+$"))

    # 'Hvad skal jeg se?' flow — REGISTRERES FØR bestillingsflow så watch_*
    # callbacks ikke fanges af andre handlers. Hvert handler har sit eget pattern.
    app.add_handler(CallbackQueryHandler(handle_watch_type_callback,     pattern=r"^watch_type:"))
    app.add_handler(CallbackQueryHandler(handle_watch_mood_callback,     pattern=r"^watch_mood:"))
    app.add_handler(CallbackQueryHandler(handle_watch_page_callback,     pattern=r"^watch_page:"))
    app.add_handler(CallbackQueryHandler(handle_watch_search_callback,   pattern=r"^watch_search$"))
    app.add_handler(CallbackQueryHandler(handle_watch_surprise_callback, pattern=r"^watch_surprise$"))
    app.add_handler(CallbackQueryHandler(handle_watch_cancel_callback,   pattern=r"^watch_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_watch_noop_callback,     pattern=r"^watch_noop$"))

    # Bestillingsflow
    app.add_handler(CallbackQueryHandler(handle_pick_callback,      pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(handle_confirm_callback,   pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_callback,    pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(handle_watchlist_callback, pattern=r"^watchlist:"))
    app.add_handler(CallbackQueryHandler(handle_back_callback,      pattern=r"^back:"))

    # Info-links fra lister — fleksibelt regex fanger både /info_movie_123 og /infomovie123
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