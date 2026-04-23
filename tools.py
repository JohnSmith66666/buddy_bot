"""
tools.py - All Claude Tool Use definitions for Buddy.

CHANGES vs previous version:
  - Removed Seerr tools: request_movie, request_tv, get_all_requests, get_request_status
  - Added direct Radarr/Sonarr tools: add_movie, add_series
  - Sorteringslogik er nu håndteret automatisk i service-laget baseret på
    genre (film) og original_language (serier) — Claude behøver ikke angive kategori.
"""

TOOLS = [
    # ── TMDB ──────────────────────────────────────────────────────────────────
    {
        "name": "search_media",
        "description": (
            "Soeg efter film og/eller TV-serier. "
            "Brug dette vaerktoej naar brugeren spoerger om en bestemt film eller serie, "
            "vil finde noget at se, eller naevner en titel. "
            "Returnerer op til 10 resultater per type med titel, aar, genre_ids og beskrivelse."
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
            "original_language, tvdb_id og season_numbers. "
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
            "Brug dette naar brugeren spoerger 'hvilke film har jeg med X' eller 'har vi noget med Y'."
        ),
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
        "description": (
            "Hent brugerens 'Se videre'-liste — titler der er paabegyndt men ikke faerdigset."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_plex_metadata",
        "description": (
            "Hent tekniske specs for en Plex-titel: oplosning, HDR, videocodec, lyd, bitrate."
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

    # ── Radarr ────────────────────────────────────────────────────────────────
    {
        "name": "add_movie",
        "description": (
            "Tilfoej en film direkte til Radarr saa den downloades automatisk. "
            "MUS KUN kaldes hvis check_plex_library returnerede 'missing'. "
            "Kald altid get_media_details foerst for at faa title, year og genres. "
            "Rodmappe bestemmes automatisk: Animation → /Movies/Animation, alt andet → /Movies/Film."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {"type": "integer", "description": "TMDB ID fra get_media_details."},
                "title":   {"type": "string",  "description": "Filmens titel."},
                "year":    {"type": "integer",  "description": "Udgivelsesaar."},
                "genres":  {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Genre-navne fra get_media_details (f.eks. ['Action', 'Animation']).",
                },
            },
            "required": ["tmdb_id", "title", "year", "genres"],
        },
    },

    # ── Sonarr ────────────────────────────────────────────────────────────────
    {
        "name": "add_series",
        "description": (
            "Tilfoej en TV-serie direkte til Sonarr saa den downloades automatisk. "
            "MUS KUN kaldes hvis check_plex_library returnerede 'missing'. "
            "Kald get_media_details foerst — brug tvdb_id, original_language og season_numbers NOEJAGTIGT. "
            "Rodmappe bestemmes automatisk: dansk (original_language='da') → /TV/TV, alt andet → /TV/Serier."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tvdb_id": {"type": "integer", "description": "TVDB ID fra get_media_details."},
                "title":   {"type": "string",  "description": "Seriens titel."},
                "year":    {"type": "integer",  "description": "Premiere-aar."},
                "original_language": {
                    "type": "string",
                    "description": "Originalsprog fra get_media_details (f.eks. 'da', 'en').",
                },
                "season_numbers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Eksakte saesonnumre fra get_media_details — kopier direkte.",
                },
            },
            "required": ["tvdb_id", "title", "year", "original_language", "season_numbers"],
        },
    },

    # ── Tautulli ──────────────────────────────────────────────────────────────
    {
        "name": "get_popular_on_plex",
        "description": (
            "Hent de mest populaere film og serier paa Plex-serveren. "
            "Brug KUN dette naar brugeren spoerger om hvad der er 'populaert', 'mest set' eller 'hitter'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Antal dage der skal kigges tilbage. Standard er 30."},
            },
            "required": [],
        },
    },
    {
        "name": "get_user_watch_stats",
        "description": (
            "Hent brugerens personlige statistik — seertid, antal afspilninger og top 5 film/serier."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Antal dage (f.eks. 30, 365)."},
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
                "query": {"type": "string", "description": "Valgfri titel at lede efter."},
            },
            "required": [],
        },
    },
    {
        "name": "get_recently_added",
        "description": (
            "Hent det nyeste indhold der er tilfojet til Plex-serveren. "
            "Brug ALTID dette naar brugeren siger ord som 'nyt', 'tilfojet', 'landet', 'kommet'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Antal elementer. Brug 50+ for laengere perioder."},
            },
            "required": [],
        },
    },
]