"""
features/watchlist/__init__.py - Watchlist feature for Buddy 2.0.

CHANGES (v0.2.0 — main menu integration):
  - Tilføjet category=PERSONAL og status=STUB attributter.
  - Status=STUB betyder labellen vises med 🔧 emoji i hovedmenuen
    så brugeren ved featuren er under bygning.
  - Tilføjet "back:main" handler så ⬅️ Tilbage knap virker.

UNCHANGED (v0.1.0):
  - WatchlistFeature klasse oprettet og registreret.
  - register_handlers() opretter CallbackQueryHandler for "menu:watchlist".
  - Stub-handler viser placeholder-besked + tilbage-knap.

CALLBACK_DATA-KONVENTION:
  - menu:watchlist                 — main menu klik (åbn watchlist)
  - watchlist:list                 — vis hele listen (kommer)
  - watchlist:add:<tmdb>:<type>    — tilføj titel (kommer)
  - watchlist:remove:<tmdb>:<type> — fjern titel (kommer)
  - watchlist:toggle:<tmdb>:<type> — toggle (kommer)
  - watchlist:back                 — tilbage til hovedmenu
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from features import Feature, FeatureCategory, FeatureRegistry, FeatureStatus
from services import user_data_service

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Stub handlers (erstattes i næste iteration)
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_watchlist_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Stub: Brugeren trykkede '📺 Min watchlist' i hovedmenu."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user

    # Track at brugeren åbnede watchlist (analytics)
    user_data_service.log_feature_usage(
        telegram_id=user.id,
        feature="watchlist",
        action="menu_open",
    )

    # Tæl entries
    try:
        movie_count = await user_data_service.count_watchlist(user.id, media_type="movie")
        tv_count    = await user_data_service.count_watchlist(user.id, media_type="tv")
        total       = movie_count + tv_count
    except Exception as e:
        logger.error("watchlist menu — count fejlede: %s", e)
        movie_count = tv_count = total = 0

    if total == 0:
        text = (
            "📺 *Min watchlist* 🔧\n\n"
            "_Du har ikke gemt nogen titler endnu._\n\n"
            "Når du finder en spændende film eller serie, "
            "kan du gemme den her med ⭐-knappen — så kan vi finde den frem igen senere.\n\n"
            "_Funktionen åbner snart for dig — den er under bygning lige nu._ 🔧"
        )
    else:
        text = (
            f"📺 *Min watchlist* 🔧\n\n"
            f"Du har gemt:\n"
            f"  🎬 *{movie_count}* film\n"
            f"  📺 *{tv_count}* serier\n"
            f"  ──────────\n"
            f"  📊 *{total}* i alt\n\n"
            f"_Selve listen-visningen er under bygning — "
            f"vender tilbage med den helt snart!_ 🔧"
        )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Tilbage", callback_data="back:main"),
    ]])

    try:
        await query.edit_message_text(
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning("watchlist menu edit fejlede: %s — sender plain", e)
        plain = text.replace("*", "").replace("_", "")
        await query.edit_message_text(text=plain, reply_markup=keyboard)


# ══════════════════════════════════════════════════════════════════════════════
# Feature class
# ══════════════════════════════════════════════════════════════════════════════

@FeatureRegistry.register
class WatchlistFeature(Feature):
    """
    📺 Min watchlist — bruger-gemte titler til senere visning.

    Foundation for andre features i Buddy 2.0.
    """

    id            = "watchlist"
    label         = "📺 Min watchlist"
    enabled       = True
    requires_plex = True
    menu_order    = 30
    category      = FeatureCategory.PERSONAL
    status        = FeatureStatus.STUB  # 🔧 Under bygning

    description = (
        "Gem film og serier til senere visning. "
        "Buddy holder styr på din liste og minder dig om den."
    )

    def register_handlers(self, app: Application) -> None:
        """Registrér watchlist-relaterede Telegram handlers."""
        # Hovedmenu-knap → åbn watchlist
        app.add_handler(CallbackQueryHandler(
            _handle_watchlist_menu,
            pattern=r"^menu:watchlist$",
        ))

        logger.debug("WatchlistFeature handlers registreret (stub v0.2.0)")