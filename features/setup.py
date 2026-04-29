"""
features/setup.py - Central setup module for Buddy 2.0 integration.

Dette modul samler ALL Buddy 2.0 initialisering ét sted så main.py
kun behøver at importere ÉN funktion: setup_buddy_2(app).

DESIGN-PRINCIP:
  - Minimal indgriben i main.py.
  - main.py ændres KUN ved at tilføje 1 import og 1 funktionskald.
  - Al integration-logik er her.
  - Hvis du vil rulle Buddy 2.0 tilbage, fjerner du bare det ene kald.

CHANGES (v0.1.0 — initial):
  - setup_buddy_2(app) registrerer alle features + main menu callbacks.
  - Importerer alle features for at trigger @register decorators.
"""

import logging

from telegram.ext import Application

# Importer alle features — dette trigger @FeatureRegistry.register decorators
import features.main_menu  # noqa: F401
import features.watch      # noqa: F401
import features.watchlist  # noqa: F401

from features import FeatureRegistry
from features.main_menu import register_main_menu_handlers

logger = logging.getLogger(__name__)


def setup_buddy_2(app: Application) -> None:
    """
    Initialisér alle Buddy 2.0 features og main menu handlers.

    Kaldes ÉN gang fra main.py's main() funktion, helst lige før
    app.add_error_handler(handle_error).

    Eksempel:
        from features.setup import setup_buddy_2

        # ... eksisterende app.add_handler() kald ...

        setup_buddy_2(app)  # ← tilføj denne linje

        app.add_error_handler(handle_error)
        app.run_polling(...)
    """
    # Registrér alle feature-handlers (watch, watchlist, etc.)
    FeatureRegistry.register_all_handlers(app)

    # Registrér main menu callbacks (back:main, menu:cat:*, menu:noop)
    register_main_menu_handlers(app)

    logger.info("✅ Buddy 2.0 setup complete")