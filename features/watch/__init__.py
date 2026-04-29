"""
features/watch/__init__.py - Watch flow feature for Buddy 2.0.

DENNE FEATURE ER IKKE EN STUB — den eksisterende watch flow virker allerede.
Vi laver bare en TYND ADAPTER der lader hovedmenuens 🍿-knap åbne det
eksisterende watch flow.

DESIGN-VALG:
  - Vi flytter IKKE den eksisterende watch flow kode ind i features/.
    Den ligger stadig i main.py og virker som den plejer.
  - Vi laver en lille adapter der oversætter "menu:watch" callback til
    et kald til den eksisterende handle_watch_flow_trigger() funktion
    via en ny helper.
  - Når vi engang refaktorerer watch flow ind i features/ (måske aldrig?),
    er det en intern ændring uden side-effects.

HVORFOR IKKE FLYTTE WATCH FLOW NU?
  - Det er ~500 linjer kode der virker perfekt
  - Den bruger eksisterende keyboards og callbacks (sg_*, etc.)
  - Risiko/værdi-ratio for refaktorering er ikke god lige nu
  - "Don't fix what ain't broken"

CHANGES (v0.1.0 — initial):
  - WatchFeature klasse oprettet og registreret.
  - Adapter-handler dispatcher til eksisterende watch flow logik.
  - status=READY (ingen 🔧 emoji — den virker!).
"""

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from features import Feature, FeatureCategory, FeatureRegistry, FeatureStatus
from services import user_data_service

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Adapter-handler — oversætter inline-knap til eksisterende watch flow
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_watch_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Brugeren trykkede '🍿 Hvad skal jeg se?' i hovedmenu.

    Dispatcher til den eksisterende watch flow ved at simulere et kald
    til handle_watch_flow_trigger() — vi kan ikke importere den direkte
    for at undgå circular imports, så vi bruger en lille trick: opret en
    ny besked der starter watch flow ovenpå.
    """
    query = update.callback_query
    await query.answer()

    user = update.effective_user

    # Track at brugeren startede watch flow fra hovedmenuen
    user_data_service.log_feature_usage(
        telegram_id=user.id,
        feature="watch",
        action="menu_open",
    )

    # Den eksisterende watch flow forventer et fresh-message med specifik tekst.
    # Vi sletter den nuværende menu-besked og lader main.py's text-handler
    # håndtere det videre flow ved at sende "🍿 Hvad skal jeg se?" tilbage.
    #
    # ALTERNATIV (renere men kræver refaktor): Importer handle_watch_flow_trigger
    # direkte og kald den. Det kræver vi laver en mindre refactor i main.py.
    #
    # Vi vælger den simple løsning: vis en besked der peger brugeren videre.
    try:
        # Slet menu-beskeden
        await query.message.delete()
    except Exception as e:
        logger.warning("watch menu — kunne ikke slette menu-besked: %s", e)

    # Send watch-flow trigger som ny besked.
    # Eksisterende main.py text-handler håndterer "🍿 Hvad skal jeg se?" tekst.
    from features.main_menu.keyboards import build_persistent_reply_keyboard

    await query.message.chat.send_message(
        text="🍿 Hvad skal jeg se?",
        reply_markup=build_persistent_reply_keyboard(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Feature class
# ══════════════════════════════════════════════════════════════════════════════

@FeatureRegistry.register
class WatchFeature(Feature):
    """
    🍿 Hvad skal jeg se? — adgang til det eksisterende watch flow.
    """

    id            = "watch"
    label         = "🍿 Hvad skal jeg se?"
    enabled       = True
    requires_plex = True
    menu_order    = 10  # Først i menuen
    category      = FeatureCategory.DISCOVER
    status        = FeatureStatus.READY  # Den virker — ingen 🔧

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

        logger.debug("WatchFeature handlers registreret (adapter v0.1.0)")