"""
tools.py - All Claude Tool Use definitions for Buddy.

Adding a new tool: define it here, then add the handler in ai_handler.py.
"""

TOOLS = [
    # ── TMDB ──────────────────────────────────────────────────────────────────
    {
        "name": "search_media",
        "description": (
            "Soeg efter film og/eller TV-serier. "
            "Brug dette vaerktoej naar brugeren spoerger om en bestemt film eller serie, "
            "vil finde noget at se, eller naevner en titel. "
            "Returnerer op til 30 resultater per type med titel, aar, genre_ids og beskrivelse."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Soegetermet — typisk en titel eller nogleord."},
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv", "both"],
                    "description": "Brug 'movie' for film, 'tv' for serier, 'both' hvis det er uklart.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_media_details",
        "description": (
            "Hent detaljerede oplysninger om en specifik film eller TV-serie. "
            "Brug dette ALTID foer en Seerr-anmodning for at faa genre_ids, "
            "original_language og season_numbers. "
            "Kraever et TMDB ID fra search_media."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer", "description": "TMDB ID paa filmen eller serien."},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
            },
            "required": ["tmdb_id", "media_type"],
        },
    },
    {
        "name": "get_trending",
        "description": "Hent de mest populaere og trendende film og serier denne uge.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_recommendations",
        "description": (
            "Find film eller serier der ligner en specifik titel. "
            "Kraever et TMDB ID fra search_media."
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
        "name": "get_watch_providers",
        "description": (
            "Find ud af hvilke danske streamingtjenester en titel er tilgaengelig paa. "
            "Returnerer KUN danske udbydere (DK)."
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
        "name": "search_person",
        "description": (
            "Soeg efter en skuespiller, instruktoer eller andet filmhold-medlem. "
            "Brug dette naar brugeren naevner et personnavn eller spoerger om en persons karriere."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Personens navn."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_person_filmography",
        "description": "Hent den fulde filmografi for en person. Kraever TMDB person-ID fra search_person.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person_id": {"type": "integer"},
            },
            "required": ["person_id"],
        },
    },
    {
        "name": "get_now_playing",
        "description": (
            "Hent film der korer i biografen lige nu (dansk region). "
            "Brug dette naar brugeren spoerger om hvad der er i biografen, "
            "'hvad korer der' eller vil vide hvad der vises i oejeblikket."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_upcoming",
        "description": (
            "Hent kommende film der snart udkommer i biografen (dansk region). "
            "Brug dette naar brugeren spoerger om kommende film, "
            "'hvad kommer der snart' eller vil vide hvad der er paa vej."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },

    # ── Plex ──────────────────────────────────────────────────────────────────
    {
        "name": "check_plex_library",
        "description": (
            "Tjek om en specifik titel allerede findes i Plex-biblioteket. "
            "SKAL altid kaldes foer request_movie eller request_tv. "
            "Hvis 'found': fortael brugeren vi allerede har den, og anmod IKKE."
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
        "name": "get_plex_collection",
        "description": (
            "Soeg i Plex-biblioteket efter ALLE titler der matcher et nogleord. "
            "Brug dette naar brugeren spoerger om hvad vi har af en bestemt franchise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
            },
            "required": ["keyword", "media_type"],
        },
    },
    {
        "name": "get_on_deck",
        "description": (
            "Hent brugerens 'Se videre'-liste — titler der er paabegyndt men ikke faerdigset. "
            "Brug dette naar brugeren spoerger hvad de er i gang med, eller er ubeslutsom."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_plex_metadata",
        "description": (
            "Hent tekniske specs for en Plex-titel: oplosning, HDR, videocodec, lyd, bitrate. "
            "Returner KUN tekniske specs — aldrig filnavne eller mappestier."
        ),
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
        "description": (
            "Find tilfaeldige usete film eller serier i Plex. "
            "Brug dette naar brugeren er ubeslutsom eller spoerger 'hvad skal jeg se i aften'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
                "genre": {"type": "string", "description": "Valgfrit genre-filter, f.eks. 'action'."},
            },
            "required": ["media_type"],
        },
    },
    {
        "name": "get_similar_in_library",
        "description": (
            "Find titler i Plex der ligner en bestemt film eller serie. "
            "Brug dette naar brugeren siger 'find noget der ligner X'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "get_missing_from_collection",
        "description": (
            "Find hvad der mangler af en samling eller franchise i Plex. "
            "Brug dette naar brugeren spoerger 'hvad mangler jeg af X' eller 'er jeg komplet med Y'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "collection_name": {"type": "string", "description": "F.eks. 'Olsen-banden' eller 'Marvel'."},
            },
            "required": ["collection_name"],
        },
    },
    {
        "name": "search_plex_by_actor",
        "description": (
            "Find film eller serier i Plex-biblioteket med en bestemt skuespiller eller instruktør. "
            "Brug dette når brugeren spørger 'hvilke film har jeg med X' eller 'har vi noget med Y'. "
            "Søger i metadata — ikke i titler. Langt mere præcis end get_plex_collection til personnavne."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "actor_name": {
                    "type": "string",
                    "description": "Skuespillerens eller instruktørens fulde navn, f.eks. 'Kate Winslet'.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "description": "Søg i film eller serier. Standard er 'movie'.",
                },
            },
            "required": ["actor_name"],
        },
    },

    # ── Seerr ─────────────────────────────────────────────────────────────────
    {
        "name": "request_movie",
        "description": (
            "Bestil en film. MUS KUN kaldes hvis check_plex_library returnerede 'missing'. "
            "Kald altid get_media_details foerst for at bestemme category. "
            "category='animation' hvis genre ID 16. "
            "category='dansk' hvis original_language='da' og ikke animation. "
            "category='standard' ellers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer"},
                "category": {"type": "string", "enum": ["animation", "dansk", "standard"]},
            },
            "required": ["tmdb_id", "category"],
        },
    },
    {
        "name": "request_tv",
        "description": (
            "Bestil en TV-serie. MUS KUN kaldes hvis check_plex_library returnerede 'missing'. "
            "Kald get_media_details foerst — brug season_numbers NOEJAGTIGT som de er fra TMDB. "
            "category='tv_program' for reality/talk/nyheder/dokumentar eller dansk uden Drama/Krimi. "
            "category='standard' for international fiktion og dansk Drama/Krimi."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer"},
                "season_numbers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Eksakte saesonnumre fra get_media_details — kopier direkte.",
                },
                "category": {"type": "string", "enum": ["tv_program", "standard"]},
            },
            "required": ["tmdb_id", "season_numbers", "category"],
        },
    },
    {
        "name": "get_all_requests",
        "description": (
            "Hent alle aktive bestillinger og deres status. "
            "Brug dette naar brugeren spoerger 'hvad er paa vej', "
            "'hvad har jeg bestilt' eller vil have et overblik over bestillingslisten."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_request_status",
        "description": (
            "Tjek status paa en specifik bestilling. "
            "Brug dette naar brugeren spoerger 'er X kommet endnu' eller 'hvad sker der med Y'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Titel der soges efter i bestillingslisten."},
            },
            "required": ["title"],
        },
    },

    # ── Tautulli ──────────────────────────────────────────────────────────────
    {
        "name": "get_popular_on_plex",
        "description": (
            "Hent de mest populære film og serier på Plex-serveren som en prioriteret topliste. "
            "Brug dette når brugeren spørger 'hvad ser andre' eller 'hvad er mest populært'. "
            "Returnerer top 10 film og top 10 serier sorteret efter popularitet — "
            "KUN titler og årstal, ingen bruger- eller afspilningstal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Antal dage der skal kigges tilbage. Standard er 7, medmindre brugeren beder om andet (f.eks. 28 eller 30).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_user_watch_stats",
        "description": (
            "Hent brugerens personlige Plex-statistik (hvor meget tid de har brugt, mest sete osv.). "
            "Brug dette når brugeren spørger ind til deres egne vaner, f.eks. 'hvor meget har jeg set i år'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Antal dage der skal kigges tilbage (f.eks. 30, 365). Undlad hvis der ønskes 'all time'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_user_history",
        "description": (
            "Søg i brugerens egen afspilningshistorik. "
            "Brug dette når brugeren spørger 'hvad var det jeg så i tirsdags?' eller 'hvornår så jeg X?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Valgfri titel eller søgeord at lede efter i historikken.",
                },
            },
            "required": [],
        },
    },
]