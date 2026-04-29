"""
features/__init__.py - Feature base class and central registry for Buddy 2.0.

Dette modul definerer arkitekturen for hvordan ALLE Buddy 2.0 features
struktureres. Hver feature er en selvstændig pakke i features/ mappen
der følger samme mønster:

    features/
    ├── __init__.py          (denne fil — base-klasse + registry)
    ├── watchlist/
    │   ├── __init__.py      (Feature subklasse + register decorator)
    │   ├── handlers.py      (Telegram handlers — tynd UI-adapter)
    │   ├── service.py       (forretningslogik — UI-agnostisk)
    │   ├── keyboards.py     (InlineKeyboardMarkup builders)
    │   └── messages.py      (danske tekst-templates)
    ├── recommendations/
    │   └── ...
    └── archaeologist/
        └── ...

DESIGN-PRINCIPPER:
  - Hver feature er SELVSTÆNDIG: kan bygges, testes og rolles tilbage uafhængigt.
  - Hver feature følger SAMME MØNSTER: handlers/service/keyboards/messages.
  - main.py rører IKKE feature-koden direkte — den kalder kun
    FeatureRegistry.register_all_handlers(app) ÉN gang ved opstart.
  - Feature-flags: enabled=False slår en feature fra uden at slette kode.
  - UI-agnostisk service-lag forbereder fremtidig MiniApp/API.

KONVENTIONER FOR CALLBACK_DATA:
  - Hovedmenu-knapper:   "menu:<feature_id>"        (fx "menu:watchlist")
  - Feature-interne:     "<feature_id>:<action>:<args>"  (fx "watchlist:add:27205:movie")
  - Globale:             "back:main", "cancel"

Det giver os entydig routing — main.py kan dispatche callbacks ud til
den rigtige feature baseret på prefixen før første ":".

CHANGES (v0.1.0 — initial):
  - Feature abstract base class med id, label, enabled, requires_plex.
  - FeatureRegistry singleton med register decorator + lookup helpers.
  - register_all_handlers() kaldes fra main.py ved bot opstart.
"""

import logging
from abc import ABC, abstractmethod
from typing import Type

from telegram.ext import Application

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Feature base class
# ══════════════════════════════════════════════════════════════════════════════

class Feature(ABC):
    """
    Abstract base class for all Buddy 2.0 features.

    Hver feature SKAL:
      1. Sætte `id` (unik string, fx "watchlist")
      2. Sætte `label` (Telegram knap-tekst, fx "📺 Min watchlist")
      3. Implementere register_handlers(app) der tilføjer Telegram handlers

    Hver feature KAN overskrive:
      - enabled (default True) — sæt False for at deaktivere uden at slette kode
      - requires_plex (default True) — sæt False for features der virker uden Plex
      - menu_order (default 100) — lavere tal = højere oppe i menuen
      - description (default "") — vises i /help
    """

    # ── Required attributes ───────────────────────────────────────────────────
    id: str = ""
    label: str = ""

    # ── Optional attributes ───────────────────────────────────────────────────
    enabled: bool = True
    requires_plex: bool = True
    menu_order: int = 100
    description: str = ""

    @abstractmethod
    def register_handlers(self, app: Application) -> None:
        """
        Register all Telegram handlers this feature needs.

        Kaldes ÉN gang ved bot-opstart fra FeatureRegistry.register_all_handlers().
        Featuren skal selv vide hvilke CommandHandler, CallbackQueryHandler,
        MessageHandler osv. den behøver.

        Eksempel:
            from telegram.ext import CommandHandler, CallbackQueryHandler
            from .handlers import cmd_watchlist, handle_watchlist_callback

            app.add_handler(CommandHandler("watchlist", cmd_watchlist))
            app.add_handler(CallbackQueryHandler(
                handle_watchlist_callback, pattern=r"^watchlist:"
            ))
        """
        ...

    def get_menu_button(self) -> tuple[str, str]:
        """
        Returnér (label, callback_data) til hovedmenu-keyboard builder.

        Standard callback_data er "menu:<id>". Override hvis featuren
        har brug for noget andet (sjældent — undgå hvis muligt).
        """
        return (self.label, f"menu:{self.id}")

    def __repr__(self) -> str:
        status = "enabled" if self.enabled else "DISABLED"
        return f"<Feature id='{self.id}' label='{self.label}' {status}>"


# ══════════════════════════════════════════════════════════════════════════════
# Feature registry
# ══════════════════════════════════════════════════════════════════════════════

class FeatureRegistry:
    """
    Central registry — alle Feature-subklasser registreres her via
    @FeatureRegistry.register decorator.

    Brug:
        @FeatureRegistry.register
        class WatchlistFeature(Feature):
            id = "watchlist"
            label = "📺 Min watchlist"
            ...

    Registry'et er en class-level singleton — ingen instans nødvendig.
    Det betyder vi kan importere features hvor som helst, og de bliver
    automatisk registreret når deres modul importeres.
    """

    _features: dict[str, Feature] = {}

    @classmethod
    def register(cls, feature_class: Type[Feature]) -> Type[Feature]:
        """
        Decorator der registrerer en Feature-subklasse.

        Anvendes som @FeatureRegistry.register oven over class-deklarationen.
        Opretter automatisk én instans af klassen og gemmer den i registry'et.
        """
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
        """
        Returnér alle registrerede features sorteret efter menu_order.

        Args:
          include_disabled: Hvis True, inkluderes features med enabled=False.
                            Default False (kun enabled features).
        """
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
    def register_all_handlers(cls, app: Application) -> None:
        """
        Registrér Telegram-handlers for alle enabled features.

        Kaldes ÉN gang fra main.py ved bot-opstart, efter app er bygget
        og før app.run_polling().

        Eksempel i main.py:
            from features import FeatureRegistry
            import features.watchlist  # noqa: F401 — trigger @register
            # ... import flere features ...

            app = Application.builder()....build()
            # ... eksisterende handler-registrering ...
            FeatureRegistry.register_all_handlers(app)
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
        """
        Ryd registry'et — primært til tests.

        ADVARSEL: Bruges IKKE i production-kode. Hvis du tror du har brug
        for det, har du sandsynligvis et arkitektur-problem.
        """
        cls._features.clear()
        logger.debug("FeatureRegistry reset")


# ══════════════════════════════════════════════════════════════════════════════
# Public exports
# ══════════════════════════════════════════════════════════════════════════════

__all__ = ["Feature", "FeatureRegistry"]