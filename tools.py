"""
tools.py - Claude Tool Use definitions for Buddy.

CHANGES vs previous version (v1.2.0 — Etape 4: find_unwatched_v2 som AI-tool):
  - Tilføjet find_unwatched_v2: Dataversion af film-anbefalingstoolet baseret
    på subgenre-systemet (36 subgenrer i 9 kategorier). Henter usete film fra
    Plex der matcher specifikke TMDB-keywords + Plex-genre kombinationer.
    Erstatter find_unwatched når brugeren beder om en specifik undergenre
    (fx 'horror med slasher-vibe', 'heist film', 'tidsrejse-film').
  - VIGTIGT: Description'en indeholder ALLE 36 subgenre-IDs så Claude kan
    vælge den rigtige uden at skulle gætte. Subgenre-IDs er stabile og
    kan kun ændres ved at opdatere SUBGENRES dict i subgenre_service.py.
  - Bevarer find_unwatched som fallback for brede genre-spørgsmål.

UNCHANGED (v1.1.0 — recommend_from_seed combined tool):
  - Tilføjet recommend_from_seed: Combined tool der erstatter sekvensen
    get_recommendations + N×check_plex_library + viewCount-filtrering med
    ét kald. Sparer 5-7 sekunder på anbefalingsflow.
    Returnerer KUN titler på Plex (og usete hvis only_unwatched=true).

UNCHANGED (v1.0.4 — search_plex_by_actor instruktør-fix):
  - search_plex_by_actor: Tilføjet eksplicit advarsel om at værktøjet KUN
    finder skuespillerroller i Plex — ikke instruktørfilm.

UNCHANGED (v0.9.4 — search_media year-filter):
  - search_media: valgfri `year` parameter, årstal sendes separat.
  - check_plex_library: tmdb_id parameter for GUID-matching.
  - check_franchise_status: instruks om 'den seneste'/'næste' i serie.
"""

TOOLS = [
    # ── TMDB ──────────────────────────────────────────────────────────────────
    {
        "name": "search_media",
        "description": (
            "Soeg efter film og/eller TV-serier til informationsformaal. "
            "Brug dette til at besvare spoergsmaal om en titel — IKKE til bestilling. "
            "Til bestilling: tjek Plex, og svar derefter med SHOW_SEARCH_RESULTS-kommandoen. "
            "VIGTIGT: query maa KUN indeholde titlen — aldrig aarstal eller parentes. "
            "Hvis brugeren naevner et aarstal (f.eks. 'The Drama fra 2026' eller "
            "'Breaking the Sound Barrier (2021)'), send titlen rent i query "
            "og send aarstal separat via year-parameteren. "
            "Eksempel: query='The Drama', year=2026 — IKKE query='The Drama 2026'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Kun titlen — ingen aarstal, ingen parentes. "
                        "Korrekt: 'The Drama'. Forkert: 'The Drama 2026'."
                    ),
                },
                "media_type": {"type": "string", "enum": ["movie", "tv", "both"]},
                "year": {
                    "type": "integer",
                    "description": (
                        "Valgfrit aarstal-filter. Send her hvis brugeren naevner et aarstal. "
                        "Film: primary_release_year. TV: first_air_date_year. "
                        "MÅ IKKE inkluderes i query."
                    ),
                },
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
        "description": "Find danske streamingtjenester en titel er tilgaengelig paa. Kraever TMDB ID.",
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
            "VIGTIGT: query maa KUN indeholde personens navn — aldrig jobtitel, "
            "rolle eller andre ord. "
            "✅ KORREKT: query='Quentin Tarantino' "
            "❌ FORKERT: query='Quentin Tarantino director'"
        ),
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
            "samt top 10 TV-serier. "
            "BRUG DETTE naar brugeren spoerger om en INSTRUKTOER (f.eks. 'hvilke "
            "Tarantino-film har vi?' eller 'vis mig Spielbergs film'). "
            "Instruktoerfilm kan IKKE findes via search_plex_by_actor — det vaerktoej "
            "finder kun skuespillerroller. Workflow for instruktoer-spoergsmaal: "
            "1. Kald search_person for at finde person_id. "
            "2. Kald get_person_filmography med person_id. "
            "3. Kald check_plex_library parallelt for alle film i movie_credits. "
            "Til skuespiller-analyse (ikke instruktoer): brug search_plex_by_actor."
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
            "Send altid tmdb_id med hvis du har det — det sikrer korrekt match "
            "ogsaa for titler gemt under engelsk navn (f.eks. 'Boundless' for 'Den graenselose'). "
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
            "Soeg i Plex efter film hvor en SKUESPILLER medvirker. "
            "VIGTIGT: Dette vaerktoej finder KUN titler hvor personen optræder "
            "som skuespiller i Plex-databasen — IKKE som instruktoer, producer osv. "
            "Brug dette naar brugeren spoerger om en skuespiller: "
            "'hvilke film har vi med Tom Hanks?', 'vis mig Cate Blanchetts film'. "
            "Brug IKKE dette til instruktoer-spoergsmaal (Tarantino, Spielberg, Nolan osv.) "
            "— brug i stedet get_person_filmography + check_plex_library. "
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
        "name": "find_unwatched_v2",
        "description": (
            "PRIMÆRT VÆRKTØJ til subgenre-anbefalinger. "
            "Find usete film i Plex der matcher en specifik undergenre via TMDB-keywords "
            "+ Plex-genre kombinationer. Datadrevet — bruger forhandsindekserede "
            "metadata for 6500+ film, så svar er hurtige (<2 sek) og præcise.\n\n"
            "BRUG DETTE NÅR brugeren beder om en specifik undergenre (ikke en bred kategori):\n"
            "  - 'find en heist film jeg ikke har set' → crime_heist\n"
            "  - 'noget med tidsrejse' → scifi_time\n"
            "  - 'vis mig en slasher' → horror_slasher\n"
            "  - 'romantisk komedie' → comedy_romcom\n"
            "  - 'Anden Verdenskrig film' → true_wwii\n"
            "  - 'noget med superhelte' → action_superhero\n\n"
            "BRUG IKKE dette hvis brugeren beder om brede genrer ('action', 'horror') "
            "uden specifik undergenre — brug find_unwatched i stedet.\n\n"
            "GYLDIGE SUBGENRE_IDS (vælg den der bedst matcher brugerens forespørgsel):\n"
            "  Komedie:    comedy_dark, comedy_romcom, comedy_standup, comedy_satire\n"
            "  Action:     action_superhero, action_martial, action_survival, action_roadtrip\n"
            "  Gyser:      horror_psycho, horror_slasher, horror_creature, horror_supernatural\n"
            "  Krimi:      crime_heist, crime_noir, crime_serialkiller, crime_mafia\n"
            "  Sci-fi:     scifi_time, scifi_dystopia, scifi_alien, fantasy_magic\n"
            "  Drama:      drama_youth, drama_tearjerker, drama_family, drama_love\n"
            "  Familie:    family_cartoon, family_christmas, family_animal\n"
            "  Sandt:      true_story, true_biography, true_wwii\n"
            "  Speciel:    special_revenge, special_musical, special_lgbt, "
            "special_sports, special_spy, special_indie\n\n"
            "Returnerer: {status, subgenre, subgenre_label, results: [5 film med "
            "tmdb_id og titel], stats}. Resultaterne er ALLEREDE filtreret til usete "
            "film på Plex — vis dem direkte i ✅-format med /info_movie_X links.\n\n"
            "VIGTIGT: Hvis status='missing', betyder det at brugeren har set alt i "
            "den valgte subgenre. Foreslå da en relateret subgenre i stedet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subgenre_id": {
                    "type": "string",
                    "description": (
                        "Subgenre-ID fra listen ovenfor. Vaelg den der bedst matcher "
                        "brugerens forespoergsel. Skal vaere lowercase + underscore."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Antal forslag at returnere. Standard 5. Maks 10.",
                },
            },
            "required": ["subgenre_id"],
        },
    },
    {
        "name": "find_unwatched",
        "description": (
            "Find tilfaeldige usete film eller serier i Plex. "
            "Brug dette til BREDE genre-spoergsmaal ('find noget action', 'en horror-film'). "
            "For SPECIFIKKE undergenrer ('heist', 'slasher', 'tidsrejse') — brug i "
            "stedet find_unwatched_v2 som er hurtigere og mere praecis."
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
        "name": "recommend_from_seed",
        "description": (
            "PRIMÆRT VÆRKTØJ til anbefalinger baseret på en seed-titel. "
            "Henter TMDB-anbefalinger OG krydstjekker mod Plex OG filtrerer usete "
            "i ét kald. Returnerer KUN titler der er paa Plex og som brugeren ikke "
            "har set endnu. Sparer 5-7 sekunder vs. den gamle sekvens "
            "(get_recommendations + flere check_plex_library kald). "
            "Brug dette naar brugeren beder om noget at se der ligner X — f.eks. "
            "'noget der ligner Inception', 'anbefal noget i samme stil som "
            "Breaking Bad'. "
            "Workflow: 1) Find seed-titlens TMDB ID via search_media. 2) Kald "
            "recommend_from_seed med tmdb_id og media_type. 3) Vis resultaterne "
            "direkte — ingen ekstra check_plex_library kald nødvendige."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {
                    "type": "integer",
                    "description": "TMDB ID for seed-titlen (filmen/serien som anbefalingerne skal baseres på).",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "description": "Type for seed-titlen.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maks antal resultater der returneres. Standard 8.",
                },
                "only_unwatched": {
                    "type": "boolean",
                    "description": (
                        "Hvis true (standard), returneres kun titler brugeren ikke har set. "
                        "Sæt til false hvis brugeren ønsker alle anbefalinger paa Plex (sete + usete)."
                    ),
                },
            },
            "required": ["tmdb_id", "media_type"],
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
            "Returnerer altid top 10 film og top 10 serier med tmdb_id — brug det til links. "
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
            "Brug naar brugeren siger 'nyt', 'tilfojet', 'landet', 'kommet'. "
            "Send count=20 som standard for at faa et godt mix af film og serier."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"count": {"type": "integer", "description": "Antal titler. Standard 20."}},
            "required": [],
        },
    },
]