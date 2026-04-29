"""
features/watchlist/keyboards.py - Inline keyboard builders for watchlist UI.

CHANGES (v0.1.0 — initial):
  - build_watchlist_keyboard() — knapper under watchlist-listen
  - build_empty_watchlist_keyboard() — knapper når listen er tom
  - build_loading_keyboard() — under sync (kun tilbage-knap)

CALLBACK-DATA KONVENTIONER:
  - menu:watchlist        — åbn watchlist (fra hovedmenu)
  - watchlist:sync        — manuel sync nu
  - watchlist:sort        — åbn sortér-menu (Leverance B)
  - back:main             — tilbage til hovedmenu
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_watchlist_keyboard(has_items: bool = True) -> InlineKeyboardMarkup:
    """
    Knapper under watchlist-listen.

    Args:
      has_items: Hvis False, vis kun tilbage-knap (tom watchlist).
    """
    rows: list[list[InlineKeyboardButton]] = []

    if has_items:
        # Manuel sync + sortér (sortér kommer i Leverance B)
        rows.append([
            InlineKeyboardButton("🔄 Sync nu", callback_data="watchlist:sync"),
        ])

    rows.append([
        InlineKeyboardButton("⬅️ Tilbage", callback_data="back:main"),
    ])

    return InlineKeyboardMarkup(rows)


def build_empty_watchlist_keyboard() -> InlineKeyboardMarkup:
    """Tom watchlist — kun tilbage-knap."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Tjek igen", callback_data="watchlist:sync"),
        InlineKeyboardButton("⬅️ Tilbage", callback_data="back:main"),
    ]])


def build_loading_keyboard() -> InlineKeyboardMarkup:
    """Under sync — kun tilbage-knap (afbryd ikke understøttet)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Tilbage", callback_data="back:main"),
    ]])