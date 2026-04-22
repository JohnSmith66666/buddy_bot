"""
ai_handler.py - Manages all communication with the Anthropic Claude API.

Uses Tool Use (function calling) so Claude can query TMDB, check Plex,
and request media via Seerr. New tools can be added to TOOLS and
_handle_tool_call() as the project grows.
"""

import json
import logging
from collections import defaultdict

import anthropic

from config import ANTHROPIC_API_KEY
from services.plex_service import check_library
from services.seerr_service import request_movie, request_tv
from services.tmdb_service import (
    get_media_details,
    get_person_filmography,
    get_recommendations,
    get_trending,
    get_watch_providers,
    search_media,
    search_person,
)

logger = logging.getLogger(__name__)

# ── Anthropic client ──────────────────────────────────────────────────────────

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Du er en eksplosiv, humoristisk og super hjælpsom medie-overlord. Du taler dansk.

SOEGNING — VIGTIGT:
Naar brugeren skriver en titel med et aarstal i parentes, f.eks. 'Filmnavn (2026)', skal du:
1. Fjerne parentesen og aarstal fra selve soege-query'et — send KUN 'Filmnavn' som query.
2. Bruge aarstallet til at identificere det korrekte resultat i svaret bagefter.
Eksempel: brugeren skriver 'Thunderbolts (2025)' → send query='Thunderbolts', filtrer paa aar=2025.
Du finder aldrig paa information selv — brug altid dine vaerktoejer til at hente data.

PLEX-TJEK FOER ANMODNING — BENHAARD REGEL:
Foer du NOGENSINDE sender en anmodning (request_movie eller request_tv), SKAL du:
1. Kalde check_plex_library med titel og aar fra søgeresultatet.
2. Hvis status er "found": Fortael brugeren at vi allerede har det paa Plex. Stop her. Anmod IKKE.
3. Kun hvis status er "missing": Fortsaet med anmodningslogikken nedenfor.

ANMODNINGSREGLER - SORTERING:
Foer du anmoder, SKAL du kalde get_media_details for at faa genre_ids, original_language og season_numbers.

For FILM - vaelg category saaledes:
- category="animation"  hvis genre ID 16 (Animation) er til stede
- category="dansk"      hvis original_language er "da" OG filmen ikke er animation
- category="standard"   i alle andre tilfaelde

For SERIER:
- Du SKAL sende season_numbers NOEJAGTIGT som de fremgaar af 'season_numbers' feltet i get_media_details.
- Du maa ALDRIG antage eller opdigte saesonnumre. Hvis TMDB kun viser [25], sender du [25]. Ikke [1, 25].
- Vaelg category saaledes:
  * category="tv_program" hvis een eller flere af disse betingelser er opfyldt:
      - genren indeholder Reality (10764), Talk (10767), News (10763) eller Dokumentar (99)
      - original_language er "da" OG genren indeholder Familie (10751)
      - original_language er "da" OG genren mangler baade Drama (18) og Krimi (80)
  * category="standard" for internationale serier og ren dansk fiktion med Drama (18) eller Krimi (80)

PRAESENTATION:
Naar du praesentererer soegeresultater, viser du titel, aar, genre og en kort beskrivelse.
Naar du praesentererer en person, viser du navn, rolle og deres mest kendte vaerker.
Naar du viser streaming-udbydere, naevner du KUN danske tjenester.
Naar du bekraefter en vellykket anmodning, fortaeller du at du har bestilt den og holder oeje med den.
Naar status er "already_queued": Fortael at den ikke er paa Plex endnu, men at den allerede er bestilt og er paa vej - anmod IKKE igen.
Naar status er "already_available": Fortael at den er tilgaengelig i biblioteket.
Hvis du ikke finder en titel: Sig "Jeg kan desvaerre ikke finde den film/serie, du leder efter. Er du sikker paa at titlen er helt rigtig, eller er den maske saa ny at den slet ikke er annonceret endnu? 🕷️"

SPROGLIGE REGLER — MEGET VIGTIGT:
Du maa ALDRIG naevne disse ord i dine svar til brugeren:
- Seerr, Radarr, Sonarr, TMDB
- API, rootFolder, payload, endpoint, database, systemet
- download, downloader, downloading, henter (erstat med 'er på vej' eller 'tilføjes snart til Plex')

Du MAA gerne naevne Plex, da det er selve biblioteket brugeren kender.

Brug i stedet disse vendinger:
- 'er allerede bestilt' eller 'er allerede anmodet om' (i stedet for 'ligger i koeen')
- 'jeg holder oeje med den for dig' eller 'den bliver automatisk tilfoejt til Plex, saa snart den er klar'
- 'jeg har bestilt den til dig' eller 'jeg har sat den paa bestillingslisten' (ved ny anmodning)
- Hvis noget fejler: lyd som en hjælpsom assistent, ikke en computerfejl. Sig f.eks. "Av, noget gik galt hos mig - proev igen om lidt! 🔧"

FORMATTERING:
Du skriver KUN i Telegram-kompatibelt format.
Brug *fed tekst* med enkelt stjerne for fed.
Brug _kursiv_ med underscore for kursiv.
Brug ALDRIG ## headers, ** dobbelt stjerne, eller andre Markdown-formater.
Hold svarene korte og snappy - maks 3-4 linjer medmindre brugeren beder om mere."""

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_media",
        "description": (
            "Soeg efter film og/eller TV-serier paa TMDB. "
            "Brug dette vaerktoej naar brugeren spoerger om en bestemt film eller serie, "
            "vil finde noget at se, eller naevner en titel. "
            "Returnerer op til 5 resultater per type med titel, aar, genre_ids og beskrivelse."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Soegetermet - typisk en titel eller nogleord.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv", "both"],
                    "description": (
                        "Hvilken type medie der soges efter. "
                        "Brug 'movie' for film, 'tv' for serier, 'both' hvis det er uklart."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_media_details",
        "description": (
            "Hent detaljerede oplysninger om en specifik film eller TV-serie fra TMDB. "
            "Brug dette vaerktoej naar brugeren vil vide mere om et bestemt resultat, "
            "ELLER naar du skal forberede en Seerr-anmodning. "
            "For serier returnerer dette felt 'season_numbers' som skal sendes direkte til request_tv. "
            "Kraever et TMDB ID fra search_media."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {
                    "type": "integer",
                    "description": "TMDB ID paa filmen eller serien.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "description": "Om det er en film eller en serie.",
                },
            },
            "required": ["tmdb_id", "media_type"],
        },
    },
    {
        "name": "check_plex_library",
        "description": (
            "Tjek om en film eller serie allerede findes i Plex-biblioteket. "
            "SKAL altid kaldes foer request_movie eller request_tv. "
            "Hvis status er 'found', skal du fortaelle brugeren at vi allerede har det paa Plex "
            "og IKKE sende en Seerr-anmodning. "
            "Hvis status er 'missing', kan du fortsaette med Seerr."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Titlen paa filmen eller serien (fra TMDB).",
                },
                "year": {
                    "type": "integer",
                    "description": "Udgivelsesaaret fra TMDB (bruges til praeciis matching).",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "description": "Om det er en film eller en serie.",
                },
            },
            "required": ["title", "media_type"],
        },
    },
    {
        "name": "search_person",
        "description": (
            "Soeg efter en skuespiller, instruktoer eller andet filmhold-medlem paa TMDB. "
            "Brug dette vaerktoej naar brugeren naevner et personnavn, spoerger om en "
            "skuespillers karriere, eller vil finde film med en bestemt person."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Personens navn, f.eks. 'Tom Hanks' eller 'Christopher Nolan'.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_person_filmography",
        "description": (
            "Hent den fulde filmografi for en specifik person - baade film og TV-serier. "
            "Kraever et TMDB person-ID fra search_person."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person_id": {
                    "type": "integer",
                    "description": "TMDB person-ID fra search_person resultater.",
                },
            },
            "required": ["person_id"],
        },
    },
    {
        "name": "get_trending",
        "description": (
            "Hent de mest populaere og trendende film og serier denne uge. "
            "Brug dette vaerktoej naar brugeren spoerger om hvad der er populaert lige nu "
            "eller vil have inspiration til noget at se."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_recommendations",
        "description": (
            "Find film eller serier der ligner en specifik titel. "
            "Brug dette vaerktoej naar brugeren vil have anbefalinger baseret paa noget "
            "de allerede kan lide. Kraever et TMDB ID fra search_media."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {
                    "type": "integer",
                    "description": "TMDB ID paa den titel der baseres anbefalinger paa.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "description": "Om det er en film eller en serie.",
                },
            },
            "required": ["tmdb_id", "media_type"],
        },
    },
    {
        "name": "get_watch_providers",
        "description": (
            "Find ud af hvilke danske streamingtjenester en film eller serie er tilgaengelig paa. "
            "Brug dette vaerktoej naar brugeren spoerger 'hvor kan jeg se X' eller "
            "'er X paa Netflix'. Returnerer KUN danske udbydere (DK)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {
                    "type": "integer",
                    "description": "TMDB ID paa filmen eller serien.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "description": "Om det er en film eller en serie.",
                },
            },
            "required": ["tmdb_id", "media_type"],
        },
    },
    {
        "name": "request_movie",
        "description": (
            "Anmod om download af en film via Seerr. "
            "MUS KUN kaldes hvis check_plex_library returnerede status='missing'. "
            "VIGTIGT: Kald altid get_media_details foerst for at bestemme den korrekte category. "
            "Saet category='animation' hvis genre ID 16 er til stede. "
            "Saet category='dansk' hvis original_language='da' og filmen ikke er animation. "
            "Saet category='standard' i alle andre tilfaelde."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {
                    "type": "integer",
                    "description": "TMDB ID paa filmen der skal anmodes om.",
                },
                "category": {
                    "type": "string",
                    "enum": ["animation", "dansk", "standard"],
                    "description": (
                        "'animation' → Animation-mappen, "
                        "'dansk' → Dansk-mappen, "
                        "'standard' → Film-mappen."
                    ),
                },
            },
            "required": ["tmdb_id", "category"],
        },
    },
    {
        "name": "request_tv",
        "description": (
            "Anmod om download af en TV-serie via Seerr. "
            "MUS KUN kaldes hvis check_plex_library returnerede status='missing'. "
            "VIGTIGT: Kald altid get_media_details foerst. "
            "Brug 'season_numbers' fra get_media_details NOEJAGTIGT som de er. "
            "Tilfoej ALDRIG saesonnumre der ikke staar i TMDB's liste. "
            "SORTERING: Saet category='tv_program' hvis genren indeholder Reality (10764), "
            "Talk (10767), News (10763) eller Dokumentar (99) — ELLER hvis original_language='da' "
            "og genren indeholder Familie (10751) eller mangler baade Drama (18) og Krimi (80). "
            "Saet category='standard' for international fiktion og dansk Drama/Krimi."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tmdb_id": {
                    "type": "integer",
                    "description": "TMDB ID paa serien der skal anmodes om.",
                },
                "season_numbers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Eksakte saesonnumre fra 'season_numbers' i get_media_details. "
                        "Kopier listen direkte — tilfoej eller fjern ikke noget."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": ["tv_program", "standard"],
                    "description": (
                        "'tv_program' → TV-programmer-mappen. "
                        "'standard' → Serier-mappen."
                    ),
                },
            },
            "required": ["tmdb_id", "season_numbers", "category"],
        },
    },
]

# ── In-memory conversation history per user ───────────────────────────────────

_histories: dict[int, list[dict]] = defaultdict(list)
_MAX_HISTORY = 20


def _trim_history(telegram_id: int) -> None:
    history = _histories[telegram_id]
    if len(history) > _MAX_HISTORY:
        _histories[telegram_id] = history[-_MAX_HISTORY:]


# ── Tool execution ────────────────────────────────────────────────────────────

async def _handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """Execute the requested tool and return the result as a JSON string."""
    logger.info("Tool call: %s(%s)", tool_name, tool_input)

    if tool_name == "search_media":
        results = await search_media(
            query=tool_input["query"],
            media_type=tool_input.get("media_type", "both"),
        )
        return json.dumps(results, ensure_ascii=False)

    if tool_name == "get_media_details":
        details = await get_media_details(
            tmdb_id=tool_input["tmdb_id"],
            media_type=tool_input["media_type"],
        )
        return json.dumps(details, ensure_ascii=False)

    if tool_name == "check_plex_library":
        result = await check_library(
            title=tool_input["title"],
            year=tool_input.get("year"),
            media_type=tool_input["media_type"],
        )
        return json.dumps(result, ensure_ascii=False)

    if tool_name == "search_person":
        results = await search_person(query=tool_input["query"])
        return json.dumps(results, ensure_ascii=False)

    if tool_name == "get_person_filmography":
        filmography = await get_person_filmography(person_id=tool_input["person_id"])
        return json.dumps(filmography, ensure_ascii=False)

    if tool_name == "get_trending":
        results = await get_trending()
        return json.dumps(results, ensure_ascii=False)

    if tool_name == "get_recommendations":
        results = await get_recommendations(
            tmdb_id=tool_input["tmdb_id"],
            media_type=tool_input["media_type"],
        )
        return json.dumps(results, ensure_ascii=False)

    if tool_name == "get_watch_providers":
        providers = await get_watch_providers(
            tmdb_id=tool_input["tmdb_id"],
            media_type=tool_input["media_type"],
        )
        return json.dumps(providers, ensure_ascii=False)

    if tool_name == "request_movie":
        result = await request_movie(
            tmdb_id=tool_input["tmdb_id"],
            category=tool_input.get("category", "standard"),
        )
        return json.dumps(result, ensure_ascii=False)

    if tool_name == "request_tv":
        result = await request_tv(
            tmdb_id=tool_input["tmdb_id"],
            season_numbers=tool_input["season_numbers"],
            category=tool_input.get("category", "standard"),
        )
        return json.dumps(result, ensure_ascii=False)

    return json.dumps({"error": f"Ukendt vaerktoej: {tool_name}"})


# ── Public API ────────────────────────────────────────────────────────────────

async def get_ai_response(telegram_id: int, user_message: str) -> str:
    """
    Send a message to Claude and return the assistant's reply.

    Handles the full Tool Use agentic loop:
      1. Send message + tools to Claude.
      2. If Claude requests a tool, execute it and send the result back.
      3. Repeat until Claude returns a plain text response.
    """
    _histories[telegram_id].append({"role": "user", "content": user_message})
    _trim_history(telegram_id)

    try:
        while True:
            response = _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=_histories[telegram_id],
            )

            if response.stop_reason == "tool_use":
                _histories[telegram_id].append(
                    {"role": "assistant", "content": response.content}
                )

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result_json = await _handle_tool_call(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_json,
                            }
                        )

                _histories[telegram_id].append(
                    {"role": "user", "content": tool_results}
                )
                _trim_history(telegram_id)
                continue

            reply = next(
                (block.text for block in response.content if hasattr(block, "text")),
                "Jeg fik ikke et svar fra AI-hjernen. Proev igen.",
            )

            _histories[telegram_id].append({"role": "assistant", "content": reply})
            _trim_history(telegram_id)
            return reply

    except anthropic.APIError as e:
        logger.error("Anthropic API error for user %s: %s", telegram_id, e)
        return "Jeg kunne desvaerre ikke kontakte AI-hjernen lige nu. Proev igen om lidt."


def clear_history(telegram_id: int) -> None:
    """Clear the in-memory conversation history for a user (e.g. on /start)."""
    _histories.pop(telegram_id, None)