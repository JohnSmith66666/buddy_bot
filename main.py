"""
main.py - Buddy bot entry point.

CHANGES (v0.16.4 — UX polish for mobil-skaerm + info-link bugfix):
  - Loading-besked reduceret fra "🤖 Beregner svar med lynets hast..." til
    kun emoji 🔍 — fylder ~30px paa mobil i stedet for ~80px.
  - Genindfoert send_action("typing") fra v0.16.3 — det gav brugerne
    fornemmelse af at "noget sker" som de specifikt efterspurgte.
  - Reply keyboard (🍿 + 💬 knapper) bevares — fjernes IKKE midlertidigt.
  - Aendringer i AI-chat (handle_text) OG info-link (handle_info_link).
  - BUGFIX: handle_info_link fjernede tidligere tastaturet via
    ReplyKeyboardRemove() naar bruger trykkede paa /info_movie_<id>,
    men gendannede det aldrig. Knapper forsvandt efter info-kort.
    Nu fjernes ReplyKeyboardRemove() — tastaturet forbliver synligt.
  - Watch flow, feedback flow m.fl. er uaendret.

CHANGES (v0.16.3 — Performance polish, AI hot path):
  - PERFORMANCE: handle_text() laver nu plex_username + persona_id i PARALLEL
    via asyncio.gather i stedet for sekventielt. Sparer ~30ms latency
    på hver AI-besked.
  - PERFORMANCE: Fjernet overflødigt 'send_action("typing")'-kald i
    handle_text(). Loading-spinneren ('🤖 Beregner svar...') viser allerede
    tydeligt at noget sker. Sparer ~100ms latency.
  - TOTAL GEVINST: ~130ms hurtigere response-tid på hver AI-besked.
  - INGEN ANDRE ÆNDRINGER. Funktionel adfærd er 100% identisk.

CHANGES (v0.16.2 — Polish session, fundet i kode-review):
  - FIX (Bug A): handle_admin_hint_callback har nu admin-guard for konsistens
    med andre admin-callbacks (handle_admin_resolve_callback, handle_admin_seen_callback).
  - FIX (Bug B): handle_feedback_reply_to_callback havde dobbelt query.answer()
    der gav silent error i log. Validering er flyttet FØR første answer.
  - FIX (Bug C): notify_admin_about_feedback oprettede ny telegram.Bot instans
    PER notifikation (performance issue). Nu bruger vi _get_admin_bot_client()
    der lazy-initaliserer og cacher én instans for hele bot-levetiden.
  - FIX (Bug D): handle_admin_resolve/seen_callback fejlede edit på photo-
    notifikationer. Tilføjet fallback til edit_message_caption for photo-beskeder.
  - CLEANUP: Fjernet 5 unused imports (FEEDBACK_TYPES, SUBGENRES_MOVIE,
    SUBGENRES_TV, detect_media_type, get_media_details).
  - CLEANUP: Fjernet 4 lokale 'import time' statements — nu importeret én gang
    i toppen af filen.
  - CLEANUP: Fjernet 'import os as _os' alias — bruger 'os' direkte.

CHANGES (v0.16.1 — Notifikationer flyttet til Admin-bot):
  - NY: notify_admin_about_feedback() bruger nu ADMIN_BOT_TOKEN (hvis sat)
    til at sende admin-notifikationen. Det betyder admin modtager beskeden
    i sin Buddy_admin-chat — ikke i Buddy_beta-chatten.
    Det matcher den klare rolleopdeling:
      • Buddy = bruger-vendt, ren oplevelse uden admin-noise
      • Admin = systembeskeder, notifikationer, admin-tools
  - SIKKER FALLBACK: Hvis ADMIN_BOT_TOKEN env-var IKKE er sat, falder vi
    tilbage til at sende notifikation via Buddy main (gammel adfærd).
    Ingen notifikationer går tabt under migration.
  - VIGTIGT: Screenshots sendes STADIG via Buddy main bot fordi file_ids
    er fra Buddy main's bot-domæne — admin-bot kan ikke sende dem direkte.

CHANGES (v0.16.0 — Batch B nye flows):
  - NY: Auto-timeout på feedback-state. Hvis bruger har været i
    'awaiting_feedback' state i over 30 minutter uden at sende eller
    afbryde, ryddes state automatisk ved næste interaktion.
  - NY: /cancel kommando — virker som escape hatch fra feedback-state.
    Brugeren kan altid skrive /cancel for at komme ud af feedback-flow.
  - NY: Preview-trin før send (Option Y). Når brugeren trykker '✅ Send',
    vises en preview-besked med deres samlede feedback. De kan så vælge:
    [✅ Bekræft og Send] / [✏️ Rediger (skriv mere)] / [❌ Afbryd]
  - NY: Inline-knapper på admin-notifikation (Option C — hybrid):
      [💬 Svar i admin-bot] → URL-deep-link til admin-bot med /reply <id>
      [✅ Resolve]            → callback i Buddy main, opdaterer DB direkte
      [👁 Set]                → callback i Buddy main, opdaterer DB direkte
  - NY: Inline svar-knap til bruger på admin-svar besked.
    [💬 Svar tilbage] åbner feedback-flow med pre-udfyldt "Re: \\#<id> "
    så svaret oprettes som ny feedback med klar reference.
  - REFACTOR: Submit-flow har nu 2 trin internt:
      compose → preview → submit
    Tidligere: compose → submit. Det giver brugeren mulighed for at
    se hele beskeden før den sendes.

UNCHANGED (v0.15.0 — Batch A polering):
  - NY: Loading-spinner ved feedback-submit. Når brugeren trykker '✅ Send'
    vises straks "Sender din feedback... 🚀" — derefter erstattes beskeden
    med tak/fejl. Forbedrer responsiv-følelsen markant for små momenter.
  - NY: First-time tester detection. notify_admin_about_feedback() tjekker
    via database.is_first_time_feedback() OM brugeren har sendt feedback
    før. Hvis IKKE → admin-notifikationen får et tydeligt
    "🆕 NY TESTER — første feedback nogensinde!" badge øverst.
  - INGEN ANDRE ÆNDRINGER: Watch flow, AI-handler, alle admin-kommandoer
    og bestillingsflow er uberørt.

UNCHANGED (v0.14.0 — Feedback system):
  - NY KNAP: '💬 Feedback' tilføjet i bundmenuen ved siden af '🍿 Hvad skal jeg se?'.
    Knappen vises KUN efter Plex-onboarding er færdig (jvf. brugerens valg).
    _build_main_reply_keyboard() har nu 2 knapper i 2 rækker.
  - NY: setup_feedback_table() kaldes ved startup.
  - NY: Komplet feedback-flow med 4 kategorier (idea/bug/question/praise):
      Bruger trykker '💬 Feedback'
        → Trin 1: Vælg kategori (4 inline-knapper)
        → Trin 2: Skriv besked (tekst + valgfri ubegrænsede screenshots)
        → Trin 3: Tryk '✅ Send' for at indsende
        → Tak-besked + reference-ID
        → Admin får notifikation via Buddy-bot direkte
  - NY: Onboarding-state 'awaiting_feedback' for tekst-input fasen.
  - NY: context.user_data bruges til at holde feedback-draft (kategori,
    tekst, screenshot file_ids) MENS brugeren skriver. State på telegram_id
    nulstilles efter Send eller Afbryd. Pragmatisk valg fordi feedback-flow
    er kort-livet (sekunder/minutter) — ikke kritisk hvis Railway restarter.
  - NY: Photo-handler — når bruger er i 'awaiting_feedback', opfanges photos
    og file_id gemmes til feedback-recorden.
  - NY: notify_admin_about_feedback() sender Markdown-besked til admin
    direkte via context.bot (samme mønster som admin_handlers.notify_admin_new_user).
    Inkluderer screenshots hvis vedhæftet.

UNCHANGED (v0.13.0 — media-aware Watch Flow):
  - Media-valg trin (Film/Serie/Overrask).
  - Alle subgenre callbacks bærer media_type.
  - 4 navigations-niveauer i Trin 5.

UNCHANGED (v0.12.0 — audit værktøj cmd_audit_tv_subgenres).
UNCHANGED (v0.12.2 — brugerguide-link i cmd_start).
UNCHANGED (v0.12.1 — fix #1 double-fetch).
UNCHANGED (v0.10.x — alle admin-kommandoer).
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
import time
import traceback
from aiohttp import web
from collections import Counter
from datetime import datetime
from itertools import combinations

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
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
from services.feedback_service import (
    format_admin_notification,
    format_feedback_preview,
    format_user_thanks,
    get_feedback_type,
    list_feedback_type_ids,
    validate_feedback_type,
)
from services.plex_service import validate_plex_user
from services.tmdb_keywords_service import (
    fetch_metadata_batch,
    fetch_movie_metadata,
    fetch_tv_metadata,
    search_tmdb_by_title,
)
from services.subgenre_service import (
    SUBGENRE_CATEGORIES_MOVIE,
    SUBGENRE_CATEGORIES_TV,
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
# WATCH FLOW (v0.13.0) — Konstanter og helpers
# ══════════════════════════════════════════════════════════════════════════════

# Bundknap-tekst (uændret for at bevare brugervante)
WATCH_FLOW_TRIGGER = "🍿 Hvad skal jeg se?"
FEEDBACK_TRIGGER   = "💬 Feedback"

# Header-tekster i de forskellige trin
TRIN2_HEADER = (
    "🍿 *Hvad er du i humør til?*\n\n"
    "Vælg om du vil se en film, en serie — eller om jeg skal overraske dig 🎲"
)

TRIN3_HEADER_MOVIE = "🎬 *Find en film*\n\nVælg en stemning 👇"
TRIN3_HEADER_TV    = "📺 *Find en serie*\n\nVælg en stemning 👇"

TRIN4_HEADER_TPL = "{label}\n\nVælg en undergenre 👇"


# ══════════════════════════════════════════════════════════════════════════════
# FEEDBACK FLOW (v0.14.0) — Konstanter
# ══════════════════════════════════════════════════════════════════════════════

# Onboarding-state navn for feedback-input fasen
FEEDBACK_STATE = "awaiting_feedback"

# Max længde på besked-tekst (Telegram tillader 4096 i én besked)
FEEDBACK_MAX_LENGTH = 4000

# v0.16.0 — Batch B
# Auto-timeout for feedback-state (sekunder). Efter dette ryddes state
# automatisk så brugeren ikke sidder fast.
FEEDBACK_STATE_TIMEOUT_SECONDS = 30 * 60  # 30 minutter

# Username på admin-bot — bruges til deep-link fra Buddy's notifikation.
# Hvis None eller tom, fallback'er knappen til at vise et hint i stedet.
# Sættes via env-var ADMIN_BOT_USERNAME (uden @-prefix).
ADMIN_BOT_USERNAME: str = (os.getenv("ADMIN_BOT_USERNAME") or "").strip().lstrip("@")

# v0.16.1 — Admin-bot's token bruges til at sende notifikationer FRA admin-bot's
# kanal (i stedet for Buddy main-kanalen). Dermed adskilles roller:
#   • Buddy = bruger-vendt, ren oplevelse uden admin-noise
#   • Admin = systembeskeder, notifikationer, admin-tools
# Hvis env-varen ikke er sat, falder vi tilbage til at sende notifikationen
# via Buddy main (gammel adfærd).
ADMIN_BOT_TOKEN: str = (os.getenv("ADMIN_BOT_TOKEN") or "").strip()

# v0.16.2 — Lazy-initialized admin-bot client. Genbruges på tværs af alle
# notifikationer for at undgå at oprette ny HTTP-pool per kald (performance).
_admin_bot_client = None


def _get_admin_bot_client():
    """
    Lazy-init af admin-bot Bot-instans (v0.16.2 performance-fix).

    Returnerer cached instans hvis allerede oprettet — ellers opretter
    ny én gang og cacher den. Bruges af notify_admin_about_feedback().

    Returns:
      telegram.Bot instans, eller None hvis ADMIN_BOT_TOKEN ikke er sat.
    """
    global _admin_bot_client
    if not ADMIN_BOT_TOKEN:
        return None
    if _admin_bot_client is None:
        try:
            from telegram import Bot as _AdminBot
            _admin_bot_client = _AdminBot(token=ADMIN_BOT_TOKEN)
            logger.info("Admin-bot client lazy-initialiseret")
        except Exception as e:
            logger.warning("Kunne ikke initialisere admin-bot client: %s", e)
            return None
    return _admin_bot_client


def _media_label(media_type: str) -> str:
    """Dansk label for media_type — bruges i headers."""
    return "film" if media_type == "movie" else "serie"


def _media_emoji(media_type: str) -> str:
    """Emoji for media_type."""
    return "🎬" if media_type == "movie" else "📺"


def _build_main_reply_keyboard(include_feedback: bool = True) -> ReplyKeyboardMarkup:
    """
    Persistent bundknap — vises i bunden af chatten.

    v0.14.0: Tilføjet '💬 Feedback'-knap som anden række.
    include_feedback=False bruges hvis vi vil skjule den (fx før onboarding).
    """
    rows = [
        [KeyboardButton(WATCH_FLOW_TRIGGER)],
    ]
    if include_feedback:
        rows.append([KeyboardButton(FEEDBACK_TRIGGER)])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Watch Flow keyboards — bygges fra subgenre_service data
# ══════════════════════════════════════════════════════════════════════════════

def _build_media_keyboard() -> InlineKeyboardMarkup:
    """
    Trin 2: Vælg media-type.

    Callback-data:
      sg_media:movie   — gå til film-kategorier
      sg_media:tv      — gå til TV-kategorier
      sg_media:random  — random media + random subgenre → direkte til Trin 5
      sg_cancel        — afbryd
    """
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🎬 Film",       callback_data="sg_media:movie")],
        [InlineKeyboardButton("📺 Serie",      callback_data="sg_media:tv")],
        [InlineKeyboardButton("🎲 Overrask mig", callback_data="sg_media:random")],
        [InlineKeyboardButton("❌ Afbryd",     callback_data="sg_cancel")],
    ]
    return InlineKeyboardMarkup(rows)


def _build_categories_keyboard(media_type: str) -> InlineKeyboardMarkup:
    """
    Trin 3: 9 hovedkategorier for valgt media_type (1 knap pr. række)
    + Overrask + Tilbage + Afbryd.
    Callback-data: 'sg_cat:<media>:<category_id>' eller 'sg_cat:<media>:random'
    """
    rows: list[list[InlineKeyboardButton]] = []

    for cat in get_all_categories(media_type=media_type):
        rows.append([
            InlineKeyboardButton(
                cat["label"],
                callback_data=f"sg_cat:{media_type}:{cat['id']}",
            )
        ])

    # Bundrække: Overrask + Tilbage + Afbryd
    rows.append([
        InlineKeyboardButton(
            "🎲 Overrask mig",
            callback_data=f"sg_cat:{media_type}:random",
        ),
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Tilbage", callback_data="sg_back:media"),
        InlineKeyboardButton("❌ Afbryd",   callback_data="sg_cancel"),
    ])

    return InlineKeyboardMarkup(rows)


def _build_subgenres_keyboard(media_type: str, category_id: str) -> InlineKeyboardMarkup:
    """
    Trin 4: 3-6 subgenrer i denne kategori (1 knap pr. række)
    + Overrask (random subgenre i samme kasse) + Tilbage + Afbryd.
    """
    cat = get_category(category_id, media_type=media_type)
    rows: list[list[InlineKeyboardButton]] = []

    if cat:
        for sub in cat["subgenres"]:
            rows.append([
                InlineKeyboardButton(
                    sub["label"],
                    callback_data=f"sg_pick:{media_type}:{sub['id']}",
                )
            ])

    # Bundrække
    rows.append([
        InlineKeyboardButton(
            "🎲 Overrask mig (i denne kategori)",
            callback_data=f"sg_random:{media_type}:{category_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ Tilbage",
            callback_data=f"sg_back:cats:{media_type}",
        ),
        InlineKeyboardButton("❌ Afbryd", callback_data="sg_cancel"),
    ])

    return InlineKeyboardMarkup(rows)


def _build_results_keyboard(
    media_type: str,
    subgenre_id: str,
    has_results: bool,
) -> InlineKeyboardMarkup:
    """
    Trin 5: Actions efter resultater — 4 navigations-niveauer.
    """
    cat_id = get_category_for_subgenre(subgenre_id, media_type=media_type)
    rows: list[list[InlineKeyboardButton]] = []

    if has_results:
        rows.append([
            InlineKeyboardButton(
                "🔄 5 nye forslag",
                callback_data=f"sg_refresh:{media_type}:{subgenre_id}",
            ),
        ])

    if cat_id:
        rows.append([
            InlineKeyboardButton(
                "⏭️ Næste subgenre",
                callback_data=f"sg_next:{media_type}:{cat_id}",
            ),
            InlineKeyboardButton(
                "⬅️ Subgenrer",
                callback_data=f"sg_back:subs:{media_type}:{cat_id}",
            ),
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                "⬅️ Tilbage til kategorier",
                callback_data=f"sg_back:cats:{media_type}",
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️⬅️ Kategorier",
            callback_data=f"sg_back:cats:{media_type}",
        ),
        InlineKeyboardButton(
            "🏠 Forfra",
            callback_data="sg_back:media",
        ),
    ])

    rows.append([
        InlineKeyboardButton("❌ Færdig", callback_data="sg_cancel"),
    ])

    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Result formatting — fra find_unwatched_v2 dict til Markdown-besked
# ══════════════════════════════════════════════════════════════════════════════

def _format_results_message(result: dict, media_type: str) -> tuple[str, bool]:
    """Format find_unwatched_v2 resultat som Markdown-besked."""
    status         = result.get("status")
    subgenre_label = result.get("subgenre_label", "?")
    stats          = result.get("stats", {}) or {}
    unwatched      = stats.get("unwatched", 0)
    media_word     = _media_label(media_type)
    media_word_pl  = "film" if media_type == "movie" else "serier"

    if status == "error":
        return (
            f"❌ Hov, noget gik galt!\n\n"
            f"`{result.get('message', 'Ukendt fejl')}`\n\n"
            f"Prøv en anden subgenre.",
            False,
        )

    if status == "missing":
        if stats.get("in_plex", 0) == 0:
            return (
                f"😕 Hmm, jeg kunne ikke finde nogen {media_word_pl} der matcher "
                f"*{subgenre_label}* i dit bibliotek.\n\n"
                f"_Prøv en anden subgenre._",
                False,
            )
        return (
            f"🎉 *Du har set ALT i {subgenre_label} — godt gået!*\n\n"
            f"_Prøv en anden subgenre._",
            False,
        )

    results = result.get("results", []) or []
    if not results:
        return (
            f"🎉 *Du har set ALT i {subgenre_label} — godt gået!*\n\n"
            f"_Prøv en anden subgenre._",
            False,
        )

    lines = [f"*{subgenre_label}*", ""]

    if len(results) < 5:
        lines.append(
            f"⚠️ _Du har set det meste — kun {len(results)} forslag her_"
        )
        lines.append("")

    for item in results:
        title   = item.get("title") or "Ukendt"
        year    = item.get("year")
        rating  = item.get("rating")
        tmdb_id = item.get("tmdb_id")

        year_str   = f" ({year})" if year else ""
        rating_str = f" ⭐ {rating:.1f}" if rating else ""
        info_link  = (
            f"\n   /info_{media_type}_{tmdb_id}" if tmdb_id else ""
        )

        lines.append(f"🟢 *{title}*{year_str}{rating_str}{info_link}")
        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()

    lines.append("")
    lines.append(f"_{unwatched} usete {media_word_pl} i denne kategori_")

    return ("\n".join(lines), True)


# ══════════════════════════════════════════════════════════════════════════════
# Watch Flow trigger + handlers
# ══════════════════════════════════════════════════════════════════════════════

async def handle_watch_flow_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trin 1 → Trin 2: Bruger trykkede '🍿 Hvad skal jeg se?'"""
    if not await _guard(update):
        return

    user = update.effective_user
    await database.log_message(user.id, "incoming", WATCH_FLOW_TRIGGER)

    if await _needs_plex_setup(update):
        return

    await update.message.reply_text(
        TRIN2_HEADER,
        parse_mode="Markdown",
        reply_markup=_build_media_keyboard(),
    )
    logger.info("Watch flow startet for telegram_id=%s", user.id)


async def handle_subgenre_media_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Trin 2 → Trin 3: Bruger valgte media-type."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    payload = query.data.split(":", 1)[1]

    if payload == "random":
        media_type = random.choice(["movie", "tv"])
        all_ids    = list_subgenre_ids(media_type=media_type)
        if not all_ids:
            await query.answer("Hov, ingen subgenrer er konfigureret", show_alert=True)
            return
        subgenre_id = random.choice(all_ids)
        logger.info(
            "Watch flow: top-level random → media=%s, subgenre=%s",
            media_type, subgenre_id,
        )
        await _execute_subgenre_search(
            update, context, media_type, subgenre_id, edit_message=True,
        )
        return

    if payload not in ("movie", "tv"):
        logger.warning("handle_subgenre_media_callback: ukendt payload='%s'", payload)
        await query.answer("Ugyldigt valg", show_alert=True)
        return

    media_type = payload
    header = TRIN3_HEADER_MOVIE if media_type == "movie" else TRIN3_HEADER_TV

    try:
        await query.edit_message_text(
            text=header,
            parse_mode="Markdown",
            reply_markup=_build_categories_keyboard(media_type),
        )
    except Exception as e:
        logger.warning("handle_subgenre_media_callback edit fejl: %s", e)


async def handle_subgenre_category_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Trin 3 → Trin 4: Bruger valgte en kategori."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        logger.warning("handle_subgenre_category_callback: malformed data='%s'", query.data)
        await query.answer("Ugyldigt valg", show_alert=True)
        return

    _, media_type, payload = parts

    if media_type not in ("movie", "tv"):
        await query.answer("Ugyldig media-type", show_alert=True)
        return

    if payload == "random":
        cats_dict = (
            SUBGENRE_CATEGORIES_MOVIE if media_type == "movie"
            else SUBGENRE_CATEGORIES_TV
        )
        category_id = random.choice(list(cats_dict.keys()))
        logger.info(
            "Watch flow: random kategori (media=%s) → '%s'",
            media_type, category_id,
        )
    else:
        category_id = payload

    cat = get_category(category_id, media_type=media_type)
    if cat is None:
        logger.warning(
            "handle_subgenre_category_callback: ukendt category_id='%s' (media=%s)",
            category_id, media_type,
        )
        await query.answer("Den kategori findes ikke", show_alert=True)
        return

    try:
        await query.edit_message_text(
            text=TRIN4_HEADER_TPL.format(label=f"*{cat['label']}*"),
            parse_mode="Markdown",
            reply_markup=_build_subgenres_keyboard(media_type, category_id),
        )
    except Exception as e:
        logger.warning("handle_subgenre_category_callback edit fejl: %s", e)


async def handle_subgenre_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Trin 4 → Trin 5: Bruger valgte en specifik subgenre."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Ugyldigt valg", show_alert=True)
        return

    _, media_type, subgenre_id = parts

    if not validate_subgenre_id(subgenre_id, media_type=media_type):
        logger.warning(
            "handle_subgenre_pick_callback: ukendt subgenre_id='%s' (media=%s)",
            subgenre_id, media_type,
        )
        await query.answer("Den subgenre findes ikke", show_alert=True)
        return

    await _execute_subgenre_search(
        update, context, media_type, subgenre_id, edit_message=True,
    )


async def handle_subgenre_random_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Trin 4 special: Bruger trykkede '🎲 Overrask mig (i denne kategori)'."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Ugyldigt valg", show_alert=True)
        return

    _, media_type, category_id = parts
    cat = get_category(category_id, media_type=media_type)
    if cat is None or not cat["subgenres"]:
        await query.answer("Den kategori er tom", show_alert=True)
        return

    subgenre_id = random.choice([s["id"] for s in cat["subgenres"]])
    logger.info(
        "Watch flow: random subgenre i kategori '%s' (media=%s) → '%s'",
        category_id, media_type, subgenre_id,
    )

    await _execute_subgenre_search(
        update, context, media_type, subgenre_id, edit_message=True,
    )


async def handle_subgenre_refresh_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Trin 5 action: '🔄 5 nye forslag'."""
    query = update.callback_query
    await query.answer("🔄 Henter nye forslag...")

    if not await _guard(update):
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Ugyldigt valg", show_alert=True)
        return

    _, media_type, subgenre_id = parts

    if not validate_subgenre_id(subgenre_id, media_type=media_type):
        await query.answer("Den subgenre findes ikke", show_alert=True)
        return

    await _execute_subgenre_search(
        update, context, media_type, subgenre_id, edit_message=True,
    )


async def handle_subgenre_next_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Trin 5 action: '⏭️ Næste subgenre'."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Ugyldigt valg", show_alert=True)
        return

    _, media_type, category_id = parts
    cat = get_category(category_id, media_type=media_type)

    if cat is None:
        header = TRIN3_HEADER_MOVIE if media_type == "movie" else TRIN3_HEADER_TV
        await query.edit_message_text(
            text=header,
            parse_mode="Markdown",
            reply_markup=_build_categories_keyboard(media_type),
        )
        return

    if len(cat["subgenres"]) < 2:
        header = TRIN3_HEADER_MOVIE if media_type == "movie" else TRIN3_HEADER_TV
        await query.edit_message_text(
            text=header,
            parse_mode="Markdown",
            reply_markup=_build_categories_keyboard(media_type),
        )
        return

    try:
        await query.edit_message_text(
            text=TRIN4_HEADER_TPL.format(label=f"*{cat['label']}*"),
            parse_mode="Markdown",
            reply_markup=_build_subgenres_keyboard(media_type, category_id),
        )
    except Exception as e:
        logger.warning("handle_subgenre_next_callback edit fejl: %s", e)


async def handle_subgenre_back_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Tilbage-navigation."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    parts = query.data.split(":")
    if len(parts) < 2:
        await query.answer("Ugyldigt valg", show_alert=True)
        return

    direction = parts[1]

    if direction == "media":
        try:
            await query.edit_message_text(
                text=TRIN2_HEADER,
                parse_mode="Markdown",
                reply_markup=_build_media_keyboard(),
            )
        except Exception as e:
            logger.warning("handle_subgenre_back_callback (media) fejl: %s", e)
        return

    if direction == "cats":
        if len(parts) < 3:
            await query.answer("Ugyldigt valg", show_alert=True)
            return
        media_type = parts[2]
        if media_type not in ("movie", "tv"):
            await query.answer("Ugyldig media-type", show_alert=True)
            return
        header = TRIN3_HEADER_MOVIE if media_type == "movie" else TRIN3_HEADER_TV
        try:
            await query.edit_message_text(
                text=header,
                parse_mode="Markdown",
                reply_markup=_build_categories_keyboard(media_type),
            )
        except Exception as e:
            logger.warning("handle_subgenre_back_callback (cats) fejl: %s", e)
        return

    if direction == "subs":
        if len(parts) < 4:
            await query.answer("Ugyldigt valg", show_alert=True)
            return
        media_type  = parts[2]
        category_id = parts[3]
        if media_type not in ("movie", "tv"):
            await query.answer("Ugyldig media-type", show_alert=True)
            return
        cat = get_category(category_id, media_type=media_type)
        if cat is None:
            header = TRIN3_HEADER_MOVIE if media_type == "movie" else TRIN3_HEADER_TV
            await query.edit_message_text(
                text=header,
                parse_mode="Markdown",
                reply_markup=_build_categories_keyboard(media_type),
            )
            return
        try:
            await query.edit_message_text(
                text=TRIN4_HEADER_TPL.format(label=f"*{cat['label']}*"),
                parse_mode="Markdown",
                reply_markup=_build_subgenres_keyboard(media_type, category_id),
            )
        except Exception as e:
            logger.warning("handle_subgenre_back_callback (subs) fejl: %s", e)
        return

    logger.warning("handle_subgenre_back_callback: ukendt direction='%s'", direction)


async def handle_subgenre_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """'❌ Afbryd' eller '❌ Færdig' — luk flowet."""
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
    media_type: str,
    subgenre_id: str,
    edit_message: bool = True,
) -> None:
    """Fælles logik for alle veje der ender i et v2-kald."""
    query = update.callback_query
    user  = query.from_user
    chat  = query.message.chat

    sub = get_subgenre(subgenre_id, media_type=media_type)
    sub_label = sub["label"] if sub else subgenre_id

    try:
        await query.edit_message_text(
            f"{sub_label}\n\n🤖 Beregner svar med lynets hast... næsten...",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    plex_username = await database.get_plex_username(user.id)

    try:
        result = await find_unwatched_v2(
            subgenre_id=subgenre_id,
            plex_username=plex_username,
            limit=5,
        )
    except Exception as e:
        logger.error(
            "_execute_subgenre_search fejl for '%s' (media=%s): %s",
            subgenre_id, media_type, e,
        )
        try:
            await query.edit_message_text(
                f"❌ Hov, noget gik galt: `{e}`\n\nPrøv en anden subgenre.",
                parse_mode="Markdown",
                reply_markup=_build_results_keyboard(
                    media_type, subgenre_id, has_results=False,
                ),
            )
        except Exception:
            pass
        return

    message_text, has_results = _format_results_message(result, media_type)
    keyboard = _build_results_keyboard(
        media_type, subgenre_id, has_results=has_results,
    )

    await database.log_message(
        user.id,
        "incoming",
        f"[watch_flow] media={media_type} subgenre={subgenre_id}",
    )
    stats = result.get("stats", {}) or {}
    await database.log_message(
        user.id,
        "outgoing",
        f"[watch_flow] {media_type}/{subgenre_id} → "
        f"{stats.get('returned', 0)} forslag (af {stats.get('unwatched', 0)} usete)",
    )

    try:
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
# FEEDBACK FLOW (v0.14.0) — NY
# ══════════════════════════════════════════════════════════════════════════════

def _build_feedback_category_keyboard() -> InlineKeyboardMarkup:
    """
    Trin 1: Vælg feedback-kategori.

    Layout: 2x2 grid med 4 kategorier + Afbryd nederst.
    Callback-data: 'fb_type:<type_id>' eller 'fb_cancel'
    """
    rows: list[list[InlineKeyboardButton]] = []

    # 2x2 grid (idea+bug, question+praise)
    type_ids = list_feedback_type_ids()
    for i in range(0, len(type_ids), 2):
        row = []
        for tid in type_ids[i:i+2]:
            ft = get_feedback_type(tid)
            if ft:
                row.append(InlineKeyboardButton(
                    ft["label"],
                    callback_data=f"fb_type:{tid}",
                ))
        if row:
            rows.append(row)

    rows.append([
        InlineKeyboardButton("❌ Afbryd", callback_data="fb_cancel"),
    ])

    return InlineKeyboardMarkup(rows)


def _build_feedback_compose_keyboard() -> InlineKeyboardMarkup:
    """
    Trin 2: Mens brugeren skriver — '👀 Forhåndsvis' og '❌ Afbryd' knapper.

    v0.16.0: Knappen hedder nu 'Forhåndsvis' i stedet for 'Send' fordi
    den åbner preview-trinnet, ikke sender direkte.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👀 Forhåndsvis", callback_data="fb_preview")],
        [InlineKeyboardButton("❌ Afbryd",     callback_data="fb_cancel")],
    ])


def _build_feedback_preview_keyboard() -> InlineKeyboardMarkup:
    """
    Trin 3 (NY v0.16.0): Preview-trin med Bekræft / Rediger / Afbryd.

    Bruges når bruger har trykket '👀 Forhåndsvis' og ser preview af
    deres samlede feedback før den faktisk sendes.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Bekræft og Send", callback_data="fb_submit")],
        [InlineKeyboardButton("✏️ Rediger (skriv mere)", callback_data="fb_edit")],
        [InlineKeyboardButton("❌ Afbryd",            callback_data="fb_cancel")],
    ])


def _build_user_received_reply_keyboard(feedback_id: int) -> InlineKeyboardMarkup:
    """
    Inline-knap på besked brugeren modtager når admin svarer (v0.16.0 #5b).

    Knappen åbner feedback-flow med pre-udfyldt "Re: \\#<id> ".
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "💬 Svar tilbage",
            callback_data=f"fb_reply_to:{feedback_id}",
        )
    ]])


def _build_admin_notification_keyboard(
    feedback_id: int,
    user_id: int,
) -> InlineKeyboardMarkup:
    """
    Inline-knapper på admin-notifikation (v0.16.0 #5).

    Hybrid-design (Option C):
      - 💬 Svar i admin-bot → URL deep-link til admin-bot's chat
        med pre-fyldt /reply kommando.
      - ✅ Resolve / 👁 Set → callback i Buddy main, kalder DB direkte.

    Args:
      feedback_id: ID på feedback-recorden
      user_id:     telegram_id på afsenderen (bruges ikke pt., men
                   kan bruges i fremtidige features)
    """
    rows: list[list[InlineKeyboardButton]] = []

    # Knap 1: Åbn admin-bot for at svare
    if ADMIN_BOT_USERNAME:
        # Deep-link med start parameter — admin-bot kan parse /start <param>
        # for at åbne /reply <id> flow direkte.
        # Format: https://t.me/<botname>?start=reply_<id>
        deep_link = f"https://t.me/{ADMIN_BOT_USERNAME}?start=reply_{feedback_id}"
        rows.append([
            InlineKeyboardButton("💬 Svar i admin-bot", url=deep_link)
        ])
    else:
        # Fallback hvis ADMIN_BOT_USERNAME ikke er sat
        rows.append([
            InlineKeyboardButton(
                "💬 Brug /reply i admin-bot",
                callback_data=f"fb_admin_hint:{feedback_id}",
            )
        ])

    # Række 2: Hurtig-actions direkte i Buddy main
    rows.append([
        InlineKeyboardButton("✅ Resolve", callback_data=f"fb_admin_resolve:{feedback_id}"),
        InlineKeyboardButton("👁 Set",     callback_data=f"fb_admin_seen:{feedback_id}"),
    ])

    return InlineKeyboardMarkup(rows)


def _get_feedback_draft(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """
    Hent draft-data fra context.user_data — opretter tom hvis mangler.

    Struktur:
      {
        "feedback_type": "bug",
        "message_parts": ["første del", "anden del"],
        "screenshot_file_ids": ["AgACAg...", ...],
        "compose_message_id": 12345,  # message_id af 'Skriv din feedback' beskeden
        "started_at": 1730123456.789, # v0.16.0 — timestamp for timeout-detection
        "reply_to_id": 1,             # v0.16.0 — feedback ID hvis dette er svar tilbage
      }
    """

    draft = context.user_data.get("feedback_draft")
    if draft is None:
        draft = {
            "feedback_type":         None,
            "message_parts":         [],
            "screenshot_file_ids":   [],
            "compose_message_id":    None,
            "started_at":            time.time(),
            "reply_to_id":           None,
        }
        context.user_data["feedback_draft"] = draft
    return draft


def _is_feedback_state_expired(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Tjek om feedback-state er udløbet (v0.16.0 #2).

    Returns:
      True hvis state har været aktivt mere end FEEDBACK_STATE_TIMEOUT_SECONDS,
      eller hvis draft slet ikke findes (corrupted state).
    """

    draft = context.user_data.get("feedback_draft")
    if draft is None:
        return True
    started_at = draft.get("started_at", 0)
    if not started_at:
        return False
    return (time.time() - started_at) > FEEDBACK_STATE_TIMEOUT_SECONDS


def _clear_feedback_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nulstil draft-data efter Send eller Afbryd."""
    context.user_data.pop("feedback_draft", None)


async def handle_feedback_trigger(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Bruger trykkede '💬 Feedback'-knappen i bundmenuen.

    Vis kategori-knapperne. Hvis bruger ikke er Plex-onboarded,
    sendes de gennem onboarding først (samme guard som watch flow).
    """
    if not await _guard(update):
        return

    user = update.effective_user
    await database.log_message(user.id, "incoming", FEEDBACK_TRIGGER)

    if await _needs_plex_setup(update):
        return

    # Nulstil eventuel tidligere draft
    _clear_feedback_draft(context)

    await update.message.reply_text(
        "💬 *Hvad har du på hjerte?*\n\n"
        "Vælg en kategori 👇",
        parse_mode="Markdown",
        reply_markup=_build_feedback_category_keyboard(),
    )
    logger.info("Feedback flow startet for telegram_id=%s", user.id)


async def handle_feedback_type_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Bruger valgte feedback-kategori → vis 'Skriv din besked' prompt.

    Sætter onboarding_state='awaiting_feedback' og gemmer feedback_type
    i draft. Næste tekst-besked fra brugeren tolkes som feedback-indhold.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    user = query.from_user
    type_id = query.data.split(":", 1)[1]

    if not validate_feedback_type(type_id):
        await query.answer("Ukendt kategori", show_alert=True)
        return

    ft = get_feedback_type(type_id)

    # Gem feedback_type i draft + reset timestamp

    draft = _get_feedback_draft(context)
    draft["feedback_type"] = type_id
    draft["message_parts"] = []
    draft["screenshot_file_ids"] = []
    draft["started_at"] = time.time()  # v0.16.0 — for timeout-tracking

    # Sæt onboarding-state så handle_text router videre tekst til feedback-input
    await database.set_onboarding_state(user.id, FEEDBACK_STATE)

    prompt = (
        f"{ft['label']}\n\n"
        f"📝 *Skriv din feedback nu*\n\n"
        f"Du kan:\n"
        f"  • Skrive en eller flere tekstbeskeder\n"
        f"  • Sende billeder/screenshots (så mange du vil)\n"
        f"  • Trykke *✅ Send* når du er færdig\n\n"
        f"_Eller tryk ❌ Afbryd hvis du fortrød._"
    )

    try:
        sent = await query.edit_message_text(
            text=prompt,
            parse_mode="Markdown",
            reply_markup=_build_feedback_compose_keyboard(),
        )
        # Gem message_id så vi kan opdatere "Send"-knappen senere hvis nødvendigt
        if sent:
            draft["compose_message_id"] = sent.message_id
    except Exception as e:
        logger.warning("handle_feedback_type_callback edit fejl: %s", e)

    logger.info(
        "Feedback flow: telegram_id=%s valgte type='%s'",
        user.id, type_id,
    )


async def _handle_feedback_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
) -> None:
    """
    Bruger har skrevet tekst mens onboarding_state='awaiting_feedback'.

    Tilføj teksten til draft.message_parts. Bekræft kort at det er modtaget.
    """
    user = update.effective_user
    draft = _get_feedback_draft(context)

    # Combiner alle parts senere — bare append nu
    draft["message_parts"].append(text.strip())

    total_chars = sum(len(p) for p in draft["message_parts"])
    if total_chars > FEEDBACK_MAX_LENGTH:
        await update.message.reply_text(
            f"⚠️ Din besked er nu længere end {FEEDBACK_MAX_LENGTH} tegn — "
            f"jeg tager kun de første. Tryk *✅ Send* hvis du er færdig.",
            parse_mode="Markdown",
        )
        return

    # Diskret bekræftelse — ingen larmende besked
    parts_count   = len(draft["message_parts"])
    photos_count  = len(draft["screenshot_file_ids"])

    bits = []
    if parts_count == 1:
        bits.append("1 tekstbesked")
    elif parts_count > 1:
        bits.append(f"{parts_count} tekstbeskeder")

    if photos_count == 1:
        bits.append("1 screenshot")
    elif photos_count > 1:
        bits.append(f"{photos_count} screenshots")

    summary = " + ".join(bits) if bits else "intet endnu"

    await update.message.reply_text(
        f"📥 _Modtaget — du har sendt {summary}._\n\n"
        f"Tryk *👀 Forhåndsvis* når du er færdig, eller skriv mere.",
        parse_mode="Markdown",
        reply_markup=_build_feedback_compose_keyboard(),
    )


async def handle_feedback_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Bruger sendte et foto — gem file_id hvis i feedback-flow.

    Telegram sender flere størrelser; vi gemmer den største (sidste i listen).
    """
    if not await _guard(update):
        return

    user = update.effective_user

    # Tjek om bruger er i feedback-state
    state = await database.get_onboarding_state(user.id)
    if state != FEEDBACK_STATE:
        # Bruger er ikke i feedback-flow — ignorer fotoet
        # (vi har pt. ikke andre photo-flows i Buddy)
        logger.debug(
            "handle_feedback_photo: telegram_id=%s sendte foto uden at være i feedback-flow",
            user.id,
        )
        return

    if not update.message or not update.message.photo:
        return

    # Hent største foto-størrelse (sidste element)
    largest = update.message.photo[-1]
    file_id = largest.file_id

    draft = _get_feedback_draft(context)
    draft["screenshot_file_ids"].append(file_id)

    # Hvis brugeren også sendte caption, tilføj det til message_parts
    caption = update.message.caption
    if caption and caption.strip():
        draft["message_parts"].append(caption.strip())

    photos_count = len(draft["screenshot_file_ids"])
    parts_count  = len(draft["message_parts"])

    photo_word = "screenshot" if photos_count == 1 else "screenshots"

    msg = f"📷 _Screenshot modtaget ({photos_count} i alt)._"
    if parts_count > 0:
        msg += "\n\nTryk *👀 Forhåndsvis* når du er færdig."

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=_build_feedback_compose_keyboard(),
    )

    logger.info(
        "Feedback flow: telegram_id=%s tilføjede foto (#%d)",
        user.id, photos_count,
    )


async def handle_feedback_preview_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    v0.16.0 #7: Bruger trykkede '👀 Forhåndsvis' — vis preview-besked.

    Viser en samlet preview af feedback (kategori, tekst, screenshots-count)
    og giver brugeren 3 valg: Bekræft og Send / Rediger / Afbryd.
    """
    query = update.callback_query

    if not await _guard(update):
        return

    user = query.from_user
    draft = _get_feedback_draft(context)

    feedback_type = draft.get("feedback_type")
    if not feedback_type or not validate_feedback_type(feedback_type):
        await query.answer("Hov, der mangler en kategori — start forfra.", show_alert=True)
        await _abort_feedback_flow(query, context, user.id)
        return

    message_parts = draft.get("message_parts", [])
    file_ids      = draft.get("screenshot_file_ids", [])

    # Brugeren skal have skrevet noget — eller sendt mindst ét screenshot
    has_text  = any(p.strip() for p in message_parts)
    has_photo = bool(file_ids)
    if not has_text and not has_photo:
        await query.answer(
            "Du har ikke skrevet eller sendt noget endnu — skriv din feedback først.",
            show_alert=True,
        )
        return

    await query.answer()

    preview_text = format_feedback_preview(
        feedback_type    = feedback_type,
        message_parts    = message_parts,
        screenshot_count = len(file_ids),
    )

    try:
        await query.edit_message_text(
            text=preview_text,
            parse_mode="Markdown",
            reply_markup=_build_feedback_preview_keyboard(),
        )
    except Exception as e:
        logger.warning("handle_feedback_preview_callback edit fejl: %s", e)


async def handle_feedback_edit_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    v0.16.0 #7: Bruger trykkede '✏️ Rediger' i preview — gå tilbage til compose.

    Brugeren kan nu skrive flere beskeder eller sende flere screenshots.
    Den eksisterende draft bevares (de mister ikke det de allerede har skrevet).
    """
    query = update.callback_query
    await query.answer("Du kan skrive mere nu.")

    if not await _guard(update):
        return

    draft = _get_feedback_draft(context)
    feedback_type = draft.get("feedback_type")
    ft = get_feedback_type(feedback_type) if feedback_type else None
    type_label = ft["label"] if ft else "feedback"

    parts_count  = len(draft.get("message_parts", []))
    photos_count = len(draft.get("screenshot_file_ids", []))

    bits = []
    if parts_count == 1:
        bits.append("1 tekstbesked")
    elif parts_count > 1:
        bits.append(f"{parts_count} tekstbeskeder")
    if photos_count == 1:
        bits.append("1 screenshot")
    elif photos_count > 1:
        bits.append(f"{photos_count} screenshots")

    summary = " + ".join(bits) if bits else "intet endnu"

    prompt = (
        f"{type_label}\n\n"
        f"📝 *Skriv mere eller send flere screenshots*\n\n"
        f"_Du har allerede sendt: {summary}_\n\n"
        f"Tryk *👀 Forhåndsvis* igen når du er færdig."
    )

    try:
        await query.edit_message_text(
            text=prompt,
            parse_mode="Markdown",
            reply_markup=_build_feedback_compose_keyboard(),
        )
    except Exception as e:
        logger.warning("handle_feedback_edit_callback edit fejl: %s", e)


async def handle_feedback_submit_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Bruger trykkede '✅ Send' — gem feedback i DB og notificér admin.
    """
    query = update.callback_query

    if not await _guard(update):
        return

    user = query.from_user
    draft = _get_feedback_draft(context)

    # Validér draft
    feedback_type = draft.get("feedback_type")
    if not feedback_type or not validate_feedback_type(feedback_type):
        await query.answer("Hov, der mangler en kategori — start forfra.", show_alert=True)
        await _abort_feedback_flow(query, context, user.id)
        return

    message_parts = draft.get("message_parts", [])
    file_ids      = draft.get("screenshot_file_ids", [])

    # Kombinér tekstbeskeder
    full_message = "\n\n".join(p for p in message_parts if p.strip())

    # Brugeren skal have skrevet noget — eller sendt mindst ét screenshot
    if not full_message and not file_ids:
        await query.answer(
            "Du har ikke skrevet eller sendt noget endnu — skriv din feedback først.",
            show_alert=True,
        )
        return

    # Trim til max-længde
    if len(full_message) > FEEDBACK_MAX_LENGTH:
        full_message = full_message[:FEEDBACK_MAX_LENGTH] + "..."

    # Hvis kun screenshots og ingen tekst, brug placeholder
    if not full_message:
        full_message = "(Ingen tekstbesked — kun screenshot(s))"

    await query.answer("Sender din feedback... 🚀")

    # v0.15.0: Vis loading-spinner med det samme — submit-besked udskiftes
    # når DB-write + admin-notifikation er færdig. Forbedrer responsivitet.
    try:
        await query.edit_message_text(
            "🚀 *Sender din feedback...*\n\n_Et øjeblik..._",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    # v0.15.0: Tjek FØR submit om brugeren har sendt feedback før.
    # Skal være før submit_feedback() — efter submit findes der allerede
    # mindst én record så funktionen ville altid returnere False.
    is_first_time = False
    try:
        is_first_time = await database.is_first_time_feedback(user.id)
    except Exception as e:
        logger.warning("is_first_time_feedback fejl: %s", e)

    # Gem i DB
    try:
        feedback_id = await database.submit_feedback(
            telegram_id=user.id,
            feedback_type=feedback_type,
            message=full_message,
            screenshot_file_ids=file_ids,
            telegram_username=user.username,
            telegram_name=user.first_name,
        )
    except Exception as e:
        logger.error("submit_feedback fejl for telegram_id=%s: %s", user.id, e)
        try:
            await query.edit_message_text(
                f"❌ Hov, der gik noget galt: `{e}`\n\nPrøv igen lidt senere.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await _abort_feedback_flow(query, context, user.id, skip_db=True)
        return

    # Tak til brugeren
    thanks_text = format_user_thanks(feedback_type, feedback_id)
    try:
        await query.edit_message_text(
            text=thanks_text,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("feedback submit edit fejl: %s", e)

    # Notificér admin (best-effort — fail vises ikke til bruger)
    try:
        feedback_row = await database.get_feedback(feedback_id)
        if feedback_row:
            await notify_admin_about_feedback(
                context, feedback_row, is_first_time=is_first_time,
            )
    except Exception as e:
        logger.error("notify_admin_about_feedback fejl: %s", e)

    # Cleanup
    _clear_feedback_draft(context)
    await database.set_onboarding_state(user.id, None)

    await database.log_message(
        user.id,
        "outgoing",
        f"[feedback] type={feedback_type} id=#{feedback_id} screenshots={len(file_ids)}",
    )
    logger.info(
        "Feedback submitted: id=%d type=%s telegram_id=%s screenshots=%d",
        feedback_id, feedback_type, user.id, len(file_ids),
    )


async def handle_feedback_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Bruger trykkede '❌ Afbryd' — luk flow og slet draft."""
    query = update.callback_query
    await query.answer("Afbrudt 👍")

    user = query.from_user

    try:
        await query.edit_message_text(
            "Ingen feedback sendt. Tryk på 💬 Feedback igen når du har lyst. 👍"
        )
    except Exception as e:
        logger.warning("handle_feedback_cancel_callback edit fejl: %s", e)

    _clear_feedback_draft(context)
    await database.set_onboarding_state(user.id, None)

    logger.info("Feedback flow afbrudt af telegram_id=%s", user.id)


async def _abort_feedback_flow(
    query, context: ContextTypes.DEFAULT_TYPE, telegram_id: int,
    skip_db: bool = False,
) -> None:
    """Hjælper til oprydning ved fejl."""
    _clear_feedback_draft(context)
    if not skip_db:
        try:
            await database.set_onboarding_state(telegram_id, None)
        except Exception as e:
            logger.warning("_abort_feedback_flow DB fejl: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# v0.16.0 #5 — Admin-callback handlers (kun admin må trykke)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_admin_resolve_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Admin trykkede '✅ Resolve' på en feedback-notifikation.

    Kalder DB direkte (ingen cross-bot kommunikation nødvendig).
    Opdaterer notifikations-beskeden så den viser at feedback er løst.
    """
    query = update.callback_query
    user  = query.from_user

    # Kun admin må bruge disse knapper
    if user.id != config.ADMIN_TELEGRAM_ID:
        await query.answer("Kun admin kan bruge denne knap.", show_alert=True)
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer("Ugyldig knap", show_alert=True)
        return

    feedback_id = int(parts[1])
    await query.answer("Markerer som løst...")

    try:
        updated = await database.update_feedback_status(feedback_id, "resolved")
    except Exception as e:
        logger.error("handle_admin_resolve_callback DB fejl: %s", e)
        await query.answer(f"Fejl: {e}", show_alert=True)
        return

    if not updated:
        await query.answer(f"Feedback #{feedback_id} ikke fundet", show_alert=True)
        return

    # Opdater notifikations-beskeden så admin ser at det er klaret
    # v0.16.2: Hvis notifikationen er en photo-besked (har caption i stedet
    # for text), bruger vi edit_message_caption som fallback.
    try:
        # Tilføj "✅ LØST" badge til den eksisterende besked
        original = query.message.text or query.message.caption or ""
        new_text = f"✅ *LØST*\n\n{original}"
        # Fjern knapper (de skal ikke kunne trykkes igen)
        if query.message.text:
            await query.edit_message_text(
                text=new_text,
                parse_mode="Markdown",
            )
        else:
            # Photo-besked → brug edit_message_caption
            await query.edit_message_caption(
                caption=new_text,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.warning("handle_admin_resolve_callback edit fejl: %s", e)
        # Best-effort — feedback er stadig løst i DB selv om visning fejler

    logger.info("admin_resolve via knap: feedback_id=%d", feedback_id)


async def handle_admin_seen_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Admin trykkede '👁 Set' på en feedback-notifikation.

    Kalder DB direkte. Opdaterer notifikations-beskeden så admin
    ser at feedback er markeret som set.
    """
    query = update.callback_query
    user  = query.from_user

    if user.id != config.ADMIN_TELEGRAM_ID:
        await query.answer("Kun admin kan bruge denne knap.", show_alert=True)
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer("Ugyldig knap", show_alert=True)
        return

    feedback_id = int(parts[1])
    await query.answer("Markerer som set...")

    try:
        updated = await database.update_feedback_status(feedback_id, "seen")
    except Exception as e:
        logger.error("handle_admin_seen_callback DB fejl: %s", e)
        await query.answer(f"Fejl: {e}", show_alert=True)
        return

    if not updated:
        await query.answer(f"Feedback #{feedback_id} ikke fundet", show_alert=True)
        return

    # Opdater notifikations-beskeden — fjern knapperne men behold teksten
    # v0.16.2: Photo-besked support via edit_message_caption fallback
    try:
        original = query.message.text or query.message.caption or ""
        new_text = f"👁 *SET*\n\n{original}"
        if query.message.text:
            await query.edit_message_text(
                text=new_text,
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_caption(
                caption=new_text,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.warning("handle_admin_seen_callback edit fejl: %s", e)

    logger.info("admin_seen via knap: feedback_id=%d", feedback_id)


async def handle_admin_hint_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Fallback når ADMIN_BOT_USERNAME ikke er sat.
    Viser hint om at admin skal bruge admin-bot manuelt.

    v0.16.2: Tilføjet admin-guard for konsistens med andre admin-callbacks.
    """
    query = update.callback_query
    user  = query.from_user

    # v0.16.2: Konsistent admin-only check (samme mønster som andre admin-callbacks)
    if user.id != config.ADMIN_TELEGRAM_ID:
        await query.answer("Kun admin kan bruge denne knap.", show_alert=True)
        return

    parts = query.data.split(":", 1)
    fb_id = parts[1] if len(parts) == 2 else "?"

    await query.answer(
        f"Åbn admin-bot og skriv:\n/reply {fb_id} <din besked>",
        show_alert=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# v0.16.0 #5b — Bruger trykker '💬 Svar tilbage' på admin-svar
# ══════════════════════════════════════════════════════════════════════════════

async def handle_feedback_reply_to_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Bruger trykkede '💬 Svar tilbage' på admin's svar (v0.16.0 #5b).

    Starter ny feedback-flow med pre-udfyldt "Re: \\#<id> " som første
    message_part. Brugeren kan så skrive deres svar og sende det som
    ny feedback der refererer til den oprindelige.

    v0.16.2: Validering flyttet FØR første query.answer() for at undgå
    dobbelt-answer warning fra Telegram API.
    """
    query = update.callback_query

    # v0.16.2: Validér FØR vi svarer på callbacken
    parts = query.data.split(":", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer("Ugyldig reference", show_alert=True)
        return

    if not await _guard(update):
        await query.answer()  # luk callback hvis guard fejler
        return

    await query.answer()
    user = query.from_user

    original_feedback_id = int(parts[1])

    # Hent original feedback for at gætte rigtig kategori (vi bruger samme)
    original_type = "question"  # default
    try:
        original = await database.get_feedback(original_feedback_id)
        if original:
            original_type = original.get("feedback_type") or "question"
    except Exception as e:
        logger.warning("handle_feedback_reply_to_callback DB fejl: %s", e)

    # Initialiser ny feedback-draft som fortsættelse

    _clear_feedback_draft(context)
    draft = _get_feedback_draft(context)
    draft["feedback_type"]       = original_type
    draft["message_parts"]       = [f"Re: #{original_feedback_id}"]
    draft["screenshot_file_ids"] = []
    draft["started_at"]          = time.time()
    draft["reply_to_id"]         = original_feedback_id

    # Sæt onboarding-state
    await database.set_onboarding_state(user.id, FEEDBACK_STATE)

    ft = get_feedback_type(original_type)
    type_label = ft["label"] if ft else "Feedback"

    prompt = (
        f"💬 *Svar på feedback \\#{original_feedback_id}*\n\n"
        f"_Kategori: {type_label}_\n\n"
        f"📝 *Skriv dit svar nu*\n\n"
        f"Du kan:\n"
        f"  • Skrive en eller flere tekstbeskeder\n"
        f"  • Sende billeder/screenshots\n"
        f"  • Trykke *👀 Forhåndsvis* når du er færdig\n\n"
        f"_Beskeden 'Re: \\#{original_feedback_id}' tilføjes automatisk._"
    )

    # Send som ny besked (kan ikke edit'e den eksisterende admin-svar besked
    # fordi det ville fjerne admin's faktiske svar)
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=prompt,
            parse_mode="Markdown",
            reply_markup=_build_feedback_compose_keyboard(),
        )
    except Exception as e:
        logger.warning("handle_feedback_reply_to_callback send fejl: %s", e)

    logger.info(
        "feedback_reply_to: telegram_id=%s starter svar på #%d",
        user.id, original_feedback_id,
    )


async def notify_admin_about_feedback(
    context: ContextTypes.DEFAULT_TYPE,
    feedback: dict,
    is_first_time: bool = False,
) -> None:
    """
    Send notifikation til admin om ny feedback.

    v0.16.1: Notifikationen sendes nu via ADMIN-bot's token (ikke Buddy main).
    Det betyder admin modtager beskeden i sin Buddy_admin-chat — ikke i
    Buddy_beta-chatten. Det matcher rolleopdelingen:
      • Buddy = bruger-vendt, ren oplevelse
      • Admin = systembeskeder, notifikationer, admin-tools

    Hvis ADMIN_BOT_TOKEN env-var ikke er sat, falder vi tilbage til at sende
    notifikation via Buddy main (gammel adfærd) — så ingen notifikationer
    går tabt under migration.

    Sender først tekst-beskeden, derefter screenshots som media-group hvis
    der er nogen vedhæftet.

    NB: Screenshot file_ids stammer fra Buddy main bot. Admin-bot kan IKKE
    sende dem direkte — vi bruger derfor Buddy main's bot til screenshots
    (samme metode som tidligere).

    v0.15.0: Tager nu 'is_first_time' parameter — videresendes til
    format_admin_notification() der så tilføjer "🆕 NY TESTER" badge.
    """
    if not feedback:
        return

    admin_id = config.ADMIN_TELEGRAM_ID
    fb_id    = feedback.get("id", 0)
    user_id  = feedback.get("telegram_id", 0)

    # v0.16.0 #5: brug compact format (knapper erstatter hint-linjer)
    notification_text = format_admin_notification(
        feedback, is_first_time=is_first_time, compact=True,
    )

    # v0.16.0 #5: tilføj inline-knapper til admin-notifikationen
    notification_keyboard = _build_admin_notification_keyboard(fb_id, user_id)

    file_ids = feedback.get("screenshot_file_ids", []) or []

    # v0.16.1: Vælg hvilken bot der skal sende notifikationen.
    # Hvis ADMIN_BOT_TOKEN er sat → brug admin-bot client (notifikationen
    # popper op i Buddy_admin-chatten, ikke Buddy main).
    # Ellers → fallback til Buddy main bot (gammel adfærd).
    # v0.16.2: Bruger nu cached/lazy-init client i stedet for at oprette
    # ny Bot-instans per notifikation (performance).
    notification_bot = context.bot  # default: Buddy main
    notification_via = "buddy_main"
    admin_client = _get_admin_bot_client()
    if admin_client is not None:
        notification_bot = admin_client
        notification_via = "admin_bot"

    # Send tekst-besked
    try:
        await notification_bot.send_message(
            chat_id=admin_id,
            text=notification_text,
            parse_mode="Markdown",
            reply_markup=notification_keyboard,
        )
    except Exception as e:
        # Fallback uden Markdown hvis der er parsing-fejl
        logger.warning("notify_admin_about_feedback Markdown fejl: %s", e)
        try:
            plain = notification_text.replace("*", "").replace("_", "").replace("`", "")
            plain = plain.replace("\\#", "#").replace("\\(", "(").replace("\\)", ")")
            plain = plain.replace("\\-", "-")
            await notification_bot.send_message(
                chat_id=admin_id,
                text=plain,
                reply_markup=notification_keyboard,
            )
        except Exception as e2:
            logger.error(
                "notify_admin_about_feedback fallback fejl (via=%s): %s",
                notification_via, e2,
            )
            return

    logger.info(
        "Admin-notifikation sendt for feedback #%d (via=%s, screenshots=%d)",
        fb_id, notification_via, len(file_ids),
    )

    # Send screenshots hvis nogen.
    # VIGTIGT: file_ids er fra Buddy main's bot-domæne. Admin-bot kan IKKE
    # sende dem direkte. Vi bruger derfor ALTID Buddy main's bot til
    # screenshots — uanset hvor tekst-notifikationen blev sendt fra.
    if file_ids:
        photo_bot = context.bot  # altid Buddy main (file_ids er fra Buddy)
        try:
            if len(file_ids) == 1:
                await photo_bot.send_photo(
                    chat_id=admin_id,
                    photo=file_ids[0],
                    caption=f"📷 Screenshot fra feedback #{feedback.get('id', '?')}",
                )
            else:
                # Send som media group (max 10 per group)
                media_group = [
                    InputMediaPhoto(media=fid)
                    for fid in file_ids[:10]
                ]
                # Caption på første medie
                if media_group:
                    media_group[0] = InputMediaPhoto(
                        media=file_ids[0],
                        caption=f"📷 Screenshots fra feedback #{feedback.get('id', '?')}",
                    )
                await photo_bot.send_media_group(
                    chat_id=admin_id,
                    media=media_group,
                )

                # Hvis der er flere end 10, send resten i ny gruppe
                if len(file_ids) > 10:
                    extra_group = [
                        InputMediaPhoto(media=fid)
                        for fid in file_ids[10:20]
                    ]
                    if extra_group:
                        await photo_bot.send_media_group(
                            chat_id=admin_id,
                            media=extra_group,
                        )
        except Exception as e:
            logger.error("notify_admin_about_feedback photo-send fejl: %s", e)


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
# /test_v2 — admin DEBUG-kommando
# ══════════════════════════════════════════════════════════════════════════════

def _format_subgenre_list() -> str:
    """List ALLE subgenrer fra både film og TV katalog."""
    lines = [
        "📋 *Alle subgenre-IDs*",
        "═══════════════════════",
        "",
        "_Brug `/test_v2 <subgenre_id>` for at teste én._",
        "",
        "🎬 *FILM-SUBGENRER*",
        "─────────────",
    ]
    for cat in get_all_categories(media_type="movie"):
        lines.append(f"*{cat['label']}*")
        for sub in cat["subgenres"]:
            lines.append(f"  • `{sub['id']}` — {sub['label']}")
        lines.append("")

    lines.extend([
        "📺 *TV-SUBGENRER*",
        "─────────────",
    ])
    for cat in get_all_categories(media_type="tv"):
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
            "`/test_v2 tv_period_drama`\n"
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
        all_ids = list_subgenre_ids(media_type="all")
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
# /audit_tv_subgenres — uændret
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_audit_tv_subgenres(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    await update.message.chat.send_action("typing")
    loading = await update.message.reply_text(
        "🔍 *Auditer TV Subgenres*\n\n"
        "Tester alle subgenrer mod TV-data...",
        parse_mode="Markdown",
    )

    sub_ids = list_subgenre_ids(media_type="all")

    counts = await asyncio.gather(*[
        database.count_titles_by_subgenre(sub_id, media_type="tv")
        for sub_id in sub_ids
    ])

    results = [
        (sub_id, count, get_subgenre(sub_id))
        for sub_id, count in zip(sub_ids, counts)
    ]
    results.sort(key=lambda x: x[1], reverse=True)

    strong = [r for r in results if r[1] > 20]
    medium = [r for r in results if 1 <= r[1] <= 20]
    empty  = [r for r in results if r[1] == 0]

    try:
        status = await database.get_metadata_status()
        total_tv = status["by_media_type"]["tv"]["fetched"]
    except Exception:
        total_tv = 0

    lines = [
        "📊 *TV Subgenre Coverage*",
        "═══════════════════════",
        "",
        f"📺 *Total serier i database:* {total_tv:,}",
        f"📋 *Subgenrer testet:* {len(sub_ids)}",
        "",
        f"✅ *Stærke (>20 serier):* {len(strong)}",
        f"⚠️ *Svage (1-20 serier):* {len(medium)}",
        f"❌ *Tomme (0 serier):* {len(empty)}",
        "",
    ]

    if strong:
        lines.append("✅ *STÆRKE SUBGENRER:*")
        for sub_id, count, sub in strong:
            label = sub["label"] if sub else sub_id
            lines.append(f"  • {label} — *{count}* serier")
        lines.append("")

    if medium:
        lines.append("⚠️ *SVAGE SUBGENRER (1-20):*")
        for sub_id, count, sub in medium:
            label = sub["label"] if sub else sub_id
            lines.append(f"  • {label} — *{count}* serier")
        lines.append("")

    if empty:
        lines.append("❌ *TOMME SUBGENRER (0 serier):*")
        for sub_id, count, sub in empty:
            label = sub["label"] if sub else sub_id
            lines.append(f"  • {label}")
        lines.append("")

    lines.append("💡 *ANBEFALING:*")
    if len(empty) > 0:
        lines.append(f"  • Skjul {len(empty)} tomme subgenrer for TV-mode")
    if len(strong) >= 25:
        lines.append("  • God dækning — byg TV-flow med eksisterende katalog")
    elif len(strong) >= 15:
        lines.append("  • OK dækning — overvej at tilføje 2-3 TV-specifikke subgenrer")
    else:
        lines.append("  • Lav dækning — anbefaler at tilføje TV-specifikke subgenrer")

    report = "\n".join(lines)

    try:
        await loading.delete()
    except Exception:
        pass

    if len(report) <= 3500:
        await update.message.reply_text(report, parse_mode="Markdown")
    else:
        split_at = report.rfind("\n\n", 0, 3500)
        if split_at == -1:
            split_at = 3500
        await update.message.reply_text(report[:split_at], parse_mode="Markdown")
        await asyncio.sleep(0.3)
        await update.message.reply_text(report[split_at:], parse_mode="Markdown")

    logger.info(
        "audit_tv_subgenres: %d stærke, %d svage, %d tomme af %d total",
        len(strong), len(medium), len(empty), len(sub_ids),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Markdown helpers
# ══════════════════════════════════════════════════════════════════════════════

def escape_markdown(text: str) -> str:
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

BRUGERGUIDE_URL = "https://johnsmith66666.github.io/buddy-guide/"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Velkomst-handler for /start."""
    if not await _guard(update):
        return
    user = update.effective_user
    clear_history(user.id)
    await database.log_message(user.id, "incoming", "/start")
    if await _needs_plex_setup(update):
        return

    reply = (
        f"👋 Hej {user.first_name}!\n\n"
        "Jeg er din personlige medie-assistent for Plex. "
        "Tryk på 🍿 *Hvad skal jeg se?* for at finde noget at se, "
        "eller bare skriv til mig som til en ven.\n\n"
        "Har du en idé eller fundet en bug? Tryk på 💬 *Feedback*-knappen.\n\n"
        f"📖 [Brugerguide]({BRUGERGUIDE_URL})"
    )
    await update.message.reply_text(
        reply,
        parse_mode="Markdown",
        reply_markup=_build_main_reply_keyboard(),
        disable_web_page_preview=True,
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


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    v0.16.0 #2: /cancel — escape hatch fra feedback-state (eller andre states).

    Hvis brugeren er fastlåst i feedback-flow, kan de skrive /cancel for
    at komme ud. Rydder draft + onboarding_state.
    """
    if not await _guard(update):
        return

    user = update.effective_user
    await database.log_message(user.id, "incoming", "/cancel")

    onboarding_state = await database.get_onboarding_state(user.id)

    if onboarding_state == FEEDBACK_STATE:
        _clear_feedback_draft(context)
        await database.set_onboarding_state(user.id, None)
        await update.message.reply_text(
            "✅ *Feedback annulleret*\n\n"
            "Ingen feedback blev sendt. Tryk på 💬 Feedback igen når du har lyst.",
            parse_mode="Markdown",
            reply_markup=_build_main_reply_keyboard(),
        )
        logger.info("cmd_cancel: ryddede feedback-state for telegram_id=%s", user.id)
        return

    if onboarding_state == "awaiting_plex":
        # Brugeren er midt i Plex-onboarding — det vil vi IKKE annullere
        # fordi de har brug for at gennemføre det.
        await update.message.reply_text(
            "Hov! Du skal lige først give mig dit Plex-brugernavn for at "
            "komme videre. 🎬\n\n"
            "Skriv det herunder.",
        )
        return

    # Hvis der ikke er noget at annullere
    await update.message.reply_text(
        "Der er ikke noget at annullere lige nu. 👌\n\n"
        "Tryk 🍿 *Hvad skal jeg se?* eller 💬 *Feedback* for at komme i gang.",
        parse_mode="Markdown",
        reply_markup=_build_main_reply_keyboard(),
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
    query = update.callback_query
    await query.answer(text="Annulleret 👍", show_alert=False)
    token = query.data.split(":", 1)[1]
    if token != "none":
        await database.get_pending_request(token)
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

async def handle_info_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    # v0.16.4 FIX: Tidligere fjernede ReplyKeyboardRemove() tastaturet (knapper)
    # naar bruger trykkede paa /info_movie_<id> link — og det blev aldrig
    # genoprettet. Resultat: knapper forsvandt efter info-kort.
    # Loesning: Drop ReplyKeyboardRemove + brug samme minimale emoji-loader
    # som AI-chat (handle_text). Tastaturet forbliver synligt.
    await update.message.chat.send_action("typing")
    loading_msg = await update.message.reply_text("🔍")

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
# Message handler
# ══════════════════════════════════════════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Bundknap-triggers (skal håndteres FØR onboarding-state checks)
    if text == WATCH_FLOW_TRIGGER:
        await handle_watch_flow_trigger(update, context)
        return
    if text == FEEDBACK_TRIGGER:
        await handle_feedback_trigger(update, context)
        return

    await database.log_message(user.id, "incoming", text)

    onboarding_state = await database.get_onboarding_state(user.id)

    # v0.14.0: Hvis bruger er i feedback-input fasen, route teksten der
    if onboarding_state == FEEDBACK_STATE:
        # v0.16.0 #2: Auto-timeout efter 30 min uden aktivitet
        if _is_feedback_state_expired(context):
            logger.info(
                "Feedback-state udløbet for telegram_id=%s — rydder",
                user.id,
            )
            _clear_feedback_draft(context)
            await database.set_onboarding_state(user.id, None)
            await update.message.reply_text(
                "⏰ *Din feedback-session er udløbet*\n\n"
                "Det er gået for længe siden du startede. Tryk på "
                "💬 Feedback-knappen igen for at starte forfra.\n\n"
                "_Tip: Skriv /cancel for at komme ud af feedback-flow når som helst._",
                parse_mode="Markdown",
                reply_markup=_build_main_reply_keyboard(),
            )
            return
        await _handle_feedback_text_input(update, context, text)
        return

    if onboarding_state == "awaiting_plex":
        await _handle_plex_input(update, text)
        return

    if await _needs_plex_setup(update):
        return

    if check_session_timeout(user.id):
        await cmd_start(update, context)
        return

    # v0.16.4 — UX polish for mobil-skaerm:
    #   • Loading-besked reduceret til kun emoji (🔍) for minimal screen-fylding
    #   • Typing-indicator genindfoert som primaer "noget sker"-signal
    #   • Reply keyboard bevares (🍿 + 💬 knapperne)
    #   • Tastatur-friendly: Kortere besked = stoerre chance for at iOS/Android
    #     lukker skrivetastaturet automatisk
    plex_username, persona_id = await asyncio.gather(
        database.get_plex_username(user.id),
        database.get_persona(user.id),
    )
    # Native Telegram "Buddy is typing..." indikator i toppen af chat
    await update.message.chat.send_action("typing")
    # Minimal emoji-only loading-besked — fylder kun ~30px paa mobil
    loading_msg = await update.message.reply_text("🔍")
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
# Global error handler
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
    await database.setup_feedback_table()  # v0.14.0
    await _start_webhook_server()
    if not config.WEBHOOK_SECRET:
        logger.warning(
            "WEBHOOK_SECRET er ikke sat — webhooks accepteres uden token-tjek!"
        )
    logger.info("Buddy started in '%s' environment.", config.ENVIRONMENT)
    logger.info(
        "VERSION CHECK — v0.16.4-beta | "
        "feedback-system: JA (v3 inline-buttons) | media-aware-watch-flow: JA | "
        "tmdb-metadata-cache: JA | find-unwatched-v2: JA | "
        "test-v2-cmd: JA | audit-tv-subgenres: JA | "
        "top-keywords-dump: JA | first-time-tester-detect: JA | "
        "loading-spinner: JA (emoji-only) | preview-step: JA | auto-timeout: JA | "
        "/cancel-cmd: JA | admin-inline-buttons: JA | reply-back-button: JA | "
        "admin-notif-via-admin-bot: %s | "
        "polish-fixes: A,B,C,D | parallel-db-hot-path: JA | "
        "typing-action: JA (genindfoert) | minimal-loading: JA | "
        "info-link-keyboard-fix: JA"
        % ("JA" if ADMIN_BOT_TOKEN else "NEJ (fallback til Buddy)")
    )
    if not ADMIN_BOT_USERNAME:
        logger.warning(
            "ADMIN_BOT_USERNAME env-var er ikke sat — admin-notifikations-knappen "
            "'💬 Svar i admin-bot' vil falde tilbage til et tekst-hint."
        )
    if not ADMIN_BOT_TOKEN:
        logger.warning(
            "ADMIN_BOT_TOKEN env-var er ikke sat — admin-notifikationer sendes "
            "stadig via Buddy main (i stedet for admin-bot). "
            "Tilføj ADMIN_BOT_TOKEN til buddy-main service for at flytte "
            "notifikationerne til admin-bot's chat."
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
    app.add_handler(CommandHandler("cancel",          cmd_cancel))

    # Engangs admin-kommandoer
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

    # TV subgenre audit
    app.add_handler(CommandHandler("audit_tv_subgenres", cmd_audit_tv_subgenres))

    # Admin approval
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve_user:\d+$"))

    # ── Watch Flow (Session 2 — v0.13.0) ────────────────────────────────────
    app.add_handler(CallbackQueryHandler(handle_subgenre_media_callback,    pattern=r"^sg_media:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_category_callback, pattern=r"^sg_cat:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_pick_callback,     pattern=r"^sg_pick:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_random_callback,   pattern=r"^sg_random:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_refresh_callback,  pattern=r"^sg_refresh:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_next_callback,     pattern=r"^sg_next:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_back_callback,     pattern=r"^sg_back:"))
    app.add_handler(CallbackQueryHandler(handle_subgenre_cancel_callback,   pattern=r"^sg_cancel$"))

    # ── Feedback Flow (NYT v0.14.0, opgraderet v0.16.0) ────────────────────
    app.add_handler(CallbackQueryHandler(handle_feedback_type_callback,    pattern=r"^fb_type:"))
    app.add_handler(CallbackQueryHandler(handle_feedback_preview_callback, pattern=r"^fb_preview$"))
    app.add_handler(CallbackQueryHandler(handle_feedback_edit_callback,    pattern=r"^fb_edit$"))
    app.add_handler(CallbackQueryHandler(handle_feedback_submit_callback,  pattern=r"^fb_submit$"))
    app.add_handler(CallbackQueryHandler(handle_feedback_cancel_callback,  pattern=r"^fb_cancel$"))
    # v0.16.0 #5b: Bruger trykker 'Svar tilbage' på admin-svar
    app.add_handler(CallbackQueryHandler(handle_feedback_reply_to_callback, pattern=r"^fb_reply_to:"))
    # v0.16.0 #5: Admin trykker hurtig-knapper på notifikation
    app.add_handler(CallbackQueryHandler(handle_admin_resolve_callback, pattern=r"^fb_admin_resolve:"))
    app.add_handler(CallbackQueryHandler(handle_admin_seen_callback,    pattern=r"^fb_admin_seen:"))
    app.add_handler(CallbackQueryHandler(handle_admin_hint_callback,    pattern=r"^fb_admin_hint:"))

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

    # ── Photo handler (NYT v0.14.0) ─────────────────────────────────────────
    # Photos håndteres af feedback-flow når bruger er i 'awaiting_feedback' state.
    app.add_handler(MessageHandler(filters.PHOTO, handle_feedback_photo))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(handle_error)

    logger.info("Starting polling …")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()