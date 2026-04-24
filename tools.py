"""
tools.py - Claude Tool Use definitions for Buddy.

CHANGES vs previous version:
  - Fjernet add_movie og add_series — bestilling sker nu via Inline Keyboards.
  - Claude trigger bestilling via SHOW_SEARCH_RESULTS-svar, ikke direkte tool-kald.
  - get_trending har nu en `media_type` parameter ("movie", "tv", "all").
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
        "description": "Hent detaljerede oplysninger om en specifik film eller TV-serie via TMDB ID.",
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
            "Hent de mest populaere og trendende film og/eller serier denne uge globalt. "
            "Brug media_type for at filtrere: 'movie' for kun film, 'tv' for kun serier, "
            "'all' for begge blandet. Standard er 'all'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv", "all"],
                    "description": "Filtrer paa type: 'movie', 'tv' eller 'all'. Standard: 'all'.",
                },
            },
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
        "description": "Hent den fulde filmografi for en person. Kraever TMDB person-ID.",
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

    # ── Plex ──────────────────────────────────────────────────────────────────
    {
        "name": "check_plex_library",
        "description": (
            "Tjek om en specifik titel allerede findes i Plex-biblioteket. "
            "SKAL altid kaldes foer en bestilling. "
            "Hvis 'found': fortael brugeren vi allerede har den og STOP. "
            "Hvis 'missing': svar med SHOW_SEARCH_RESULTS-kommandoen."
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
        "description": "Soeg i Plex efter alle titler der matcher et nogleord eller franchisenavn.",
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
        "description": "Find film eller serier i Plex med en bestemt skuespiller eller instruktør.",
        "input_schema": {
            "type": "object",
            "properties": {
                "actor_name": {"type": "string"},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
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
        "description": "Find hvad der mangler af en samling eller franchise i Plex.",
        "input_schema": {
            "type": "object",
            "properties": {"collection_name": {"type": "string"}},
            "required": ["collection_name"],
        },
    },

    # ── Tautulli ──────────────────────────────────────────────────────────────
    {
        "name": "get_popular_on_plex",
        "description": "Hent de mest populaere film og serier paa Plex-serveren.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Antal dage. Standard 30."},
            },
            "required": [],
        },
    },
    {
        "name": "get_user_watch_stats",
        "description": "Hent brugerens personlige statistik — seertid og top 5 film/serier.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "get_user_history",
        "description": "Soeg i brugerens egen afspilningshistorik.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
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
            "properties": {
                "count": {"type": "integer"},
            },
            "required": [],
        },
    },
]