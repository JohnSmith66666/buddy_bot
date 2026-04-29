"""
features/main_menu/__init__.py - Main menu feature for Buddy 2.0.

Hovedmenuen er en SPECIEL feature der:
  1. Registrerer "back:main" og "menu:home" callbacks (universal navigation)
  2. Registrerer "menu:cat:<category>" callbacks (kategori-undermenuer)
  3. Eksponerer show_main_menu() som API til main.py og andre features

DESIGN-PRINCIPPER:
  - Hovedmenuen er IKKE selv en knap i menuen — den ER menuen.
    Derfor sætter vi enabled=False så den ikke vises som feature-knap.
    register_handlers() kører stadig fordi vi explicit kalder den.
  - show_main_menu() er den ENESTE måde at vise hovedmenuen — kald den
    fra cmd_start, fra "🏠 Hjem" reply-button, og fra "back:main" callbacks.
  - Beskeden er adaptiv: bruger fornavn hvis tilgængeligt, ellers generisk.

CHANGES (v0.1.0 — initial):
  - MainMenuFeature klasse oprettet.
  - show_main_menu() public API til at vise menuen.
  - handle_back_main_callback() håndterer ⬅️ Tilbage knapper.
  - handle_category_callback() håndterer kategori-undermenuer.
  - handle_noop_callback() håndterer placeholder-knapper.
"""

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from features import (
    Feature,
    FeatureCategory,
    FeatureRegistry,
)
from features.main_menu.keyboards import (
    build_category_menu_inline,
    build_main_menu_inline,
    build_persistent_reply_keyboard,
)
from services import user_data_service

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Public API — show_main_menu (kaldes fra main.py og andre features)
# ══════════════════════════════════════════════════════════════════════════════

async def show_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit: bool = False,
) -> None:
    """
    Vis hovedmenuen til brugeren.

    Args:
      update:  Den indkommende Telegram update.
      context: PTB context (bruges ikke pt., men holdes klar til fremtidig brug).
      edit:    Hvis True, redigér eksisterende besked (typisk fra callback).
               Hvis False, send ny besked (typisk fra /start eller reply-keyboard).

    Bruges fra:
      - cmd_start i main.py
      - 🏠 Hjem knap-handler i main.py
      - "back:main" callback (handle_back_main_callback)
    """
    user = update.effective_user
    if user is None:
        return

    first_name = user.first_name or "der"

    text = (
        f"👋 *Hej {_escape(first_name)}!*\n\n"
        f"Hvad har du lyst til? 🍿"
    )

    inline_keyboard = build_main_menu_inline()

    if edit and update.callback_query:
        # Redigér eksisterende besked (kommer fra inline-knap)
        try:
            await update.callback_query.edit_message_text(
                text=text,
                parse_mode="Markdown",
                reply_markup=inline_keyboard,
            )
            return
        except Exception as e:
            logger.warning("show_main_menu edit fejl: %s — sender ny besked", e)

    # Send ny besked (kommer fra /start eller reply-button)
    if update.message:
        await update.message.reply_text(
            text=text,
            parse_mode="Markdown",
            reply_markup=inline_keyboard,
        )
    elif update.callback_query and update.callback_query.message:
        # Fallback hvis edit ikke virkede
        await update.callback_query.message.reply_text(
            text=text,
            parse_mode="Markdown",
            reply_markup=inline_keyboard,
        )


def _escape(text: str) -> str:
    """Minimal Markdown-escape for fornavn (undgår fejl ved navne med _ * etc.)"""
    if not text:
        return ""
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Callback handlers
# ══════════════════════════════════════════════════════════════════════════════

async def handle_back_main_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Håndterer ⬅️ Tilbage knap der peger på 'back:main'."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    user_data_service.log_feature_usage(
        telegram_id=user.id,
        feature="main_menu",
        action="back_to_main",
    )

    await show_main_menu(update, context, edit=True)


async def handle_category_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Håndterer 'menu:cat:<category>' callbacks fra grupperet hovedmenu.

    Aktiveres kun når antal features ≥ MAX_FLAT_FEATURES og menuen er
    i grupperet visning. I Phase 1 (få features) bruges dette ikke.
    """
    query = update.callback_query
    await query.answer()

    user = update.effective_user

    # Parse category fra callback_data: "menu:cat:<value>"
    try:
        category_value = query.data.split(":", 2)[2]
        category = FeatureCategory(category_value)
    except (IndexError, ValueError) as e:
        logger.warning("handle_category_callback: ugyldig callback_data='%s': %s",
                       query.data, e)
        await query.edit_message_text("❌ Ugyldigt menu-valg. Prøv igen.")
        return

    user_data_service.log_feature_usage(
        telegram_id=user.id,
        feature="main_menu",
        action="open_category",
        metadata={"category": category_value},
    )

    from features import CATEGORY_LABELS
    cat_label = CATEGORY_LABELS.get(category, category.value)

    text = f"*{cat_label}*\n\n_Vælg en funktion:_"
    keyboard = build_category_menu_inline(category)

    try:
        await query.edit_message_text(
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning("handle_category_callback edit fejl: %s", e)


async def handle_noop_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Håndterer placeholder/no-op callbacks (fx 'menu:noop')."""
    query = update.callback_query
    await query.answer("Denne funktion er ikke aktiv endnu.", show_alert=False)


# ══════════════════════════════════════════════════════════════════════════════
# Feature class
# ══════════════════════════════════════════════════════════════════════════════

@FeatureRegistry.register
class MainMenuFeature(Feature):
    """
    🏠 Hovedmenu — central hub for alle Buddy 2.0 features.

    SPECIEL FEATURE: Vises IKKE som knap i menuen (enabled=False for menu-visning,
    men handlers registreres alligevel via custom register_handlers).
    """

    id            = "main_menu"
    label         = "🏠 Hjem"          # Vises ikke pga enabled=False
    enabled       = False              # ← gør at den IKKE vises som menu-knap
    requires_plex = False
    menu_order    = 0                  # Ville være først hvis enabled=True

    def register_handlers(self, app: Application) -> None:
        """
        Registrér hovedmenu-callbacks.

        VIGTIGT: Selvom enabled=False (så den ikke vises i menuen), kalder
        FeatureRegistry IKKE register_handlers automatisk for disabled features.
        Vi løser det ved at kalde register_main_menu_handlers() direkte fra main.py.
        Se register_main_menu_handlers() nedenfor.
        """
        # Denne metode kaldes IKKE fordi enabled=False.
        # Vi har register_main_menu_handlers() som workaround.
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Manual handler registration (workaround for enabled=False)
# ══════════════════════════════════════════════════════════════════════════════

def register_main_menu_handlers(app: Application) -> None:
    """
    Registrér main menu callbacks manuelt (kaldes fra main.py).

    Da MainMenuFeature har enabled=False (for at undgå at vises som knap),
    kører FeatureRegistry IKKE dens register_handlers automatisk.
    Derfor kalder main.py denne funktion direkte EFTER FeatureRegistry.register_all_handlers().
    """
    # back:main → vis hovedmenu igen
    app.add_handler(CallbackQueryHandler(
        handle_back_main_callback,
        pattern=r"^back:main$",
    ))

    # menu:cat:<category> → vis kategori-undermenu
    app.add_handler(CallbackQueryHandler(
        handle_category_callback,
        pattern=r"^menu:cat:",
    ))

    # menu:noop → placeholder/no-op
    app.add_handler(CallbackQueryHandler(
        handle_noop_callback,
        pattern=r"^menu:noop$",
    ))

    logger.info("✅ Main menu handlers registreret (back:main, menu:cat:*, menu:noop)")


# ══════════════════════════════════════════════════════════════════════════════
# Public exports
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "show_main_menu",
    "register_main_menu_handlers",
    "build_persistent_reply_keyboard",  # re-export fra keyboards.py
]