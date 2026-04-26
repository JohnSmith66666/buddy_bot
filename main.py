"""
main.py - Buddy bot entry point.

CHANGES vs previous version (v0.10.5 — /test_metadata kommando, Step 1 af subgenre-projekt):
  - NY ENGANGS-FEATURE: /test_metadata <tmdb_id eller titel>
    Step 1 af subgenre-projektet (jf. plan med PostgreSQL cache + GIN-indekser):
    * Henter TMDB-genrer + keywords for én film/serie
    * Formaterer output pænt så vi manuelt kan vurdere mængden af
      "guld" vs. "støj" i keyword-listen
    * To input-modes:
        /test_metadata 27205          → direkte TMDB ID
        /test_metadata Inception       → titel-søgning, viser top 5 hits
    * Bruger ny services/tmdb_keywords_service.py
  - VERSION CHECK opdateret til v0.10.5-beta.

UNCHANGED (v0.10.4 — /test_enrich DRY-RUN kommando — bevares).
UNCHANGED (v0.10.3 — /dump_genres admin-kommando).
UNCHANGED (v0.10.2 — /genres admin-kommando).
UNCHANGED (v0.10.1 — genre-parameter fix).
UNCHANGED (v0.10.0 — 'Hvad skal jeg se?' interaktivt flow).
UNCHANGED (v0.9.8 — Annuller-knap photo-fix).
"""

import asyncio
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
    fetch_movie_metadata,
    fetch_tv_metadata,
    search_tmdb_by_title,
)
from services.webhook_service import handle_radarr_webhook, handle_sonarr_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"(https?://[^\s)\]>\"]+)")


# ══════════════════════════════════════════════════════════════════════════════
# 'HVAD SKAL JEG SE?' — Konstanter og helpers (uændret fra v0.10.4)
# ══════════════════════════════════════════════════════════════════════════════

WATCH_FLOW_TRIGGER = "🍿 Hvad skal jeg se?"
MAX_MOODS = 2

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

WATCH_PAGES: dict[int, list[int]] = {
    1: [1, 2, 3, 4, 5, 6],
    2: [7, 8, 9, 10, 11, 12],
    3: [13, 14, 15, 16, 17],
}
TOTAL_PAGES = 3


def _build_main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(WATCH_FLOW_TRIGGER)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _get_watch_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if "watch_flow" not in context.user_data:
        context.user_data["watch_flow"] = {
            "media_type":     None,
            "selected_moods": [],
            "page":           1,
        }
    return context.user_data["watch_flow"]


def _clear_watch_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("watch_flow", None)


def _build_type_selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Film",   callback_data="watch_type:movie"),
            InlineKeyboardButton("📺 Serie",  callback_data="watch_type:tv"),
        ],
        [InlineKeyboardButton("🎲 Overrask mig!", callback_data="watch_type:surprise")],
        [InlineKeyboardButton("❌ Afbryd",        callback_data="watch_cancel")],
    ])


def _build_mood_keyboard(state: dict) -> InlineKeyboardMarkup:
    page          = state["page"]
    selected      = set(state["selected_moods"])
    media_type    = state["media_type"]
    mood_ids      = WATCH_PAGES[page]

    rows: list[list[InlineKeyboardButton]] = []

    row: list[InlineKeyboardButton] = []
    for mood_id in mood_ids:
        mood   = WATCH_MOODS[mood_id]
        prefix = "✅ " if mood_id in selected else ""
        label  = f"{prefix}{mood['emoji']} {mood['name']}"
        row.append(InlineKeyboardButton(label, callback_data=f"watch_mood:{mood_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Forrige", callback_data=f"watch_page:{page-1}"))
    if page < TOTAL_PAGES:
        nav_row.append(InlineKeyboardButton("Næste ➡️",   callback_data=f"watch_page:{page+1}"))
    if nav_row:
        rows.append(nav_row)

    if selected:
        search_btn = InlineKeyboardButton("🚀 Søg nu!", callback_data="watch_search")
    else:
        search_btn = InlineKeyboardButton("🔒 Vælg en stemning", callback_data="watch_noop")
    rows.append([
        search_btn,
        InlineKeyboardButton("🎲 Overrask mig!", callback_data="watch_surprise"),
    ])

    rows.append([InlineKeyboardButton("❌ Afbryd", callback_data="watch_cancel")])

    return InlineKeyboardMarkup(rows)


def _build_mood_message_text(state: dict) -> str:
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
    media_type = state.get("media_type")
    media_word = "film" if media_type == "movie" else ("serier" if media_type == "tv" else "film eller serier")

    if mode == "search":
        all_genres: list[str] = []
        for mood_id in state["selected_moods"]:
            for g in WATCH_MOODS[mood_id]["genres"]:
                if g not in all_genres:
                    all_genres.append(g)

        genre_list = "\n".join(f"- {g}" for g in all_genres)
        media_arg  = "movie" if media_type == "movie" else "tv"

        return (
            f"Brugeren vil have anbefalinger til {media_word} fra disse genrer:\n"
            f"{genre_list}\n\n"
            f"VIGTIGT — gør PRÆCIS dette:\n"
            f"1. Kald find_unwatched ÉN GANG PER GENRE — separate parallelle kald. "
            f"Hvert kald skal have media_type='{media_arg}' og genre=<præcis ét genre-navn fra listen>. "
            f"Send ALDRIG flere genrer som komma-separeret streng — det fejler.\n"
            f"2. Når du har resultaterne, vælg de 4-5 bedste {media_word} på tværs af alle "
            f"resultat-sættene.\n"
            f"3. Vis dem i ✅-listeformat med klikbare /info_movie_X eller /info_tv_X links "
            f"og en kort begrundelse for hver. Svar i din persona — kort, præcis, venlig."
        )

    if mode == "surprise":
        return (
            f"Find 4-5 tilfældige, fremragende skjulte perler blandt {media_word} på Plex, "
            f"som brugeren ikke har set endnu. Sælg dem godt med en kort, fængende "
            f"begrundelse. Brug ✅-listeformat med klikbare /info_movie_X eller "
            f"/info_tv_X links."
        )

    return (
        "Find 4-5 tilfældige, fremragende skjulte perler på Plex (både film og serier "
        "er fint), som brugeren ikke har set endnu. Sælg dem godt med en kort, fængende "
        "begrundelse. Brug ✅-listeformat med klikbare /info_movie_X eller "
        "/info_tv_X links."
    )


# ══════════════════════════════════════════════════════════════════════════════
# /genres + /dump_genres — Engangs admin-kommandoer (uændret fra tidligere)
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

    logger.info(
        "dump_genres: %d film + %d serier dumpet (%.1f KB) → %s",
        data["metadata"]["movie_count"], data["metadata"]["tv_count"], size_kb, filename,
    )

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
# /test_metadata — Engangs admin DRY-RUN af TMDB metadata-fetch (v0.10.5)
# ══════════════════════════════════════════════════════════════════════════════
#
# DESIGN:
#   STEP 1 i subgenre-projektet (jf. plan med PostgreSQL cache + GIN-indekser).
#   Formål: vurdere kvaliteten af TMDB keywords MANUELT før vi designer
#   database-schemaet og whitelist-logikken.
#
# To input-modes:
#   /test_metadata 27205         → direkte TMDB ID (movie default)
#   /test_metadata tv 1396       → TV ID
#   /test_metadata Inception     → titel-søgning, viser top 5 hits
#
# Output viser:
#   - Titel, år, media_type
#   - Alle TMDB-genrer (engelsk)
#   - Alle TMDB-keywords med count
#   - Vurderings-prompt: "Hvor mange er guld vs. støj?"

def _format_metadata_report(meta: dict) -> str:
    """Formater metadata-resultat som pæn Markdown-besked til Telegram."""
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

    lines.extend([
        "",
        f"🏷️ *TMDB Keywords* ({k_count})",
        "─────────────────",
    ])

    if keywords:
        # Vis ALLE keywords som bullet-liste — vi vil se den fulde "støj"
        for kw in keywords:
            lines.append(f"  • {kw}")
    else:
        lines.append("  _(ingen keywords)_")

    if meta.get("keyword_warning"):
        lines.extend(["", f"⚠️ _{meta['keyword_warning']}_"])

    lines.extend([
        "",
        "─────────────────",
        "🔍 *Vurder selv:*",
        "  • Hvor mange keywords er *guld* (subgenre-værdige)?",
        "    F.eks.: cyberpunk, heist, slasher, neo-noir",
        "  • Hvor mange er *støj* (irrelevant for subgenre)?",
        "    F.eks.: 'based on novel', 'duringcreditsstinger'",
        "",
        "_Når du har kigget på 5-10 forskellige film, kan vi designe whitelisten._",
    ])

    return "\n".join(lines)


def _format_search_results(query: str, results: list[dict]) -> str:
    """Vis søgeresultater når brugeren skrev en titel."""
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
    """
    Engangs admin-kommando: /test_metadata <tmdb_id eller titel>

    Eksempler:
      /test_metadata 27205              → Inception (movie default)
      /test_metadata movie 27205        → eksplicit movie
      /test_metadata tv 1396            → Breaking Bad
      /test_metadata Inception          → titel-søgning
      /test_metadata Breaking Bad       → titel-søgning
    """
    user = update.effective_user
    if user is None or user.id != config.ADMIN_TELEGRAM_ID:
        return

    # Parse argumenter
    args = context.args
    if not args:
        await update.message.reply_text(
            "📖 *Brug:*\n"
            "`/test_metadata <tmdb_id>`\n"
            "`/test_metadata movie <tmdb_id>`\n"
            "`/test_metadata tv <tmdb_id>`\n"
            "`/test_metadata <titel>`\n\n"
            "*Eksempler:*\n"
            "`/test_metadata 27205`\n"
            "`/test_metadata tv 1396`\n"
            "`/test_metadata Inception`",
            parse_mode="Markdown",
        )
        return

    await update.message.chat.send_action("typing")

    # ── Mode 1: 'movie <id>' eller 'tv <id>' ──────────────────────────────────
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

    # ── Mode 2: kun et tal (default movie) ────────────────────────────────────
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

        # Hvis ikke fundet som movie, prøv tv automatisk
        if meta.get("status") == "not_found":
            try:
                meta = await fetch_tv_metadata(tmdb_id)
                if meta.get("status") == "ok":
                    logger.info("test_metadata: ID %d fundet som TV efter movie-fejl", tmdb_id)
            except Exception:
                pass

        report = _format_metadata_report(meta)
        try:
            await loading.delete()
        except Exception:
            pass
        await update.message.reply_text(report, parse_mode="Markdown")
        return

    # ── Mode 3: titel-søgning ────────────────────────────────────────────────
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

    # Hvis præcis 1 resultat — hent metadata direkte
    if len(results) == 1:
        r = results[0]
        if r["media_type"] == "movie":
            meta = await fetch_movie_metadata(r["tmdb_id"])
        else:
            meta = await fetch_tv_metadata(r["tmdb_id"])
        report = _format_metadata_report(meta)
        await update.message.reply_text(report, parse_mode="Markdown")
        return

    # Flere resultater — vis liste
    report = _format_search_results(query, results)
    await update.message.reply_text(report, parse_mode="Markdown")


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
# 'Hvad skal jeg se?' callbacks (uændret)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_watch_flow_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    user = update.effective_user
    await database.log_message(user.id, "incoming", WATCH_FLOW_TRIGGER)

    if await _needs_plex_setup(update):
        return

    _clear_watch_state(context)

    await update.message.reply_text(
        "🍿 *Hvad skal jeg se?*\n\nVælg hvad du er i humør til:",
        parse_mode="Markdown",
        reply_markup=_build_type_selection_keyboard(),
    )
    logger.info("Watch flow startet for telegram_id=%s", user.id)


async def handle_watch_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    media_type = query.data.split(":", 1)[1]

    if media_type == "surprise":
        state = _get_watch_state(context)
        prompt = _build_watch_prompt(state, mode="wildcard")
        await _execute_ai_handoff(update, context, query, prompt)
        return

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


async def handle_watch_mood_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def handle_watch_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def handle_watch_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    state = _get_watch_state(context)
    if not state.get("media_type"):
        prompt = _build_watch_prompt(state, mode="wildcard")
    else:
        prompt = _build_watch_prompt(state, mode="surprise")

    await _execute_ai_handoff(update, context, query, prompt)


async def handle_watch_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _clear_watch_state(context)

    try:
        await query.edit_message_text("Aflyst. Tryk på 🍿-knappen igen når du vil have hjælp. 👍")
    except Exception as e:
        logger.warning("handle_watch_cancel_callback edit fejl: %s", e)


async def handle_watch_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Vælg mindst 1 stemning først 👇", show_alert=False)


async def _execute_ai_handoff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query,
    prompt: str,
) -> None:
    user        = update.effective_user
    chat        = query.message.chat
    state_snapshot = dict(_get_watch_state(context))
    _clear_watch_state(context)

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

    await _process_ai_reply(update, context, chat, user, plex_username, reply)


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
    await query.answer()

    token = query.data.split(":", 1)[1]
    if token != "none":
        await database.get_pending_request(token)

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
# /info_movie_<id> og /info_tv_<id> handler
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
            "WEBHOOK_SECRET er ikke sat — webhooks accepteres uden token-tjek!"
        )
    logger.info("Buddy started in '%s' environment.", config.ENVIRONMENT)
    logger.info(
        "VERSION CHECK — v0.10.5-beta | "
        "søgeresultater-UX: JA | foto-fix: JA | watch-flow: JA | "
        "watch-genre-split-fix: JA | genres-cmd: JA | dump-genres-cmd: JA | "
        "test-metadata-cmd: JA"
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

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("skift_plex",    cmd_skift_plex))
    app.add_handler(CommandHandler("genres",        cmd_genres))         # Engangs admin
    app.add_handler(CommandHandler("dump_genres",   cmd_dump_genres))    # Engangs admin
    app.add_handler(CommandHandler("test_metadata", cmd_test_metadata))  # NY v0.10.5

    # Admin approval
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve_user:\d+$"))

    # 'Hvad skal jeg se?' flow
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