"""
services/subgenre_service.py - Subgenre catalog and lookup helpers.

CHANGES (v0.1.0 — Etape 1 af subgenre-projekt):
  - NY SERVICE: Definerer SUBGENRES dictionary (35 subgenrer i 9 kasser)
    og hjælpefunktioner til lookup fra Watch Flow UI og find_unwatched_v2.
  - Hver subgenre har:
      * id          — unik identifier (bruges i callback_data + DB-queries)
      * label       — dansk navn med emoji (vises på knap)
      * plex_genre  — krævet Plex-genre ("dørmand", None hvis ingen)
      * keywords    — TMDB keywords der matcher (OR-logik)
  - SUBGENRE_CATEGORIES: 9 hovedkasser → liste af subgenre-IDs.
  - Designet er datadrevet baseret på keywords_movie_min5 dump (6.588 film).

DESIGN-PRINCIPPER:
  - "Hybrid keyword + Plex-genre" — Plex-genren fungerer som dørmand for at
    sortere falske positives fra (fx 'murder' i en romantisk komedie).
  - OR-logik internt: en film skal matche MINDST ÉN keyword i subgenrens
    liste. Det giver robuste puljer (alternativet AND ville være for strikt).
  - Engelske TMDB keywords for konsistens med find_unwatched_v2 SQL-queries.
  - Danske subgenre-labels for venlig UX.
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
#   label          → vises på knap i Watch Flow (dansk + emoji)
#   plex_genre     → krævet Plex-genre ("dørmand"), None hvis ingen
#   keywords       → TMDB keywords (engelsk, OR-match)
#
# OR-logik internt: film matcher hvis MINDST ÉN keyword er til stede.
# Plex-genre er AND-tjek hvis sat: film SKAL have den genre PLUS mindst én keyword.
#
# Datadrevet ud fra /top_keywords movie all 5 dump:
#   Hver subgenre er valideret til at have minimum ~30 matches.

SUBGENRES: dict[str, dict] = {

    # ──── 1. KOMEDIE-SKUFFEN ──────────────────────────────────────────────────
    "comedy_dark": {
        "label":      "🖤 Kulsort & Absurd",
        "plex_genre": "Komedie",
        "keywords":   ["dark comedy", "absurd"],
    },
    "comedy_romcom": {
        "label":      "💕 Rom-Com",
        "plex_genre": "Komedie",
        "keywords":   ["romcom", "romantic"],
    },
    "comedy_standup": {
        "label":      "🎤 Stand-up",
        "plex_genre": "Komedie",
        "keywords":   ["stand-up comedy"],
    },
    "comedy_satire": {
        "label":      "🤪 Slapstick & Parodi",
        "plex_genre": "Komedie",
        "keywords":   ["satire", "hilarious"],
    },

    # ──── 2. ACTION & EVENTYR-SKUFFEN ─────────────────────────────────────────
    "action_superhero": {
        "label":      "🦸 Superhelte",
        "plex_genre": None,
        "keywords":   ["superhero", "based on comic"],
    },
    "action_martial": {
        "label":      "🥋 Slå på tæven",
        "plex_genre": "Action",
        "keywords":   ["martial arts", "shootout"],
    },
    "action_survival": {
        "label":      "🏃 Kampen for overlevelse",
        "plex_genre": "Action",
        "keywords":   ["survival", "escape"],
    },
    "action_roadtrip": {
        "label":      "🚗 Ud på landevejen",
        "plex_genre": None,
        "keywords":   ["road trip"],
    },

    # ──── 3. GYS, SPÆNDING & KRYB-SKUFFEN ─────────────────────────────────────
    "horror_psycho": {
        "label":      "🧠 Psykologisk Terror",
        "plex_genre": "Thriller",
        "keywords":   ["psychological thriller"],
    },
    "horror_slasher": {
        "label":      "🪓 Slasher & Blod",
        "plex_genre": "Gyser",
        "keywords":   ["slasher", "gore", "murder"],
    },
    "horror_creature": {
        "label":      "🧟 Monstre & Zombier",
        "plex_genre": "Gyser",
        "keywords":   ["zombie", "monster", "creature"],
    },
    "horror_supernatural": {
        "label":      "👻 Det Overnaturlige",
        "plex_genre": "Gyser",
        "keywords":   ["supernatural horror", "witch"],
    },

    # ──── 4. KRIMI & MYSTERIUM-SKUFFEN ────────────────────────────────────────
    "crime_heist": {
        "label":      "💰 Det Store Kup",
        "plex_genre": None,
        "keywords":   ["heist", "bank robbery"],
    },
    "crime_noir": {
        "label":      "🚬 Neo-Noir & Detektiver",
        "plex_genre": None,
        "keywords":   ["neo-noir", "detective", "investigation"],
    },
    "crime_serialkiller": {
        "label":      "🩸 Seriemordere",
        "plex_genre": None,
        "keywords":   ["serial killer", "psychopath"],
    },
    "crime_mafia": {
        "label":      "🕴️ Mafia & Gangstere",
        "plex_genre": "Kriminalitet",
        "keywords":   ["gangster", "police"],
    },

    # ──── 5. SCI-FI & FANTASY-SKUFFEN ─────────────────────────────────────────
    "scifi_time": {
        "label":      "🕰️ Tidsrejser",
        "plex_genre": None,
        "keywords":   ["time travel"],
    },
    "scifi_dystopia": {
        "label":      "🏚️ Dystopi & Undergang",
        "plex_genre": None,
        # FIX: TMDB bruger "post-apocalyptic future" (53 hits), ikke "post-apocalyptic" (0 hits)
        "keywords":   ["dystopia", "post-apocalyptic future", "apocalypse"],
    },
    "scifi_alien": {
        "label":      "👽 Aliens & Rummet",
        "plex_genre": None,
        "keywords":   ["alien"],
    },
    "fantasy_magic": {
        "label":      "🧙 Magi & Trolddom",
        "plex_genre": "Fantasy",
        "keywords":   ["magic", "witch"],
    },

    # ──── 6. DE STORE FØLELSER-SKUFFEN ────────────────────────────────────────
    "drama_youth": {
        "label":      "🎒 Ungdom & Opvækst",
        "plex_genre": None,
        "keywords":   ["coming of age", "high school", "teenager"],
    },
    "drama_tearjerker": {
        "label":      "💔 Frem med lommetørklædet",
        "plex_genre": "Drama",
        "keywords":   ["tragic", "loss of loved one", "suicide"],
    },
    "drama_family": {
        "label":      "👨‍👩‍👧 Familiedrama",
        "plex_genre": "Drama",
        "keywords":   ["family relationships", "sibling relationship"],
    },
    "drama_love": {
        "label":      "❤️ Episk Kærlighed",
        "plex_genre": "Romantik",
        "keywords":   ["love", "romantic"],
    },

    # ──── 7. FAMILIE & ANIMATION-SKUFFEN ──────────────────────────────────────
    "family_cartoon": {
        "label":      "🎨 Ren Tegnefilm",
        "plex_genre": "Animation",
        "keywords":   ["cartoon", "live action and animation"],
    },
    "family_christmas": {
        "label":      "🎄 Julehygge",
        "plex_genre": None,
        "keywords":   ["christmas", "holiday"],
    },
    "family_animal": {
        "label":      "🐶 Dyr i hovedrollen",
        "plex_genre": None,
        "keywords":   ["dog", "anthropomorphism"],
    },

    # ──── 8. BASERET PÅ VIRKELIGHEDEN-SKUFFEN ─────────────────────────────────
    "true_story": {
        "label":      "📖 Sande Historier",
        "plex_genre": None,
        "keywords":   ["based on true story"],
    },
    "true_biography": {
        "label":      "👤 Det store portræt",
        "plex_genre": None,
        "keywords":   ["biography"],
    },
    "true_wwii": {
        "label":      "🪖 Anden Verdenskrig",
        "plex_genre": None,
        "keywords":   ["world war ii"],
    },

    # ──── 9. SPECIELT-SKUFFEN ─────────────────────────────────────────────────
    "special_musical": {
        "label":      "🎵 Musicals",
        "plex_genre": None,
        "keywords":   ["musical"],
    },
    "special_lgbt": {
        "label":      "🌈 LGBT+",
        "plex_genre": None,
        "keywords":   ["lgbt", "gay theme"],
    },
    "special_sports": {
        "label":      "⚽ Sportsfilm",
        "plex_genre": None,
        "keywords":   ["sports", "boxing", "basketball"],
    },
    "special_revenge": {
        "label":      "⚔️ Hævn-historier",
        "plex_genre": None,
        "keywords":   ["revenge"],
    },
    "special_spy": {
        "label":      "🕴️ Spionage",
        "plex_genre": None,
        "keywords":   ["spy", "espionage"],
    },
    "special_indie": {
        "label":      "🎬 Indie-perler",
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
        "label":     "😂 Komedie",
        "subgenres": ["comedy_dark", "comedy_romcom", "comedy_standup", "comedy_satire"],
    },
    "action": {
        "label":     "💥 Action & Eventyr",
        "subgenres": ["action_superhero", "action_martial", "action_survival", "action_roadtrip"],
    },
    "horror": {
        "label":     "🔪 Gys, Spænding & Kryb",
        "subgenres": ["horror_psycho", "horror_slasher", "horror_creature", "horror_supernatural"],
    },
    "crime": {
        "label":     "🕵️ Krimi & Mysterium",
        "subgenres": ["crime_heist", "crime_noir", "crime_serialkiller", "crime_mafia"],
    },
    "scifi": {
        "label":     "🚀 Sci-Fi & Fantasy",
        "subgenres": ["scifi_time", "scifi_dystopia", "scifi_alien", "fantasy_magic"],
    },
    "drama": {
        "label":     "😭 De Store Følelser",
        "subgenres": ["drama_youth", "drama_tearjerker", "drama_family", "drama_love"],
    },
    "family": {
        "label":     "🧸 Familie & Animation",
        "subgenres": ["family_cartoon", "family_christmas", "family_animal"],
    },
    "true": {
        "label":     "🧠 Baseret på Virkeligheden",
        "subgenres": ["true_story", "true_biography", "true_wwii"],
    },
    "special": {
        "label":     "✨ Specielt",
        "subgenres": [
            "special_musical", "special_lgbt", "special_sports",
            "special_revenge", "special_spy", "special_indie",
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
        "label": "😂 Komedie",
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