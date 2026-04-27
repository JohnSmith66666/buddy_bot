"""
services/subgenre_service.py - Subgenre catalog and lookup helpers.

CHANGES (v0.3.2 — Master fix for 8 højrisiko TV-subgenrer):
  - DATADRIVET FIX: 8 subgenrer havde adjektiv-keywords ('hilarious', 'witty',
    'dramatic') eller decade-keywords ('1980s', 'nostalgic') der gav misfit-
    matches. Brugeren havde fx The Mentalist (Crime+Drama) i tv_sitcom og
    Cobra Kai i tv_period_drama.

  - LØSNING — Hybrid pakke:
    * tv_period_drama: Fjern decade-keywords (1980s/1990s/etc + nostalgic),
      tilføj plex_genre='Drama' som dørmand
    * tv_sitcom: Fjern adjektiv-keywords (hilarious/amused/witty),
      tilføj plex_genre='Komedie'
    * tv_dark_comedy: Tilføj plex_genre='Komedie' (absurd+satire trigger ikke
      længere udenfor komedie-genren)
    * tv_emotional_drama: Tilføj plex_genre='Drama' (alle keywords er adjektiver,
      så plex_genre er kritisk dørmand)
    * tv_psychological_thriller: Tilføj plex_genre='Thriller' (intense/tense
      er adjektiver der lukker uden Plex-genre filter)
    * tv_animation: Tilføj plex_genre='Animation' (cartoon-keyword fanger ellers
      børnefilm)
    * tv_romcom: Tilføj plex_genre='Romantik' (love/romance er for brede)
    * tv_family_drama: Tilføj plex_genre='Drama' ('family' er meget bredt)

  - BEVAREDE BESLUTNINGER (de 19 andre subgenrer er UÆNDREDE):
    * tv_book_adaptation/tv_comic_adaptation/tv_miniseries/tv_lgbt:
      INGEN plex_genre passer (cross-genre) — keywords er specifikke nok
    * tv_murder_mystery/tv_horror_creature/tv_war/tv_medical/tv_legal/etc:
      Keywords er entitet-specifikke (zombie, lawyer, world war ii)

  - FORVENTET EFFEKT: ~20-30% færre hits per fixed subgenre, men ~95% match-
    præcision. Walking Dead bevares i tv_horror_creature, Sherlock i
    tv_book_adaptation, etc. — fordi vi IKKE rør deres subgenrer.

UNCHANGED (v0.3.0 — TV-subgenrer tilføjet, media-aware arkitektur):
  - SUBGENRES_TV med 27 datadrevne TV-subgenrer baseret på analyse af
    1.110 fetched serier i tmdb_metadata. Alle 27 har data.
  - SUBGENRE_CATEGORIES_TV med 9 hovedkategorier til TV-flow.
  - Alle helper-funktioner (get_subgenre, get_category, etc.) tager nu
    en valgfri media_type parameter ('movie' eller 'tv'). Default: 'movie'
    for at sikre bagudkompatibilitet med eksisterende kode.
  - BAGUDKOMPATIBILITET: SUBGENRES og SUBGENRE_CATEGORIES (uden _MOVIE suffix)
    bevares som aliases der peger på film-katalogerne.
  - SELF-CHECK: Validerer både film- og TV-katalogerne ved import.

UNCHANGED (v0.2.0 — sjove danske labels for film):
  - 36 film-subgenrer i 9 kategorier baseret på datadrevet analyse af
    keywords_movie_min5 dump (6.588 film).
  - Hybrid keyword + Plex-genre logik (OR mellem keywords, AND med plex_genre).

DESIGN-PRINCIPPER:
  - Sjove danske labels med personlighed.
  - Engelske TMDB keywords for konsistens med find_titles_by_subgenre SQL.
  - Hver subgenre har minimum ~10-15 matches i biblioteket (datavalideret).
  - TV-subgenrer er ALDRIG identiske med film-subgenrer (forskellige keywords
    fordi serier bruger andre TMDB-tags end film).
  - PLEX_GENRE DØRMAND: Tilføjes KUN når keywords indeholder adjektiver
    eller meget brede termer der ellers giver false positives. Cross-genre
    subgenrer (book_adaptation, miniseries, lgbt) får ALDRIG plex_genre.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SUBGENRE-KATALOG — FILM (uændret fra v0.2.0)
# ══════════════════════════════════════════════════════════════════════════════
#
# Format per subgenre:
#   id             → unik ID, må kun indeholde lowercase + underscore
#   label          → sjovt dansk navn med emoji (vises på knap)
#   plex_genre     → krævet Plex-genre ("dørmand"), None hvis ingen
#   keywords       → TMDB keywords (engelsk, OR-match)

SUBGENRES_MOVIE: dict[str, dict] = {

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


SUBGENRE_CATEGORIES_MOVIE: dict[str, dict] = {
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
# SUBGENRE-KATALOG — TV (NY i v0.3.0, justeret i v0.3.2 — Master Fix)
# ══════════════════════════════════════════════════════════════════════════════
#
# 27 datadrevne TV-subgenrer baseret på analyse af 1.110 fetched serier
# i tmdb_metadata. Alle har minimum 10+ hits, ingen tomme.
#
# v0.3.2 ændringer markeret med "FIX (v0.3.2)" kommentarer.
#
# DESIGN-NOTE: plex_genre er KUN tilføjet hvor keywords er adjektiver eller
# meget brede termer. Cross-genre subgenrer (book_adaptation, miniseries,
# lgbt) har ALDRIG plex_genre fordi de findes i alle Plex-genrer.

SUBGENRES_TV: dict[str, dict] = {

    # ──── 1. KRIMINALITET & MYSTIK (4 subgenrer) ──────────────────────────────
    "tv_murder_mystery": {
        "label":      "🔪 Hvem Gjorde Det?",
        "plex_genre": None,  # Keywords er specifikke nok (serial killer, homicide)
        "keywords":   [
            "murder", "murder investigation", "murder mystery",
            "serial killer", "investigation", "mysterious",
        ],
    },
    "tv_true_crime": {
        "label":      "🎙️ True Crime — Reality check",
        "plex_genre": None,  # 'true crime' er meget specifik
        "keywords":   ["true crime", "based on true story"],
    },
    "tv_police_procedural": {
        "label":      "👮 Afspærringstape & Sirener",
        "plex_genre": None,  # 'police', 'fbi' er specifikke entiteter
        "keywords":   [
            "police", "police detective", "police procedural",
            "police officer", "fbi", "fbi agent", "criminal",
        ],
    },
    "tv_neo_noir": {
        "label":      "🕶️ Neo-noir & Detektiv",
        "plex_genre": None,  # 'neo-noir', 'homicide detective' er specifikke
        "keywords":   ["detective", "neo-noir", "homicide detective"],
    },

    # ──── 2. KOMEDIE (3 subgenrer) ────────────────────────────────────────────
    "tv_sitcom": {
        "label":      "🛋️ Sofa-Komedier",
        # FIX (v0.3.2): Tilføjet plex_genre='Komedie' + fjernet adjektiv-
        # keywords (hilarious, amused, witty). Bekræftet problem: The Mentalist
        # blev fanget via 'witty' selvom det er Crime+Drama, ikke Komedie.
        "plex_genre": "Komedie",
        "keywords":   [
            "sitcom", "workplace comedy", "dramedy", "mockumentary",
        ],
    },
    "tv_dark_comedy": {
        "label":      "🖤 Mørk Komedie",
        # FIX (v0.3.2): Tilføjet plex_genre='Komedie'. 'absurd' og 'satire'
        # er adjektiver der ellers fanger dokumentarer og dramaer.
        "plex_genre": "Komedie",
        "keywords":   ["dark comedy", "absurd", "satire"],
    },
    "tv_romcom": {
        "label":      "💕 Romantik & Romcom",
        # FIX (v0.3.2): Tilføjet plex_genre='Romantik'. 'love' og 'romance'
        # er for brede til at stå alene.
        "plex_genre": "Romantik",
        "keywords":   ["romance", "romcom", "love", "marriage", "teenage romance"],
    },

    # ──── 3. DRAMA (3 subgenrer) ──────────────────────────────────────────────
    "tv_family_drama": {
        "label":      "👨‍👩‍👧 Familie-drama",
        # FIX (v0.3.2): Tilføjet plex_genre='Drama'. 'family' alene fanger
        # børneserier og familieshows blandet.
        "plex_genre": "Drama",
        "keywords":   [
            "family", "family relationships", "family drama",
            "dysfunctional family",
        ],
    },
    "tv_teen_drama": {
        "label":      "🎓 Teen Drama & Coming of Age",
        "plex_genre": None,  # 'teen drama', 'high school' er specifikke nok
        "keywords":   [
            "teen drama", "high school", "coming of age",
            "teenager", "based on young adult novel",
        ],
    },
    "tv_emotional_drama": {
        "label":      "💔 Tunge Følelser",
        # FIX (v0.3.2): Tilføjet plex_genre='Drama'. ALLE keywords er
        # adjektiver (dramatic, intimate, complex, etc) — 100% risky-score.
        # Plex-genre er kritisk dørmand for at forhindre over-matching.
        "plex_genre": "Drama",
        "keywords":   [
            "dramatic", "intimate", "complex",
            "thoughtful", "introspective", "tragic",
        ],
    },

    # ──── 4. GYS & THRILLER (3 subgenrer) ─────────────────────────────────────
    "tv_horror_supernatural": {
        "label":      "👻 Spøgelser & Overnaturlig",
        # Keywords er specifikke (ghost, demon, witch) — bevarer breddi
        # for shows som Buffy/Charmed der er Drama+Fantasy
        "plex_genre": None,
        "keywords":   [
            "supernatural", "supernatural horror", "ghost",
            "demon", "demon hunter", "demonic possession",
            "haunted house", "paranormal phenomena", "witch", "witchcraft",
        ],
    },
    "tv_horror_creature": {
        "label":      "🧟 Monstre, Blod & Gys",
        # Keywords er specifikke entiteter (zombie, vampire, monster)
        # Bevarer Walking Dead (Drama+Sci-Fi), True Blood (Drama+Fantasy)
        "plex_genre": None,
        "keywords":   [
            "zombie", "zombie apocalypse", "monster", "creature",
            "vampire", "werewolf", "horror", "gore", "slasher",
        ],
    },
    "tv_psychological_thriller": {
        "label":      "🧠 Psykologisk Thriller",
        # FIX (v0.3.2): Tilføjet plex_genre='Thriller'. 'thriller', 'tense',
        # 'intense', 'suspenseful' er adjektiver der ellers giver false positives.
        "plex_genre": "Thriller",
        "keywords":   [
            "psychological thriller", "psychological horror", "psychopath",
            "thriller", "suspenseful", "tense", "intense",
        ],
    },

    # ──── 5. SCI-FI & FANTASY (3 subgenrer) ───────────────────────────────────
    "tv_scifi_dystopia": {
        "label":      "🏚️ Dystopi & Apokalypse",
        "plex_genre": None,  # 'dystopia', 'apocalypse' er specifikke
        "keywords":   ["dystopia", "post-apocalyptic future", "apocalypse", "survival"],
    },
    "tv_scifi_space": {
        "label":      "🛸 Sci-fi & Rumrejser",
        "plex_genre": None,  # 'space opera', 'space travel' er specifikke
        "keywords":   [
            "science fiction", "space", "space travel",
            "space opera", "alien", "time travel",
        ],
    },
    "tv_fantasy": {
        "label":      "🧙 Fantasy & Magi",
        "plex_genre": None,  # 'fantasy world', 'magic' er specifikke
        "keywords":   ["fantasy world", "dark fantasy", "magic"],
    },

    # ──── 6. ADAPTATIONER (2 subgenrer) ───────────────────────────────────────
    "tv_book_adaptation": {
        "label":      "📖 Bogadaptationer",
        # INGEN plex_genre — bog-adaptationer findes i ALLE genrer:
        # GoT (Fantasy), Sherlock (Mystery), Bridgerton (Romance), Dexter (Crime)
        "plex_genre": None,
        "keywords":   ["based on novel or book", "based on young adult novel"],
    },
    "tv_comic_adaptation": {
        "label":      "🦸 Spandex & Superkræfter",
        # INGEN plex_genre — comic-adaptationer er Action+Sci-Fi+Drama blandet
        "plex_genre": None,
        "keywords":   [
            "superhero", "based on comic", "based on graphic novel",
            "marvel cinematic universe (mcu)", "super power",
        ],
    },

    # ──── 7. TV-FORMATER (3 subgenrer) ────────────────────────────────────────
    "tv_miniseries": {
        "label":      "🎬 Hurtigt Overstået (Miniserier)",
        # INGEN plex_genre — miniseries er FORMAT, ikke genre
        # Chernobyl (Drama), Mare of Easttown (Crime), Queen's Gambit (Drama)
        "plex_genre": None,
        "keywords":   ["miniseries"],
    },
    "tv_animation": {
        "label":      "🎨 Tegnet for voksne",
        # FIX (v0.3.2): Tilføjet plex_genre='Animation'. 'cartoon' alene
        # fanger ellers børneserier — Animation-genre + 'adult animation'
        # keyword sikrer voksen-content.
        "plex_genre": "Animation",
        "keywords":   ["adult animation", "cartoon"],
    },
    "tv_reality": {
        "label":      "🏆 Reality TV",
        "plex_genre": None,  # Plex har sjældent 'Reality' som genre, keywords er specifikke
        "keywords":   [
            "reality competition", "reality tv", "alternative reality",
            "music documentary", "sports documentary", "biographical documentary",
        ],
    },

    # ──── 8. HISTORISK (2 subgenrer) ──────────────────────────────────────────
    "tv_period_drama": {
        "label":      "🏛️ Korsetter & Gamle Dage",
        # FIX (v0.3.2): Decade-keywords FJERNET (1980s, 1990s, 1970s, 1960s,
        # 1950s, nostalgic) - de gav mismatches med moderne serier sat i
        # fortiden (Cobra Kai, Stranger Things, GLOW). Tilføjet plex_genre=
        # 'Drama' som ekstra dørmand for at sikre det er ægte period drama.
        "plex_genre": "Drama",
        "keywords":   [
            "period drama", "period piece", "historical drama",
            "historical", "historical fiction", "19th century",
        ],
    },
    "tv_war": {
        "label":      "🪖 Til Fronten!",
        "plex_genre": None,  # 'world war ii' er meget specifik
        "keywords":   ["world war ii", "war", "military"],
    },

    # ──── 9. PÅ ARBEJDE (3 subgenrer) ─────────────────────────────────────────
    "tv_medical": {
        "label":      "🏥 Medicinsk Drama",
        "plex_genre": None,  # 'medical drama', 'hospital', 'doctor' er specifikke
        "keywords":   [
            "medical drama", "hospital", "doctor", "medical", "medical student",
        ],
    },
    "tv_legal": {
        "label":      "⚖️ I Rettens Navn",
        "plex_genre": None,  # 'lawyer', 'courtroom' er specifikke
        "keywords":   [
            "lawyer", "criminal lawyer", "courtroom drama",
            "court case", "courtroom",
        ],
    },
    "tv_spy": {
        "label":      "🕴️ Spioner & Konspiration",
        "plex_genre": None,  # 'cia', 'espionage' er specifikke
        "keywords":   [
            "central intelligence agency (cia)", "conspiracy", "espionage",
        ],
    },

    # ──── 10. SPECIELT (1 subgenre — kun LGBT) ───────────────────────────────
    "tv_lgbt": {
        "label":      "🌈 Pride & Regnbuer",
        # INGEN plex_genre — LGBT er TEMA, ikke genre
        # Pose (Drama), Heartstopper (Romance), Pose (Drama+Music)
        "plex_genre": None,
        "keywords":   ["lgbt", "gay theme"],
    },
}


SUBGENRE_CATEGORIES_TV: dict[str, dict] = {
    "tv_crime": {
        "label":     "🕵️ Kriminalitet & Mystik",
        "subgenres": [
            "tv_murder_mystery", "tv_true_crime",
            "tv_police_procedural", "tv_neo_noir",
        ],
    },
    "tv_comedy": {
        "label":     "😂 Komedie",
        "subgenres": ["tv_sitcom", "tv_dark_comedy", "tv_romcom"],
    },
    "tv_drama": {
        "label":     "😭 Drama",
        "subgenres": ["tv_family_drama", "tv_teen_drama", "tv_emotional_drama"],
    },
    "tv_horror": {
        "label":     "😱 Gys & Thriller",
        "subgenres": [
            "tv_horror_supernatural", "tv_horror_creature",
            "tv_psychological_thriller",
        ],
    },
    "tv_scifi": {
        "label":     "🛸 Sci-fi & Fantasy",
        "subgenres": ["tv_scifi_dystopia", "tv_scifi_space", "tv_fantasy"],
    },
    "tv_adaptations": {
        "label":     "📚 Adaptationer",
        "subgenres": ["tv_book_adaptation", "tv_comic_adaptation"],
    },
    "tv_formats": {
        "label":     "🎬 TV-Formater",
        "subgenres": ["tv_miniseries", "tv_animation", "tv_reality"],
    },
    "tv_historical": {
        "label":     "🏛️ Historisk",
        "subgenres": ["tv_period_drama", "tv_war"],
    },
    "tv_work": {
        "label":     "🎭 På Arbejde",
        "subgenres": ["tv_medical", "tv_legal", "tv_spy", "tv_lgbt"],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# BAGUDKOMPATIBILITET — Aliases til film-katalog
# ══════════════════════════════════════════════════════════════════════════════
#
# Eksisterende kode importerer SUBGENRES og SUBGENRE_CATEGORIES uden suffix.
# Vi bevarer dem som aliases til film-katalogerne så intet brækker.
# Ny kode bør bruge eksplicit _MOVIE eller _TV suffix.

SUBGENRES = SUBGENRES_MOVIE
SUBGENRE_CATEGORIES = SUBGENRE_CATEGORIES_MOVIE


# ══════════════════════════════════════════════════════════════════════════════
# Lookup-funktioner — media-aware
# ══════════════════════════════════════════════════════════════════════════════

def _select_catalog(media_type: str = "movie") -> tuple[dict, dict]:
    """
    Vælg det rigtige subgenre + kategori-katalog baseret på media_type.

    Returns:
      (SUBGENRES, SUBGENRE_CATEGORIES) tuple for det valgte media_type.

    Raises:
      ValueError hvis media_type ikke er 'movie' eller 'tv'.
    """
    if media_type == "tv":
        return SUBGENRES_TV, SUBGENRE_CATEGORIES_TV
    if media_type == "movie":
        return SUBGENRES_MOVIE, SUBGENRE_CATEGORIES_MOVIE
    raise ValueError(f"Ugyldig media_type: '{media_type}'. Skal være 'movie' eller 'tv'.")


def get_subgenre(subgenre_id: str, media_type: str = "movie") -> dict | None:
    """
    Hent fuld subgenre-info ved ID.

    Args:
      subgenre_id: Unikt ID, fx 'horror_slasher' eller 'tv_murder_mystery'.
      media_type:  'movie' (default) eller 'tv'.

    Returns:
      {"id": "...", "label": "...", "plex_genre": "..." | None, "keywords": [...]}
      eller None hvis ID'et ikke findes i det valgte katalog.

    AUTO-DETECT: Hvis subgenre_id starter med 'tv_' og media_type='movie' (default),
    forsøger vi automatisk at slå op i TV-kataloget. Det er en convenience for
    legacy-kald der ikke endnu er opdateret med media_type parameter.
    """
    # Auto-detect TV-subgenrer ved prefix
    if subgenre_id.startswith("tv_") and media_type == "movie":
        media_type = "tv"

    catalog, _ = _select_catalog(media_type)
    sub = catalog.get(subgenre_id)
    if sub is None:
        return None
    return {"id": subgenre_id, **sub}


def get_category(category_id: str, media_type: str = "movie") -> dict | None:
    """
    Hent kategori-info inkl. udfoldet liste af subgenrer.

    Args:
      category_id: Kategori-ID, fx 'comedy' eller 'tv_comedy'.
      media_type:  'movie' (default) eller 'tv'.

    Returns:
      {
        "id": "comedy",
        "label": "😂 Flad af Grin",
        "subgenres": [
          {"id": "comedy_dark", "label": "🖤 Kulsort & Absurd", ...},
          ...
        ],
      }
      eller None hvis kategorien ikke findes.

    AUTO-DETECT: Hvis category_id starter med 'tv_' og media_type='movie' (default),
    forsøger vi automatisk at slå op i TV-kataloget.
    """
    # Auto-detect TV-kategorier ved prefix
    if category_id.startswith("tv_") and media_type == "movie":
        media_type = "tv"

    _, categories = _select_catalog(media_type)
    cat = categories.get(category_id)
    if cat is None:
        return None

    subgenres = []
    for sub_id in cat["subgenres"]:
        sub = get_subgenre(sub_id, media_type=media_type)
        if sub is not None:
            subgenres.append(sub)
        else:
            logger.warning("Subgenre '%s' refereret af kategori '%s' (media=%s) findes ikke",
                           sub_id, category_id, media_type)

    return {
        "id":        category_id,
        "label":     cat["label"],
        "subgenres": subgenres,
    }


def get_all_categories(media_type: str = "movie") -> list[dict]:
    """
    Returnér alle kategorier i defineret rækkefølge — brugt af UI.

    Args:
      media_type: 'movie' (default, 9 kategorier) eller 'tv' (9 kategorier).
    """
    _, categories = _select_catalog(media_type)
    return [
        get_category(cat_id, media_type=media_type)
        for cat_id in categories
        if get_category(cat_id, media_type=media_type) is not None
    ]


def get_category_for_subgenre(subgenre_id: str, media_type: str = "movie") -> str | None:
    """
    Find hvilken kategori en subgenre tilhører.

    Args:
      subgenre_id: Subgenre-ID.
      media_type:  'movie' (default) eller 'tv'.

    Returns:
      category_id (string) eller None hvis subgenren ikke findes i kategorierne.

    AUTO-DETECT: Hvis subgenre_id starter med 'tv_', søges automatisk i
    TV-kataloget uanset hvad media_type er sat til.
    """
    if subgenre_id.startswith("tv_"):
        media_type = "tv"

    _, categories = _select_catalog(media_type)
    for cat_id, cat in categories.items():
        if subgenre_id in cat["subgenres"]:
            return cat_id
    return None


def list_subgenre_ids(media_type: str = "movie") -> list[str]:
    """
    Returnér alle subgenre-IDs — brugt til validering.

    Args:
      media_type: 'movie' (default), 'tv' eller 'all' for begge.
    """
    if media_type == "all":
        return list(SUBGENRES_MOVIE.keys()) + list(SUBGENRES_TV.keys())
    catalog, _ = _select_catalog(media_type)
    return list(catalog.keys())


def validate_subgenre_id(subgenre_id: str, media_type: str | None = None) -> bool:
    """
    True hvis subgenre_id eksisterer i et af katalogerne.

    Args:
      subgenre_id: Subgenre-ID at validere.
      media_type:  'movie', 'tv' eller None.
                   None (default) tjekker BEGGE kataloger for bagudkompatibilitet.
    """
    if media_type is None:
        # Tjek begge kataloger
        return subgenre_id in SUBGENRES_MOVIE or subgenre_id in SUBGENRES_TV
    catalog, _ = _select_catalog(media_type)
    return subgenre_id in catalog


def detect_media_type(subgenre_id: str) -> str | None:
    """
    Auto-detect hvilken media_type en subgenre tilhører.

    Returns:
      'movie' hvis subgenren findes i SUBGENRES_MOVIE,
      'tv'    hvis subgenren findes i SUBGENRES_TV,
      None    hvis subgenren ikke findes nogen steder.
    """
    if subgenre_id in SUBGENRES_TV:
        return "tv"
    if subgenre_id in SUBGENRES_MOVIE:
        return "movie"
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Self-check: kør ved import for at fange fejl tidligt
# ══════════════════════════════════════════════════════════════════════════════

def _self_check() -> None:
    """Verificerer at SUBGENRES_MOVIE og SUBGENRES_TV er konsistente."""

    for media_label, subgenres, categories in [
        ("MOVIE", SUBGENRES_MOVIE, SUBGENRE_CATEGORIES_MOVIE),
        ("TV",    SUBGENRES_TV,    SUBGENRE_CATEGORIES_TV),
    ]:
        # 1. Alle subgenre-IDs i kategorierne skal eksistere i SUBGENRES
        for cat_id, cat in categories.items():
            for sub_id in cat["subgenres"]:
                if sub_id not in subgenres:
                    raise RuntimeError(
                        f"subgenre_service [{media_label}] self-check fejlede: "
                        f"kategori '{cat_id}' refererer subgenre '{sub_id}' "
                        f"som ikke findes i SUBGENRES_{media_label}"
                    )

        # 2. Alle subgenrer skal være tildelt EN kategori (ingen forældreløse)
        assigned = set()
        for cat in categories.values():
            assigned.update(cat["subgenres"])
        orphans = set(subgenres.keys()) - assigned
        if orphans:
            logger.warning("[%s] Subgenrer uden kategori: %s", media_label, orphans)

        # 3. Hver subgenre skal have mindst ét keyword
        for sub_id, sub in subgenres.items():
            if not sub.get("keywords"):
                raise RuntimeError(
                    f"subgenre_service [{media_label}] self-check fejlede: "
                    f"'{sub_id}' har ingen keywords"
                )

    logger.info(
        "subgenre_service self-check OK: %d film-subgenrer i %d kategorier, "
        "%d TV-subgenrer i %d kategorier",
        len(SUBGENRES_MOVIE), len(SUBGENRE_CATEGORIES_MOVIE),
        len(SUBGENRES_TV),    len(SUBGENRE_CATEGORIES_TV),
    )


# Kør self-check ved import
_self_check()