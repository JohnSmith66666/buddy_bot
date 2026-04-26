"""
services/subgenre_service.py - Subgenre catalog and lookup helpers.

CHANGES (v0.2.0 — sjove danske labels):
  - Alle 9 kategorier og 36 subgenrer har fået nye, charmerende danske
    labels med personlighed (fx "Flad af Grin" → "Olsen Banden på steroider").
  - Strukturen er uændret: id'er, plex_genre og keywords er bevaret 100%.
  - Kun 'label'-feltet er ændret — det betyder INGEN database-migration
    eller ændring af find_films_by_subgenre() er nødvendig.

UNCHANGED (v0.1.0 — Etape 1 af subgenre-projekt):
  - 36 subgenrer i 9 kategorier baseret på datadrevet analyse af
    keywords_movie_min5 dump (6.588 film).
  - Hybrid keyword + Plex-genre logik (OR mellem keywords, AND med plex_genre).
  - Lookup-funktioner: get_subgenre, get_category, get_all_categories,
    get_category_for_subgenre, list_subgenre_ids, validate_subgenre_id.

DESIGN-PRINCIPPER:
  - Sjove danske labels med personlighed — vores "voice" som dansk medie-
    assistent. Stadig scannable: brugeren skal kunne se navnet og inden
    for 1 sekund forstå hvilken stemning det dækker.
  - Engelske TMDB keywords for konsistens med find_unwatched_v2 SQL-queries.
  - Hver subgenre har minimum ~30 matches i film-biblioteket (datavalideret).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SUBGENRE-KATALOG
# ══════════════════════════════════════════════════════════════════════════════
#
# Format per subgenre:
#   id             → unik ID, må kun indeholde lowercase + underscore
#   label          → sjovt dansk navn med emoji (vises på knap)
#   plex_genre     → krævet Plex-genre ("dørmand"), None hvis ingen
#   keywords       → TMDB keywords (engelsk, OR-match)

SUBGENRES: dict[str, dict] = {

    # ──── 1. FLAD AF GRIN-SKUFFEN ─────────────────────────────────────────────
    "comedy_dark": {
        "label":      "🖤 Kulsort & Absurd",
        "plex_genre": "Komedie",
        "keywords":   ["dark comedy", "absurd"],
    },
    "comedy_romcom": {
        "label":      "🛋️ Netflix og chill",
        "plex_genre": "Komedie",
        "keywords":   ["romcom", "romantic"],
    },
    "comedy_standup": {
        "label":      "🎤 Én mikrofon, nul filter",
        "plex_genre": "Komedie",
        "keywords":   ["stand-up comedy"],
    },
    "comedy_satire": {
        "label":      "🤪 Sluk hjernen, tak",
        "plex_genre": "Komedie",
        "keywords":   ["satire", "hilarious"],
    },

    # ──── 2. EKSPLOSIONER & TESTOSTERON-SKUFFEN ───────────────────────────────
    "action_superhero": {
        "label":      "🦸 Spandex & Superkræfter",
        "plex_genre": None,
        "keywords":   ["superhero", "based on comic"],
    },
    "action_martial": {
        "label":      "🥋 Flyvespark & Flækkede Læber",
        "plex_genre": "Action",
        "keywords":   ["martial arts", "shootout"],
    },
    "action_survival": {
        "label":      "🏃 Løb for helvede!",
        "plex_genre": "Action",
        "keywords":   ["survival", "escape"],
    },
    "action_roadtrip": {
        "label":      "🚗 Fuld tank & Dårlige valg",
        "plex_genre": None,
        "keywords":   ["road trip"],
    },

    # ──── 3. SKIFT UNDERBUKSER BAGEFTER-SKUFFEN ───────────────────────────────
    "horror_psycho": {
        "label":      "🧠 Mindfuck",
        "plex_genre": "Thriller",
        "keywords":   ["psychological thriller"],
    },
    "horror_slasher": {
        "label":      "🪓 Motorsave & Ketchup",
        "plex_genre": "Gyser",
        "keywords":   ["slasher", "gore", "murder"],
    },
    "horror_creature": {
        "label":      "🧟 Hjernespisere & Haglgeværer",
        "plex_genre": "Gyser",
        "keywords":   ["zombie", "monster", "creature"],
    },
    "horror_supernatural": {
        "label":      "👻 Ting der siger BØH i mørket",
        "plex_genre": "Gyser",
        "keywords":   ["supernatural horror", "witch"],
    },

    # ──── 4. HVEM GJORDE DET?-SKUFFEN ─────────────────────────────────────────
    "crime_heist": {
        "label":      "💰 Olsen Banden på steroider",
        "plex_genre": None,
        "keywords":   ["heist", "bank robbery"],
    },
    "crime_noir": {
        "label":      "🚬 Regnvejr & Kyniske detektiver",
        "plex_genre": None,
        "keywords":   ["neo-noir", "detective", "investigation"],
    },
    "crime_serialkiller": {
        "label":      "🩸 Gale mordere på fri fod",
        "plex_genre": None,
        "keywords":   ["serial killer", "psychopath"],
    },
    "crime_mafia": {
        "label":      "🕴️ Betonsko & Hestehoveder",
        "plex_genre": "Kriminalitet",
        "keywords":   ["gangster", "police"],
    },

    # ──── 5. LASERE & FOLIEHATTE-SKUFFEN ──────────────────────────────────────
    "scifi_time": {
        "label":      "🕰️ Rod i tidslinjen",
        "plex_genre": None,
        "keywords":   ["time travel"],
    },
    "scifi_dystopia": {
        "label":      "🏚️ Verden er gået ad helvede til",
        "plex_genre": None,
        # FIX: TMDB bruger "post-apocalyptic future" (53 hits), ikke "post-apocalyptic" (0 hits)
        "keywords":   ["dystopia", "post-apocalyptic future", "apocalypse"],
    },
    "scifi_alien": {
        "label":      "👽 De har lasere!",
        "plex_genre": None,
        "keywords":   ["alien"],
    },
    "fantasy_magic": {
        "label":      "🧙 Nørder med tryllestave",
        "plex_genre": "Fantasy",
        "keywords":   ["magic", "witch"],
    },

    # ──── 6. FIND KLEENEX FREM-SKUFFEN ────────────────────────────────────────
    "drama_youth": {
        "label":      "🎒 Hormoner & High School",
        "plex_genre": None,
        "keywords":   ["coming of age", "high school", "teenager"],
    },
    "drama_tearjerker": {
        "label":      "💔 Tudekiks & Tragedie",
        "plex_genre": "Drama",
        "keywords":   ["tragic", "loss of loved one", "suicide"],
    },
    "drama_family": {
        "label":      "👨‍👩‍👧 Dysfunktionelle familier",
        "plex_genre": "Drama",
        "keywords":   ["family relationships", "sibling relationship"],
    },
    "drama_love": {
        "label":      "❤️ Klinisk sukkerchok",
        "plex_genre": "Romantik",
        "keywords":   ["love", "romantic"],
    },

    # ──── 7. HOLD UNGERNE I RO-SKUFFEN ────────────────────────────────────────
    "family_cartoon": {
        "label":      "🎨 Computeranimeret Kaos",
        "plex_genre": "Animation",
        "keywords":   ["cartoon", "live action and animation"],
    },
    "family_christmas": {
        "label":      "🎄 Julestemning på dåse",
        "plex_genre": None,
        "keywords":   ["christmas", "holiday"],
    },
    "family_animal": {
        "label":      "🐶 Talende dyr & Pels",
        "plex_genre": None,
        "keywords":   ["dog", "anthropomorphism"],
    },

    # ──── 8. FAKTISK SKET I VIRKELIGHEDEN-SKUFFEN ─────────────────────────────
    "true_story": {
        "label":      "📖 Reality check: Det er sket!",
        "plex_genre": None,
        "keywords":   ["based on true story"],
    },
    "true_biography": {
        "label":      "👤 Noget om en berømthed",
        "plex_genre": None,
        "keywords":   ["biography"],
    },
    "true_wwii": {
        "label":      "🪖 Nazister der får tæv",
        "plex_genre": None,
        "keywords":   ["world war ii"],
    },

    # ──── 9. DET MÆRKELIGE & SÆRLIGE-SKUFFEN ──────────────────────────────────
    "special_revenge": {
        "label":      "⚔️ John Wick-syndromet",
        "plex_genre": None,
        "keywords":   ["revenge"],
    },
    "special_musical": {
        "label":      "🎵 Folk der pludselig synger",
        "plex_genre": None,
        "keywords":   ["musical"],
    },
    "special_lgbt": {
        "label":      "🌈 Pride & Regnbuer",
        "plex_genre": None,
        "keywords":   ["lgbt", "gay theme"],
    },
    "special_sports": {
        "label":      "⚽ Underdoggen vinder (måske)",
        "plex_genre": None,
        "keywords":   ["sports", "boxing", "basketball"],
    },
    "special_spy": {
        "label":      "🕴️ Martinis & Hemmelige agenter",
        "plex_genre": None,
        "keywords":   ["spy", "espionage"],
    },
    "special_indie": {
        "label":      "🎬 Film som snobberne elsker",
        "plex_genre": None,
        "keywords":   ["independent film", "cult"],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# KATEGORIER (9 hovedkasser → subgenre-IDs)
# ══════════════════════════════════════════════════════════════════════════════
#
# Hver kategori vises som én knap i Watch Flow trin 2.
# Når brugeren trykker, vises subgenrene som knapper i trin 3.

SUBGENRE_CATEGORIES: dict[str, dict] = {
    "comedy": {
        "label":     "😂 Flad af Grin",
        "subgenres": ["comedy_dark", "comedy_romcom", "comedy_standup", "comedy_satire"],
    },
    "action": {
        "label":     "💥 Eksplosioner & Testosteron",
        "subgenres": ["action_superhero", "action_martial", "action_survival", "action_roadtrip"],
    },
    "horror": {
        "label":     "😱 Skift Underbukser Bagefter",
        "subgenres": ["horror_psycho", "horror_slasher", "horror_creature", "horror_supernatural"],
    },
    "crime": {
        "label":     "🕵️ Hvem Gjorde Det?",
        "subgenres": ["crime_heist", "crime_noir", "crime_serialkiller", "crime_mafia"],
    },
    "scifi": {
        "label":     "🛸 Lasere & Foliehatte",
        "subgenres": ["scifi_time", "scifi_dystopia", "scifi_alien", "fantasy_magic"],
    },
    "drama": {
        "label":     "😭 Find Kleenex Frem",
        "subgenres": ["drama_youth", "drama_tearjerker", "drama_family", "drama_love"],
    },
    "family": {
        "label":     "🧸 Hold Ungerne I Ro",
        "subgenres": ["family_cartoon", "family_christmas", "family_animal"],
    },
    "true": {
        "label":     "🧠 Faktisk Sket I Virkeligheden",
        "subgenres": ["true_story", "true_biography", "true_wwii"],
    },
    "special": {
        "label":     "🎭 Det Mærkelige & Særlige",
        "subgenres": [
            "special_revenge", "special_musical", "special_lgbt",
            "special_sports", "special_spy", "special_indie",
        ],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# Lookup-funktioner
# ══════════════════════════════════════════════════════════════════════════════

def get_subgenre(subgenre_id: str) -> dict | None:
    """
    Hent fuld subgenre-info ved ID.

    Returnerer:
      {"id": "...", "label": "...", "plex_genre": "..." | None, "keywords": [...]}
      eller None hvis ID'et ikke findes.
    """
    sub = SUBGENRES.get(subgenre_id)
    if sub is None:
        return None
    return {"id": subgenre_id, **sub}


def get_category(category_id: str) -> dict | None:
    """
    Hent kategori-info inkl. udfoldet liste af subgenrer.

    Returnerer:
      {
        "id": "comedy",
        "label": "😂 Flad af Grin",
        "subgenres": [
          {"id": "comedy_dark", "label": "🖤 Kulsort & Absurd", ...},
          ...
        ],
      }
      eller None hvis kategorien ikke findes.
    """
    cat = SUBGENRE_CATEGORIES.get(category_id)
    if cat is None:
        return None

    subgenres = []
    for sub_id in cat["subgenres"]:
        sub = get_subgenre(sub_id)
        if sub is not None:
            subgenres.append(sub)
        else:
            logger.warning("Subgenre '%s' refereret af kategori '%s' findes ikke",
                           sub_id, category_id)

    return {
        "id":        category_id,
        "label":     cat["label"],
        "subgenres": subgenres,
    }


def get_all_categories() -> list[dict]:
    """Returnér alle 9 kategorier i defineret rækkefølge — brugt af UI."""
    return [
        get_category(cat_id)
        for cat_id in SUBGENRE_CATEGORIES
        if get_category(cat_id) is not None
    ]


def get_category_for_subgenre(subgenre_id: str) -> str | None:
    """
    Find hvilken kategori en subgenre tilhører.
    Bruges fx ved breadcrumb-navigation eller logging.
    """
    for cat_id, cat in SUBGENRE_CATEGORIES.items():
        if subgenre_id in cat["subgenres"]:
            return cat_id
    return None


def list_subgenre_ids() -> list[str]:
    """Returnér alle subgenre-IDs — brugt til validering."""
    return list(SUBGENRES.keys())


def validate_subgenre_id(subgenre_id: str) -> bool:
    """True hvis subgenre_id eksisterer i kataloget."""
    return subgenre_id in SUBGENRES


# ══════════════════════════════════════════════════════════════════════════════
# Self-check: kør ved import for at fange fejl tidligt
# ══════════════════════════════════════════════════════════════════════════════

def _self_check() -> None:
    """Verificerer at SUBGENRES og SUBGENRE_CATEGORIES er konsistente."""
    # 1. Alle subgenre-IDs i kategorierne skal eksistere i SUBGENRES
    for cat_id, cat in SUBGENRE_CATEGORIES.items():
        for sub_id in cat["subgenres"]:
            if sub_id not in SUBGENRES:
                raise RuntimeError(
                    f"subgenre_service self-check fejlede: kategori '{cat_id}' "
                    f"refererer subgenre '{sub_id}' som ikke findes i SUBGENRES"
                )

    # 2. Alle subgenrer skal være tildelt EN kategori (ingen forældreløse)
    assigned = set()
    for cat in SUBGENRE_CATEGORIES.values():
        assigned.update(cat["subgenres"])
    orphans = set(SUBGENRES.keys()) - assigned
    if orphans:
        logger.warning("Subgenrer uden kategori: %s", orphans)

    # 3. Hver subgenre skal have mindst ét keyword
    for sub_id, sub in SUBGENRES.items():
        if not sub.get("keywords"):
            raise RuntimeError(
                f"subgenre_service self-check fejlede: '{sub_id}' har ingen keywords"
            )

    logger.info(
        "subgenre_service self-check OK: %d subgenrer i %d kategorier",
        len(SUBGENRES), len(SUBGENRE_CATEGORIES),
    )


# Kør self-check ved import
_self_check()