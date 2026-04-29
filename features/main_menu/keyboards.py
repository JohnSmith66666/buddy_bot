"""
features/main_menu/keyboards.py - Keyboard builders for hovedmenuen.

Indeholder logikken til at bygge både:
  - Inline-keyboard (inde i hovedmenu-besked, dynamisk fra FeatureRegistry)
  - Reply-keyboard (persistent nederst i Telegram med 🏠 Hjem + 💬 Feedback)

DESIGN-PRINCIPPER:
  - Inline keyboard genereres dynamisk fra FeatureRegistry — features
    der tilføjes/fjernes fremover kræver ingen ændringer her.
  - Reply keyboard er statisk (kun 2 knapper) — ændres sjældent.
  - Auto-gruppering aktiveres når antal features ≥ MAX_FLAT_FEATURES.
  - 2-kolonner layout som standard for inline (matcher Telegram best practices).

CHANGES (v0.1.0 — initial):
  - build_main_menu_inline() — flad eller grupperet visning.
  - build_persistent_reply_keyboard() — Hjem + Feedback.
  - build_category_menu_inline() — undermenu for én kategori.
  - HOME_BUTTON_LABEL og FEEDBACK_BUTTON_LABEL konstanter.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import KeyboardButton, ReplyKeyboardMarkup

from features import (
    CATEGORY_LABELS,
    Feature,
    FeatureCategory,
    FeatureRegistry,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Konstanter
# ══════════════════════════════════════════════════════════════════════════════

# Reply-keyboard knap-labels (skal matche text-handlers i main.py)
HOME_BUTTON_LABEL     = "🏠 Hjem"
FEEDBACK_BUTTON_LABEL = "💬 Feedback"

# Hvornår skifter vi fra flad visning til kategori-grupperet?
# Under denne grænse: vis alle features direkte.
# Ved/over denne grænse: vis kun kategorier som top-level.
MAX_FLAT_FEATURES = 9

# Antal kolonner i inline-keyboard
INLINE_COLUMNS = 2


# ══════════════════════════════════════════════════════════════════════════════
# Inline keyboard — hovedmenu
# ══════════════════════════════════════════════════════════════════════════════

def build_main_menu_inline() -> InlineKeyboardMarkup:
    """
    Bygger inline-keyboard til hovedmenuen.

    Dynamisk fra FeatureRegistry — viser alle enabled features.
    Auto-grupperer hvis der er ≥ MAX_FLAT_FEATURES features.

    Returns:
      InlineKeyboardMarkup klar til reply_markup parameter.
    """
    features = FeatureRegistry.get_all(include_disabled=False)

    if not features:
        # Edge case: ingen features registreret — vis tom besked-knap
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("ℹ️ Ingen features tilgængelige", callback_data="menu:noop"),
        ]])

    # Beslut visnings-strategi
    if len(features) < MAX_FLAT_FEATURES:
        return _build_flat_inline(features)
    else:
        return _build_grouped_inline()


def _build_flat_inline(features: list[Feature]) -> InlineKeyboardMarkup:
    """
    Flad visning: alle features i 2-kolonne grid sorteret efter menu_order.

    Bruges når vi har <9 features.
    """
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []

    for feature in features:
        label, callback = feature.get_menu_button()
        current_row.append(
            InlineKeyboardButton(label, callback_data=callback)
        )

        if len(current_row) >= INLINE_COLUMNS:
            rows.append(current_row)
            current_row = []

    # Tilføj sidste række hvis ikke fuld
    if current_row:
        rows.append(current_row)

    return InlineKeyboardMarkup(rows)


def _build_grouped_inline() -> InlineKeyboardMarkup:
    """
    Grupperet visning: kategorier som top-level, undermenuer for hver.

    Bruges når vi har ≥9 features. Hver kategori får én knap der åbner
    en undermenu med kategoriens features.
    """
    grouped = FeatureRegistry.get_by_category(include_disabled=False)
    rows: list[list[InlineKeyboardButton]] = []

    # Sortér kategorier efter den definerede rækkefølge i FeatureCategory enum
    for category in FeatureCategory:
        if category not in grouped or not grouped[category]:
            continue

        cat_label = CATEGORY_LABELS.get(category, category.value)
        feature_count = len(grouped[category])
        button_label = f"{cat_label} ({feature_count})"

        rows.append([
            InlineKeyboardButton(
                button_label,
                callback_data=f"menu:cat:{category.value}",
            )
        ])

    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Inline keyboard — kategori-undermenu
# ══════════════════════════════════════════════════════════════════════════════

def build_category_menu_inline(category: FeatureCategory) -> InlineKeyboardMarkup:
    """
    Bygger undermenu for én specifik kategori.

    Viser alle enabled features i kategorien + tilbage-knap.
    """
    grouped = FeatureRegistry.get_by_category(include_disabled=False)
    features = grouped.get(category, [])

    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []

    for feature in features:
        label, callback = feature.get_menu_button()
        current_row.append(
            InlineKeyboardButton(label, callback_data=callback)
        )

        if len(current_row) >= INLINE_COLUMNS:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    # Tilbage-knap
    rows.append([
        InlineKeyboardButton("⬅️ Tilbage", callback_data="back:main"),
    ])

    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Reply keyboard — persistent (altid synlig)
# ══════════════════════════════════════════════════════════════════════════════

def build_persistent_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Bygger reply-keyboard der altid er synlig nederst i Telegram.

    Indeholder kun universelle navigations-knapper:
      - 🏠 Hjem      → vis hovedmenu
      - 💬 Feedback  → start feedback-flow

    Resten af features er på inline-keyboard inde i hovedmenu-beskeden.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(HOME_BUTTON_LABEL),
                KeyboardButton(FEEDBACK_BUTTON_LABEL),
            ],
        ],
        resize_keyboard=True,    # Lille keyboard, ikke fyldt op
        is_persistent=True,      # Forsvinder ikke når brugeren skriver
    )