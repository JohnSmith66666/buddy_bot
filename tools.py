"""
tools_patch.py — Præcis ændring til tools.py på GitHub.

Find check_plex_library tool-definitionen og udskift input_schema:

FØR:
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "year": {"type": "integer"},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
            },
            "required": ["title", "media_type"],
        },

EFTER:
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "year": {"type": "integer"},
                "media_type": {"type": "string", "enum": ["movie", "tv"]},
                "tmdb_id": {
                    "type": "integer",
                    "description": (
                        "TMDB ID for titlen. Sendes med naar det er tilgaengeligt "
                        "(f.eks. efter search_media). Aktiverer GUID-matching i Plex "
                        "som er 100% paalidelig og finder titler gemt under fremmed navn."
                    ),
                },
            },
            "required": ["title", "media_type"],
        },

OG opdatér beskrivelsen til:
        "description": (
            "Tjek om en specifik titel allerede findes i Plex-biblioteket. "
            "SKAL altid kaldes foer en bestilling. "
            "Send altid tmdb_id med hvis du har det — det sikrer korrekt match "
            "ogsaa for titler gemt under engelsk navn (f.eks. 'Boundless' for 'Den graenseloeose'). "
            "Skal ogsaa kaldes for ALLE titler i en trending- eller anbefalingsliste "
            "foer svaret formuleres til brugeren (se system-prompten). "
            "Hvis 'found': fortael brugeren vi allerede har den og STOP (ved bestilling). "
            "Hvis 'missing': svar med SHOW_SEARCH_RESULTS-kommandoen (ved bestilling)."
        ),
"""