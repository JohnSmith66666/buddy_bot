"""
features/watch/__init__.py - Watch flow feature for Buddy 2.0.

Tynd ADAPTER der lader hovedmenuens 🍿-knap åbne det eksisterende watch flow
i main.py.

CHANGES (v0.2.0 — fix for "knappen virker ikke"):
  - FIX: Tidligere version sendte falsk tekst-besked og forventede at
    handle_text fangede den. Det virker IKKE — Telegram ignorerer
    bot-beskeder i MessageHandlers.
  - LØSNING: Importér TRIN2_HEADER + _build_media_keyboard fra main.py
    (lazy import inde i handler for at undgå circular import).
  - Når brugeren trykker 🍿, sletter vi menu-beskeden og sender
    Trin 2 (media-valg) som ny besked — eksakt samme oplevelse som
    den gamle reply-keyboard knap.

CHANGES (v0.1.0 — initial, deprecated):
  - Tidligere implementation der ikke virkede.
"""

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from features import Feature, FeatureCategory, FeatureRegistry, FeatureStatus
from services import user_data_service

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Adapter-handler
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_watch_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Brugeren trykkede '🍿 Hvad skal jeg se?' i hovedmenu.

    Sletter menu-beskeden og åbner Trin 2 (media-valg) som ny besked.
    Resten af watch-flow (kategorier, subgenrer, resultater) håndteres
    af de eksisterende sg_* callbacks i main.py.
    """
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    chat = query.message.chat

    # Track analytics
    user_data_service.log_feature_usage(
        telegram_id=user.id,
        feature="watch",
        action="menu_open",
    )

    # Lazy import for at undgå circular dependency med main.py
    try:
        from main import TRIN2_HEADER, _build_media_keyboard
    except ImportError as e:
        logger.error("Kunne ikke importere watch-flow fra main: %s", e)
        try:
            await query.edit_message_text(
                "❌ Watch-flow ikke tilgængelig. Prøv den gamle 🍿 knap nederst."
            )
        except Exception:
            pass
        return

    # Slet hovedmenu-beskeden
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning("watch menu — kunne ikke slette menu-besked: %s", e)

    # Send Trin 2 (media-valg) — eksisterende sg_* handlers tager over derfra
    await chat.send_message(
        TRIN2_HEADER,
        parse_mode="Markdown",
        reply_markup=_build_media_keyboard(),
    )

    logger.info("Watch flow startet via hovedmenu for telegram_id=%s", user.id)


# ══════════════════════════════════════════════════════════════════════════════
# Feature class
# ══════════════════════════════════════════════════════════════════════════════

@FeatureRegistry.register
class WatchFeature(Feature):
    """🍿 Hvad skal jeg se? — adgang til det eksisterende watch flow."""

    id            = "watch"
    label         = "🍿 Hvad skal jeg se?"
    enabled       = True
    requires_plex = True
    menu_order    = 10
    category      = FeatureCategory.DISCOVER
    status        = FeatureStatus.READY

    description = (
        "Find noget at se baseret på din humør og bibliotekets undergenrer. "
        "36 specifikke kategorier i 9 hovedgrupper."
    )

    def register_handlers(self, app: Application) -> None:
        """Registrér watch-menu adapter-handler."""
        app.add_handler(CallbackQueryHandler(
            _handle_watch_menu,
            pattern=r"^menu:watch$",
        ))

        logger.debug("WatchFeature handlers registreret (adapter v0.2.0)")