"""
features/watchlist/__init__.py - Watchlist feature for Buddy 2.0.

Hybrid Plex Discover + DB watchlist med 5 min lazy sync cache.

CHANGES (v0.3.0 — RIGTIG implementation, ikke stub mere):
  - Status: STUB → READY (🔧 emoji forsvinder fra hovedmenu)
  - NY: Fuldt implementation af watchlist-visning
  - NY: Auto-sync med Plex Discover ved åbn (5 min cache)
  - NY: Manuel sync via 🔄 Sync nu knap
  - NY: Beriget metadata fra tmdb_metadata cache
  - Bruger ny services/watchlist_sync_service.py

UNCHANGED (v0.2.0):
  - Feature klasse + @register decorator
  - category=PERSONAL, menu_order=30
  - Callback registrering for "menu:watchlist"

CALLBACK-DATA KONVENTIONER:
  - menu:watchlist        — åbn watchlist (fra hovedmenu)
  - watchlist:sync        — manuel sync nu
  - back:main             — håndteres af main_menu feature
"""

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

import database
from features import Feature, FeatureCategory, FeatureRegistry, FeatureStatus
from features.watchlist.keyboards import (
    build_empty_watchlist_keyboard,
    build_loading_keyboard,
    build_watchlist_keyboard,
)
from features.watchlist.messages import (
    LOADING_FIRST_TIME,
    LOADING_SYNC,
    format_full_watchlist_message,
    format_sync_result,
)
from services import user_data_service
from services.watchlist_sync_service import (
    get_watchlist_with_metadata,
    is_sync_needed,
    sync_user_watchlist,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Hovedmenu åbn-handler
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_watchlist_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Brugeren trykkede '📺 Min watchlist' i hovedmenu.

    Lazy sync logik:
      - Hvis aldrig synced: vis "Henter..." → sync → vis resultat
      - Hvis sync er fersk (<5 min): vis cached data direkte
      - Hvis sync er gammel (>5 min): vis "Synkroniserer..." → sync → vis
    """
    query = update.callback_query
    await query.answer()

    user = update.effective_user

    # Track analytics
    user_data_service.log_feature_usage(
        telegram_id=user.id,
        feature="watchlist",
        action="menu_open",
    )

    # Hent plex_username
    plex_username = await database.get_plex_username(user.id)

    # Tjek om sync er nødvendig
    sync_needed = await is_sync_needed(user.id)

    # Vis loading-besked hvis vi skal sync'e
    if sync_needed:
        # Tjek om det er første gang (aldrig synced) for korrekt besked
        from services.watchlist_sync_service import get_last_synced_at
        last_synced = await get_last_synced_at(user.id)
        loading_text = LOADING_FIRST_TIME if last_synced is None else LOADING_SYNC

        try:
            await query.edit_message_text(
                text=loading_text,
                parse_mode="Markdown",
                reply_markup=build_loading_keyboard(),
            )
        except Exception as e:
            logger.warning("watchlist menu loading-edit fejl: %s", e)

    # Hent watchlist (med auto-sync hvis nødvendig)
    try:
        result = await get_watchlist_with_metadata(
            telegram_id=user.id,
            plex_username=plex_username,
            auto_sync=True,
        )
    except Exception as e:
        logger.error("watchlist menu — get_watchlist_with_metadata fejl: %s", e)
        try:
            await query.edit_message_text(
                text=(
                    "⚠️ *Hov, noget gik galt*\n\n"
                    f"`{e}`\n\n"
                    "Prøv igen om lidt."
                ),
                parse_mode="Markdown",
                reply_markup=build_empty_watchlist_keyboard(),
            )
        except Exception:
            pass
        return

    # Byg den fulde besked
    items          = result["items"]
    sync_status    = result["sync_status"]
    last_synced_at = result["last_synced_at"]

    text = format_full_watchlist_message(
        items=items,
        sync_status=sync_status,
        last_synced_at=last_synced_at,
    )

    # Vælg keyboard baseret på indhold
    if items:
        keyboard = build_watchlist_keyboard(has_items=True)
    else:
        keyboard = build_empty_watchlist_keyboard()

    try:
        await query.edit_message_text(
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("watchlist menu edit fejl: %s — sender plain", e)
        # Fallback uden Markdown
        plain = text.replace("*", "").replace("_", "").replace("`", "")
        try:
            await query.edit_message_text(
                text=plain,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception as e2:
            logger.error("watchlist menu fallback fejl: %s", e2)


# ══════════════════════════════════════════════════════════════════════════════
# Manuel sync handler
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_watchlist_sync(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Brugeren trykkede '🔄 Sync nu' — tving sync uanset cache-alder."""
    query = update.callback_query
    await query.answer("🔄 Synkroniserer...")

    user = update.effective_user

    user_data_service.log_feature_usage(
        telegram_id=user.id,
        feature="watchlist",
        action="manual_sync",
    )

    plex_username = await database.get_plex_username(user.id)

    # Vis loading
    try:
        await query.edit_message_text(
            text=LOADING_SYNC,
            parse_mode="Markdown",
            reply_markup=build_loading_keyboard(),
        )
    except Exception:
        pass

    # Force sync
    try:
        sync_result = await sync_user_watchlist(
            telegram_id=user.id,
            plex_username=plex_username,
            force=True,
        )
    except Exception as e:
        logger.error("watchlist manual sync fejl: %s", e)
        try:
            await query.edit_message_text(
                text=(
                    "⚠️ *Sync fejlede*\n\n"
                    f"`{e}`\n\n"
                    "Prøv igen om lidt."
                ),
                parse_mode="Markdown",
                reply_markup=build_empty_watchlist_keyboard(),
            )
        except Exception:
            pass
        return

    # Hent opdateret data og vis
    try:
        result = await get_watchlist_with_metadata(
            telegram_id=user.id,
            plex_username=plex_username,
            auto_sync=False,  # Vi har lige sync'et, ingen grund til igen
        )
    except Exception as e:
        logger.error("watchlist sync — efter-fetch fejl: %s", e)
        return

    items = result["items"]

    # Vis besked: sync result øverst, derefter listen
    sync_summary = format_sync_result(sync_result)

    if items:
        list_text = format_full_watchlist_message(
            items=items,
            sync_status=result["sync_status"],
            last_synced_at=result["last_synced_at"],
        )
        full_text = f"{sync_summary}\n\n──────────\n\n{list_text}"
        keyboard = build_watchlist_keyboard(has_items=True)
    else:
        full_text = sync_summary
        keyboard = build_empty_watchlist_keyboard()

    # Telegram har 4096 tegn-grænse — trim hvis nødvendigt
    if len(full_text) > 4000:
        full_text = full_text[:3997] + "..."

    try:
        await query.edit_message_text(
            text=full_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("watchlist sync result edit fejl: %s", e)
        plain = full_text.replace("*", "").replace("_", "").replace("`", "")
        try:
            await query.edit_message_text(
                text=plain,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception as e2:
            logger.error("watchlist sync result fallback fejl: %s", e2)


# ══════════════════════════════════════════════════════════════════════════════
# Feature class
# ══════════════════════════════════════════════════════════════════════════════

@FeatureRegistry.register
class WatchlistFeature(Feature):
    """
    📺 Min watchlist — Plex Discover watchlist med lokal cache.

    Hybrid sync: Plex er kilde til sandhed, vi cacher 5 min lokalt.
    Auto-fjern: Hvis bruger fjerner i Plex-app, fjernes også her.
    """

    id            = "watchlist"
    label         = "📺 Min watchlist"
    enabled       = True
    requires_plex = True
    menu_order    = 30
    category      = FeatureCategory.PERSONAL
    status        = FeatureStatus.READY  # ← v0.3.0: Ikke længere STUB!

    description = (
        "Se og administrer din Plex Discover watchlist. "
        "Synkroniseres automatisk med Plex-appen."
    )

    def register_handlers(self, app: Application) -> None:
        """Registrér watchlist-handlers."""
        # Hovedmenu åbn
        app.add_handler(CallbackQueryHandler(
            _handle_watchlist_menu,
            pattern=r"^menu:watchlist$",
        ))

        # Manuel sync
        app.add_handler(CallbackQueryHandler(
            _handle_watchlist_sync,
            pattern=r"^watchlist:sync$",
        ))

        logger.debug("WatchlistFeature handlers registreret (v0.3.0 RIGTIG)")