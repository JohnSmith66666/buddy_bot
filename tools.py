"""
tools.py - Claude Tool Use definitions for Buddy.

CHANGES vs previous version:
  - check_franchise_status: tilføjet instruks om at bruge dette ved 'den
    seneste', 'den nyeste' eller 'næste' film i en serie, så Claude ikke
    gætter på en ældre film baseret på træningsdata.
"""

TOOLS = [
    # ── TMDB ──────────────────────────────────────────────────────────────────
    {
        "name": "search_media",
        "description": (
            "Soeg efter film og/eller TV-serier til informationsformaal. "
            "Brug dette til at besvare spoergsmaal om en titel — IKKE til bestilling. "
            "Til bestilling: tjek Plex, og svar derefter med SHOW_SEARCH_RESULTS-kommandoen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "media_type": {"type": "string", "enum": ["movie", "tv", "both"]},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_media_details",
        "description": (
            "Hent detaljerede oplysninger om en specifik film eller TV-serie via TMDB ID. "
            "Returnerer nu ogsaa trailer_url (YouTube-link) hvis tilgaengeligt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer"},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
            },
            "required": ["tmdb_id", "media_type"],
        },
    },
    {
        "name": "get_trending",
        "description": (
            "Hent de mest trendende titler globalt denne uge. "
            "Returnerer ALTID praecis 5 film og 5 serier som en struktureret dict: "
            "{\"movies\": [5 film], \"tv\": [5 serier]}. "
            "Begge kategorier hentes altid i eet kald. "
            "Efter du modtager resultatet, SKAL du tjekke alle 10 titler i Plex "
            "via check_plex_library (se Plex-tjek reglen i system-prompten)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_recommendations",
        "description": "Find film eller serier der ligner en specifik titel. Kraever TMDB ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer"},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
            },
            "required": ["tmdb_id", "media_type"],
        },
    },
    {
        "name": "get_watch_providers",
        "description": "Find danske streamingtjenester en titel er tilgaengelig paa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer"},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
            },
            "required": ["tmdb_id", "media_type"],
        },
    },
    {
        "name": "search_person",
        "description": "Soeg efter en skuespiller, instruktoer eller andet filmhold-medlem.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_person_filmography",
        "description": (
            "Hent den fulde filmografi for en person via TMDB person-ID. "
            "Returnerer ALLE film sorteret efter popularity (stoerste hits foerst) "
            "samt top 10 TV-serier. Brug dette til karriere-spoergsmaal generelt. "
            "Til fuld Plex-analyse med mangler og statistik: brug search_plex_by_actor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"person_id": {"type": "integer"}},
            "required": ["person_id"],
        },
    },
    {
        "name": "get_now_playing",
        "description": "Hent de mest populaere film der korer i biografen lige nu (dansk region).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_upcoming",
        "description": "Hent de mest populaere kommende film der snart udkommer i biografen (dansk region).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_web",
        "description": (
            "Soeg paa internettet efter information om tv-programmer, film og underholdning "
            "der ikke findes i filmdata-basen. "
            "Brug dette naar brugeren spørger om: "
            "(1) Dansk TV-indhold som 'Kontant', 'Toppen af poppen', 'Nak & Aed', "
            "'Dansker i verden', 'DR-dokumentarer' eller andre lokale programmer der "
            "ikke er registreret i TMDB. "
            "(2) Plot-resumeer eller handling af specifikke afsnit af en serie. "
            "(3) Anmeldelser eller baggrundsstof om specifikke film og TV-programmer. "
            "Formuler query som et naturligt dansk spoergsmaal for bedste resultat. "
            "Returnerer answer (LLM-opsummering) og results (kildeliste med uddrag). "
            "VIGTIGT: Dette vaerktoej maa KUN bruges til foresproegsler relateret til "
            "tv-programmer, film, skuespillere og underholdning — isaer lokalt/dansk "
            "indhold som TMDB mangler. "
            "Det er STRENGT FORBUDT at kalde dette vaerktoej for at besvare generelle "
            "spoergsmaal om nyheder, fakta, vejret, opskrifter, sport, politik, historie, "
            "kodning eller andre emner der ikke har direkte med film og TV at goere. "
            "Brug IKKE dette til bestilling af film/serier eller Plex-relaterede spoergsmaal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Sogeforesproegslen — skal vaere relateret til film, TV eller underholdning."
                    ),
                },
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "Sogedybde. Brug 'basic' (standard). 'advanced' til svare emner.",
                },
            },
            "required": ["query"],
        },
    },

    # ── Plex ──────────────────────────────────────────────────────────────────
    {
        "name": "check_plex_library",
        "description": (
            "Tjek om en specifik titel allerede findes i Plex-biblioteket. "
            "SKAL altid kaldes foer en bestilling. "
            "Skal ogsaa kaldes for ALLE titler i en trending- eller anbefalingsliste "
            "foer svaret formuleres til brugeren (se system-prompten). "
            "Hvis 'found': fortael brugeren vi allerede har den og STOP (ved bestilling). "
            "Hvis 'missing': svar med SHOW_SEARCH_RESULTS-kommandoen (ved bestilling)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "year": {"type": "integer"},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
            },
            "required": ["title", "media_type"],
        },
    },
    {
        "name": "check_franchise_status",
        "description": (
            "Avanceret franchise-søgning: finder den officielle samling fra TMDB "
            "(f.eks. 'Avatar Collection', 'James Bond Collection', "
            "'Harry Potter Collection') og krydstjekker ALLE film mod Plex via "
            "GUID-matching (100% skudsikker) med fuzzy-matching som fallback. "
            "Brug dette NÅR brugeren spørger efter en franchise, samling eller film-serie — "
            "f.eks. 'hvilke Marvel-film har vi?', 'hvad mangler vi af Bond?', "
            "'vis mig Harry Potter-samlingen'. "
            "Brug OGSÅ dette værktøj, når brugeren spørger efter 'den seneste', "
            "'den nyeste' eller 'næste' film i en specifik serie (f.eks. 'den nyeste "
            "Avatar film' eller 'den seneste Batman'). Det sikrer, at du får hele "
            "tidslinjen fra TMDB og ikke ved en fejl gætter på en ældre film ud fra "
            "din forhåndsviden. "
            "Returnerer: collection_name, found_on_plex, missing_from_plex, counts. "
            "Input: kun keyword (f.eks. 'Avatar', 'Batman', 'James Bond')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Franchise- eller samlingsnavn, f.eks. 'Avatar', 'Batman', 'James Bond'.",
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "search_plex_by_actor",
        "description": (
            "Completionist skuespiller-analyse: slaar op BAADE lokalt paa Plex OG "
            "mod skuespillerens FULDE filmografi fra TMDB. "
            "Returnerer: total_movies, owned_movies, found_on_plex, top_5_missing. "
            "Brug dette NÅR brugeren spørger om en skuespiller — baade "
            "'hvilke film har vi med X?', 'hvad mangler vi af X?' og "
            "'hvor mange af X's film har vi?'. "
            "Input: actor_name (skuespillerens navn)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "actor_name": {
                    "type": "string",
                    "description": "Skuespillerens navn, f.eks. 'Robert Downey Jr.'",
                },
            },
            "required": ["actor_name"],
        },
    },
    {
        "name": "get_on_deck",
        "description": "Hent brugerens 'Se videre'-liste — titler paabegyndt men ikke faerdigset.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_plex_metadata",
        "description": "Hent tekniske specs for en Plex-titel: oplosning, HDR, codec, lyd.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "year": {"type": "integer"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "find_unwatched",
        "description": "Find tilfaeldige usete film eller serier i Plex.",
        "input_schema": {
            "type": "object",
            "properties": {
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
                "genre": {"type": "string"},
            },
            "required": ["media_type"],
        },
    },
    {
        "name": "get_similar_in_library",
        "description": "Find titler i Plex der ligner en bestemt film eller serie.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "get_missing_from_collection",
        "description": (
            "Find hvad der mangler af en samling i Plex via simpel TMDB-søgning. "
            "Til avanceret krydstjek med GUID-matching: brug check_franchise_status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"collection_name": {"type": "string"}},
            "required": ["collection_name"],
        },
    },

    # ── Tautulli ──────────────────────────────────────────────────────────────
    {
        "name": "get_popular_on_plex",
        "description": (
            "Hent de mest populaere film og serier paa Plex-serveren. "
            "days=30 for seneste maaned (standard), days=365 for seneste aar. "
            "days=0 for 'all time' / 'nogensinde'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Antal dage. Standard 30. Brug 0 for all-time.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_user_watch_stats",
        "description": (
            "Hent brugerens personlige statistik — seertid og top 5 film/serier. "
            "days=365 standard. days=0 for 'all time' / 'nogensinde' / 'absolut mest sete'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Antal dage. Standard 365. Brug 0 for all-time.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_user_history",
        "description": (
            "Soeg i brugerens egen afspilningshistorik. "
            "media_type='movie' for senest sete film, 'episode' for serier."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Valgfri titel-søgning i historikken.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "episode"],
                    "description": "Filtrer paa type: 'movie' eller 'episode'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_recently_added",
        "description": (
            "Hent det nyeste indhold tilfojet til Plex. "
            "Brug naar brugeren siger 'nyt', 'tilfojet', 'landet', 'kommet'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": [],
        },
    },
]