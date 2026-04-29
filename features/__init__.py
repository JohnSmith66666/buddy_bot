"""
features/__init__.py - Feature base class and central registry for Buddy 2.0.

CHANGES (v0.2.0 — main menu support):
  - NY: FeatureCategory enum (DISCOVER, PERSONAL, TOOLS, COMMUNICATION).
    Bruges af main_menu til auto-gruppering når vi rammer 9+ features.
  - NY: Feature.category attribut (default: DISCOVER).
  - NY: Feature.status attribut ('ready' | 'stub' | 'beta').
    'stub' viser 🔧 emoji i menuen så brugeren ved den er under bygning.
    'beta' viser 🧪 emoji.
  - NY: Feature.get_menu_label() helper der tilføjer status-emoji.
  - NY: FeatureRegistry.get_by_category() til auto-gruppering.

UNCHANGED (v0.1.0 — initial):
  - Feature abstract base class med id, label, enabled, requires_plex, menu_order.
  - FeatureRegistry singleton med register decorator.
  - register_all_handlers() kaldes fra main.py ved bot opstart.
"""

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Type

from telegram.ext import Application

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Feature category — for auto-grouping in main menu
# ══════════════════════════════════════════════════════════════════════════════

class FeatureCategory(str, Enum):
    """
    Kategorier til auto-gruppering i hovedmenuen.

    Når vi har <9 features vises alle direkte. Når vi rammer 9+ grupperes
    de automatisk efter kategori i undermenuer.

    Værdier er strings så de kan bruges direkte i callback_data uden
    konvertering (fx 'menu:cat:discover').
    """
    DISCOVER      = "discover"       # 🍿 Se noget, 🆕 Nye, 🔥 Trending, 🎯 For dig
    PERSONAL      = "personal"       # 📺 Watchlist, 📊 Stats, 📅 Nostalgi
    TOOLS         = "tools"          # 🔍 Søg, 🤖 Spørg Buddy
    COMMUNICATION = "communication"  # 💬 Feedback, 🔔 Notifikationer


CATEGORY_LABELS: dict[FeatureCategory, str] = {
    FeatureCategory.DISCOVER:      "🍿 Find indhold",
    FeatureCategory.PERSONAL:      "👤 Mit personlige",
    FeatureCategory.TOOLS:         "🛠 Værktøjer",
    FeatureCategory.COMMUNICATION: "💬 Kommunikation",
}


# ══════════════════════════════════════════════════════════════════════════════
# Feature status — visual indicators in menus
# ══════════════════════════════════════════════════════════════════════════════

class FeatureStatus(str, Enum):
    """
    Feature-status til visuel markering i UI.

    READY:  Færdig feature, ingen emoji-markering.
    STUB:   Under bygning, vises med 🔧 emoji.
    BETA:   Eksperimentel, vises med 🧪 emoji.
    """
    READY = "ready"
    STUB  = "stub"
    BETA  = "beta"


# Emojis vist i menu-knapper baseret på status
STATUS_EMOJI: dict[FeatureStatus, str] = {
    FeatureStatus.READY: "",
    FeatureStatus.STUB:  " 🔧",
    FeatureStatus.BETA:  " 🧪",
}


# ══════════════════════════════════════════════════════════════════════════════
# Feature base class
# ══════════════════════════════════════════════════════════════════════════════

class Feature(ABC):
    """
    Abstract base class for all Buddy 2.0 features.

    Hver feature SKAL:
      1. Sætte `id` (unik string, fx "watchlist")
      2. Sætte `label` (Telegram knap-tekst, fx "📺 Min watchlist")
      3. Implementere register_handlers(app)

    Hver feature KAN overskrive:
      - enabled (default True)
      - requires_plex (default True)
      - menu_order (default 100)
      - category (default DISCOVER)
      - status (default READY)
      - description (default "")
    """

    # ── Required attributes ───────────────────────────────────────────────────
    id: str = ""
    label: str = ""

    # ── Optional attributes ───────────────────────────────────────────────────
    enabled: bool = True
    requires_plex: bool = True
    menu_order: int = 100
    category: FeatureCategory = FeatureCategory.DISCOVER
    status: FeatureStatus = FeatureStatus.READY
    description: str = ""

    @abstractmethod
    def register_handlers(self, app: Application) -> None:
        """Register all Telegram handlers this feature needs."""
        ...

    def get_menu_label(self) -> str:
        """
        Returnér label til menu-knap, inkl. status-emoji hvis ikke ready.

        Eksempel:
          status=READY  → "📺 Min watchlist"
          status=STUB   → "📺 Min watchlist 🔧"
          status=BETA   → "📺 Min watchlist 🧪"
        """
        emoji = STATUS_EMOJI.get(self.status, "")
        return f"{self.label}{emoji}"

    def get_menu_button(self) -> tuple[str, str]:
        """Returnér (label_med_emoji, callback_data) til menu-keyboard."""
        return (self.get_menu_label(), f"menu:{self.id}")

    def __repr__(self) -> str:
        status = "enabled" if self.enabled else "DISABLED"
        return (
            f"<Feature id='{self.id}' label='{self.label}' "
            f"category={self.category.value} status={self.status.value} {status}>"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Feature registry
# ══════════════════════════════════════════════════════════════════════════════

class FeatureRegistry:
    """
    Central registry — alle Feature-subklasser registreres her via
    @FeatureRegistry.register decorator.
    """

    _features: dict[str, Feature] = {}

    @classmethod
    def register(cls, feature_class: Type[Feature]) -> Type[Feature]:
        """Decorator der registrerer en Feature-subklasse."""
        instance = feature_class()

        if not instance.id:
            raise ValueError(
                f"Feature {feature_class.__name__} mangler 'id' attribut"
            )
        if not instance.label:
            raise ValueError(
                f"Feature {feature_class.__name__} mangler 'label' attribut"
            )
        if instance.id in cls._features:
            logger.warning(
                "Feature id='%s' er allerede registreret — overskriver",
                instance.id,
            )

        cls._features[instance.id] = instance
        logger.debug("Feature registreret: %s", instance)
        return feature_class

    @classmethod
    def get_all(cls, include_disabled: bool = False) -> list[Feature]:
        """Returnér alle registrerede features sorteret efter menu_order."""
        features = list(cls._features.values())
        if not include_disabled:
            features = [f for f in features if f.enabled]
        return sorted(features, key=lambda f: f.menu_order)

    @classmethod
    def get(cls, feature_id: str) -> Feature | None:
        """Hent én feature ved ID. Returnerer None hvis ikke findes."""
        return cls._features.get(feature_id)

    @classmethod
    def is_enabled(cls, feature_id: str) -> bool:
        """Hurtigt tjek om en feature er registreret OG enabled."""
        feature = cls._features.get(feature_id)
        return feature is not None and feature.enabled

    @classmethod
    def get_by_category(
        cls,
        include_disabled: bool = False,
    ) -> dict[FeatureCategory, list[Feature]]:
        """
        Gruppér enabled features efter category.

        Returnerer dict hvor hver category mapper til en liste af features
        sorteret efter menu_order. Tomme kategorier inkluderes ikke.
        """
        features = cls.get_all(include_disabled=include_disabled)
        grouped: dict[FeatureCategory, list[Feature]] = {}

        for feature in features:
            grouped.setdefault(feature.category, []).append(feature)

        return grouped

    @classmethod
    def register_all_handlers(cls, app: Application) -> None:
        """
        Registrér Telegram-handlers for alle enabled features.

        Kaldes ÉN gang fra main.py ved bot-opstart.
        """
        enabled_features = cls.get_all(include_disabled=False)

        if not enabled_features:
            logger.warning("FeatureRegistry: ingen enabled features at registrere")
            return

        logger.info(
            "Registrerer handlers for %d feature(s): %s",
            len(enabled_features),
            [f.id for f in enabled_features],
        )

        for feature in enabled_features:
            try:
                feature.register_handlers(app)
                logger.info("✅ Feature handlers registreret: %s", feature.id)
            except Exception as e:
                logger.error(
                    "❌ Fejl ved registrering af feature '%s': %s",
                    feature.id, e,
                )

    @classmethod
    def reset(cls) -> None:
        """Ryd registry'et — primært til tests."""
        cls._features.clear()
        logger.debug("FeatureRegistry reset")


# ══════════════════════════════════════════════════════════════════════════════
# Public exports
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "Feature",
    "FeatureRegistry",
    "FeatureCategory",
    "FeatureStatus",
    "CATEGORY_LABELS",
]