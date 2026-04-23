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
            "Brug dette ALTID foer en anmodning for at faa genre_ids, "
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
        "description": "Hent de mest populaere og trendende film og serier denne uge globalt.",
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
            "Brug dette naar brugeren naevner et personnavn eller spoerger om en persons karriere generelt."
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
            "Brug dette naar brugeren spoerger om hvad der er i biografen."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_upcoming",
        "description": (
            "Hent kommende film der snart udkommer i biografen (dansk region). "
            "Brug dette naar brugeren spoerger om kommende film eller hvad der er paa vej i biografen."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },

    # ── Plex ──────────────────────────────────────────────────────────────────
    {
        "name": "check_plex_library",
        "description": (
            "Tjek om en specifik titel allerede findes i Plex-biblioteket. "
            "SKAL altid kaldes foer en bestilling. "
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
            "Soeg i Plex-biblioteket efter ALLE titler der matcher et nogleord eller franchisenavn. "
            "Brug dette til franchise-soegninger som 'Marvel', 'Star Wars', 'Olsen-banden'. "
            "Brug IKKE dette til personnavne — brug search_plex_by_actor i stedet."
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
        "name": "search_plex_by_actor",
        "description": (
            "Find film eller serier i Plex-biblioteket med en bestemt skuespiller eller instruktør. "
            "Brug dette naar brugeren spoerger 'hvilke film har jeg med X' eller 'har vi noget med Y'. "
            "Soeger i metadata — ikke i titler. Langt mere praecis end get_plex_collection til personnavne."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "actor_name": {
                    "type": "string",
                    "description": "Skuespillerens eller instruktoerens fulde navn, f.eks. 'Kate Winslet'.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "description": "Soeg i film eller serier. Standard er 'movie'.",
                },
            },
            "required": ["actor_name"],
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
                "genre": {"type": "string", "description": "Valgfrit genre-filter, f.eks. 'action' eller 'sci-fi'."},
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

    # ── Seerr ─────────────────────────────────────────────────────────────────
    {
        "name": "request_movie",
        "description": (
            "Bestil en film til serveren. MUS KUN kaldes hvis check_plex_library returnerede 'missing'. "
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
            "Bestil en TV-serie til serveren. MUS KUN kaldes hvis check_plex_library returnerede 'missing'. "
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
            "Brug dette naar brugeren spoerger 'hvad er paa vej' eller 'hvad har jeg bestilt'."
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
            "Hent de mest populaere film og serier paa Plex-serveren. "
            "Brug KUN dette naar brugeren spoerger om hvad der er 'populaert', 'mest set' eller 'hitter'. "
            "Brug IKKE dette til 'nyt' eller 'tilfojet' — brug get_recently_added i stedet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Antal dage der skal kigges tilbage. Standard er 30.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_user_watch_stats",
        "description": (
            "Hent brugerens personlige statistik — seertid, antal afspilninger og top 5 film/serier. "
            "Brug dette naar brugeren spoerger om deres egne vaner eller 'hvor meget har jeg set'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Antal dage der skal kigges tilbage (f.eks. 30, 365). Undlad for 'all time'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_user_history",
        "description": (
            "Soeg i brugerens egen afspilningshistorik. "
            "Brug dette naar brugeren spoerger 'hvad var det jeg saa i tirsdags?' eller 'hvornaar saa jeg X?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Valgfri titel eller sogeord at lede efter i historikken.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_recently_added",
        "description": (
            "Hent det nyeste indhold der er tilfojet til Plex-serveren. "
            "Brug ALTID dette naar brugeren siger ord som 'nyt', 'tilfojet', 'landet', 'kommet' eller spoerger om "
            "hvad der er tilfojet i en bestemt periode (fx 'de seneste 14 dage', 'denne uge'). "
            "Saet count hoejt (fx 50) naar brugeren spoerger om en laengere periode. "
            "Brug ALDRIG get_popular_on_plex til disse spoergsmaal — det er to helt forskellige ting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": (
                        "Antal elementer der skal hentes. "
                        "Brug 10-20 for 'seneste nyt', 30-50 for 'seneste uge', 50-100 for 'seneste 14 dage'."
                    ),
                },
            },
            "required": [],
        },
    },
]