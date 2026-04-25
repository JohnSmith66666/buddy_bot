"""
ai_handler.py - Agentic loop for Buddy.

CHANGES vs previous version:
  - INFO_SIGNAL = "SHOW_INFO:" tilføjet og eksporteret.
    Format: SHOW_INFO:<tmdb_id>:<media_type>
    Bruges af main.py til at åbne Netflix-look infokort direkte
    når brugeren beder om at se en bestemt titel.
  - max_tokens håndtering og parallel tool execution — uændret.
  - ZoneInfo dato-injektion — uændret.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _TZ_COPENHAGEN = ZoneInfo("Europe/Copenhagen")
except Exception:
    _TZ_COPENHAGEN = None

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from prompts import SYSTEM_PROMPT
from services.plex_service import (
    check_actor_on_plex,
    check_franchise_on_plex,
    check_library,
    find_unwatched,
    get_collection,
    get_missing_from_collection,
    get_on_deck,
    get_plex_metadata,
    get_similar_in_library,
    search_by_actor,
)
from services.tmdb_service import (
    get_media_details,
    get_now_playing,
    get_person_filmography,
    get_recommendations,
    get_trending,
    get_upcoming,
    get_watch_providers,
    search_media,
    search_person,
)
from services.tautulli_service import (
    get_popular_on_plex,
    get_recently_added,
    get_user_history,
    get_user_watch_stats,
)
from services.web_service import search_web
from tools import TOOLS

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

_histories: dict[int, list[dict]] = defaultdict(list)
_MAX_HISTORY = 6          # Reduceret fra 10 → sparer ~400 uncached tokens per kald
_MAX_TOOL_RESULT_CHARS = 6000

# Signal som Claude returnerer for at trigge bestillingsflow i main.py
SEARCH_SIGNAL = "SHOW_SEARCH_RESULTS:"

# Signal som Claude returnerer for at vise en trailer-knap i main.py
# Format: SHOW_TRAILER:<beskedtekst>|<youtu.be-url>
TRAILER_SIGNAL = "SHOW_TRAILER:"

# Signal som Claude returnerer for at åbne Netflix-look infokort direkte
# Format: SHOW_INFO:<tmdb_id>:<media_type>  f.eks. SHOW_INFO:157336:movie
INFO_SIGNAL = "SHOW_INFO:"

# Dansk mapning af engelske ugedags- og månedsnavne fra strftime
_UGEDAGE = {
    "Monday": "Mandag", "Tuesday": "Tirsdag", "Wednesday": "Onsdag",
    "Thursday": "Torsdag", "Friday": "Fredag", "Saturday": "Lørdag",
    "Sunday": "Søndag",
}
_MAANEDER = {
    "January": "januar", "February": "februar", "March": "marts",
    "April": "april", "May": "maj", "June": "juni",
    "July": "juli", "August": "august", "September": "september",
    "October": "oktober", "November": "november", "December": "december",
}


def _dansk_dato() -> str:
    """
    Returnerer dags dato i Copenhagen-tidszone, med ISO-format forrest.

    Output-format: '2026-04-24 (Fredag d. 24. april 2026)'
    Klokkeslættet er udeladt — det er irrelevant for dato-sammenligning.
    ISO-delen (YYYY-MM-DD) kan sammenlignes alfabetisk/numerisk direkte
    med TMDB's release_date-felter (der også er YYYY-MM-DD).

    Bruger ZoneInfo("Europe/Copenhagen") for korrekt dansk dato på Railway
    (der kører UTC). Falder tilbage til datetime.now() hvis zoneinfo fejler.
    strftime-output er engelsk på Railway — vi mapper manuelt til dansk.
    """
    try:
        nu = datetime.now(_TZ_COPENHAGEN) if _TZ_COPENHAGEN else datetime.now()
    except Exception:
        nu = datetime.now()
    iso    = nu.strftime("%Y-%m-%d")
    ugedag = _UGEDAGE.get(nu.strftime("%A"), nu.strftime("%A"))
    maaned = _MAANEDER.get(nu.strftime("%B"), nu.strftime("%B"))
    return f"{iso} ({ugedag} d. {nu.day}. {maaned} {nu.year})"


def _trim(telegram_id: int) -> None:
    h = _histories[telegram_id]
    if len(h) > _MAX_HISTORY:
        _histories[telegram_id] = h[-_MAX_HISTORY:]


def _slim_data(data, max_list_items: int = 40):
    """
    Rekursivt trim data-strukturen FØR JSON-serialisering.
    Lister cappes til max_list_items — aldrig hård string-truncation.
    Garanterer altid gyldig JSON output.

    max_list_items hævet til 40 for at matche _FRANCHISE_MAX_PER_LIST
    og sikre at alle film fra check_actor_on_plex og check_franchise_status
    når frem til Buddy med korrekte ID'er.
    """
    if isinstance(data, list):
        trimmed = data[:max_list_items]
        result  = [_slim_data(item, max_list_items) for item in trimmed]
        if len(data) > max_list_items:
            result.append({"_truncated": f"{len(data) - max_list_items} flere elementer udeladt"})
        return result
    if isinstance(data, dict):
        return {k: _slim_data(v, max_list_items) for k, v in data.items()}
    if isinstance(data, str) and len(data) > 300:
        return data[:297] + "..."
    return data


def _trim_tool_result(result: str) -> str:
    """
    Trim tool result til gyldig, kompakt JSON via strukturel trimming.
    To pas: max 10 items, derefter max 5 hvis stadig for lang.
    """
    if len(result) <= _MAX_TOOL_RESULT_CHARS:
        return result

    logger.debug("Trimming tool result from %d chars", len(result))
    try:
        data    = json.loads(result)
        slimmed = _slim_data(data)
        compact = json.dumps(slimmed, ensure_ascii=False, separators=(",", ":"))
        if len(compact) <= _MAX_TOOL_RESULT_CHARS:
            return compact
        slimmed2 = _slim_data(data, max_list_items=5)
        return json.dumps(slimmed2, ensure_ascii=False, separators=(",", ":"))
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Could not parse tool result as JSON: %s", e)
        return result


async def _dispatch(tool_name: str, tool_input: dict, plex_username: str | None) -> str:
    j = lambda x: json.dumps(x, ensure_ascii=False)

    # ── Web-søgning ───────────────────────────────────────────────────────────
    if tool_name == "search_web":
        return j(await search_web(
            query=tool_input["query"],
            search_depth=tool_input.get("search_depth", "basic"),
        ))

    # ── TMDB ──────────────────────────────────────────────────────────────────
    if tool_name == "search_media":
        return j(await search_media(
            tool_input["query"], tool_input.get("media_type", "both")
        ))
    if tool_name == "get_media_details":
        return j(await get_media_details(
            tool_input["tmdb_id"], tool_input["media_type"]
        ))
    if tool_name == "get_trending":
        return j(await get_trending())
    if tool_name == "get_recommendations":
        return j(await get_recommendations(
            tool_input["tmdb_id"], tool_input["media_type"]
        ))
    if tool_name == "get_watch_providers":
        return j(await get_watch_providers(
            tool_input["tmdb_id"], tool_input["media_type"]
        ))
    if tool_name == "search_person":
        return j(await search_person(tool_input["query"]))
    if tool_name == "get_person_filmography":
        return j(await get_person_filmography(tool_input["person_id"]))
    if tool_name == "get_now_playing":
        return j(await get_now_playing())
    if tool_name == "get_upcoming":
        return j(await get_upcoming())

    # ── Plex ──────────────────────────────────────────────────────────────────
    if tool_name == "check_plex_library":
        return j(await check_library(
            tool_input["title"],
            tool_input.get("year"),
            tool_input["media_type"],
            plex_username,
        ))
    if tool_name == "check_franchise_status":
        return j(await check_franchise_on_plex(
            keyword=tool_input["keyword"],
            plex_username=plex_username,
        ))
    if tool_name == "get_plex_collection":
        return j(await get_collection(
            tool_input["keyword"],
            tool_input.get("media_type", "movie"),
            plex_username,
        ))
    if tool_name == "search_plex_by_actor":
        return j(await check_actor_on_plex(
            actor_name=tool_input["actor_name"],
            plex_username=plex_username,
        ))
    if tool_name == "get_on_deck":
        return j(await get_on_deck(plex_username))
    if tool_name == "get_plex_metadata":
        return j(await get_plex_metadata(
            tool_input["title"], tool_input.get("year"), plex_username
        ))
    if tool_name == "find_unwatched":
        return j(await find_unwatched(
            tool_input["media_type"], tool_input.get("genre"), plex_username
        ))
    if tool_name == "get_similar_in_library":
        return j(await get_similar_in_library(tool_input["title"], plex_username))
    if tool_name == "get_missing_from_collection":
        return j(await get_missing_from_collection(
            tool_input["collection_name"], plex_username
        ))

    # ── Tautulli ──────────────────────────────────────────────────────────────
    if tool_name == "get_popular_on_plex":
        days = tool_input.get("days", 30)
        return j(await get_popular_on_plex(
            stats_count=tool_input.get("stats_count", 10),
            time_range=days if days is not None else 30,
        ))
    if tool_name == "get_user_watch_stats":
        if not plex_username:
            return j({"error": "Intet Plex-brugernavn fundet."})
        days       = tool_input.get("days")
        query_days = days if days is not None else 365
        return j(await get_user_watch_stats(
            plex_username,
            query_days=query_days,
        ))
    if tool_name == "get_user_history":
        if not plex_username:
            return j({"error": "Intet Plex-brugernavn fundet."})
        return j(await get_user_history(
            plex_username,
            length=tool_input.get("length", 25),
            query=tool_input.get("query"),
            media_type=tool_input.get("media_type"),
        ))
    if tool_name == "get_recently_added":
        return j(await get_recently_added(count=tool_input.get("count", 10)))

    return j({"error": f"Ukendt vaerktoej: {tool_name}"})


async def get_ai_response(
    telegram_id: int,
    user_message: str,
    plex_username: str | None = None,
) -> str:
    """
    Run the full agentic loop and return Buddy's reply.

    System-prompt arkitektur (to blokke):
      Blok 0 — SYSTEM_PROMPT med cache_control: ephemeral
               Indeholder alle stabile instruktioner. Caches af Anthropic
               og genbruges på tværs af kald så længe indholdet er uændret.

      Blok 1 — Dynamisk kontekst UDEN cache_control
               Indeholder aktuel dato og plex_username. Denne blok ændrer
               sig ved hvert kald (dato varierer) og må aldrig caches —
               det ville invalidere cache-blok 0 ved hvert request.
    """
    _histories[telegram_id].append({"role": "user", "content": user_message})
    _trim(telegram_id)

    # ── Blok 0: stabil, cachet system-prompt ──────────────────────────────────
    system_blocks = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # ── Blok 1: dynamisk kontekst — aldrig cachet ─────────────────────────────
    dynamic_lines = [
        f"Intern system-info (MÅ IKKE NÆVNES):\n"
        f"Dags dato (ISO) er: {_dansk_dato()}.\n"
        f"VIGTIGT: Sammenlign ALTID filmens 'release_date' med ISO-datoen. "
        f"Hvis 'release_date' er alfabetisk/matematisk MINDRE end dags dato, "
        f"ER FILMEN UDKOMMET, og du SKAL omtale den i datid (f.eks. 'udkom i', 'er landet'). "
        f"Eksempel: '2025-12-17' er MINDRE end '{_dansk_dato()[:10]}' → filmen er udkommet."
    ]
    if plex_username:
        dynamic_lines.append(f"Den aktuelle bruger hedder '{plex_username}' på Plex.")

    system_blocks.append({
        "type": "text",
        "text": "\n\n".join(dynamic_lines),
        # Ingen cache_control — denne blok er altid frisk
    })

    # ── Tools med cache på det sidste element ────────────────────────────────
    tools_with_cache = [dict(t) for t in TOOLS]
    if tools_with_cache:
        tools_with_cache[-1] = {
            **tools_with_cache[-1],
            "cache_control": {"type": "ephemeral"},
        }

    try:
        while True:
            response = await _client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1500,          # Hævet fra 1024 → undgår trunkeringer og ekstra API-kald
                system=system_blocks,
                tools=tools_with_cache,
                messages=_histories[telegram_id],
            )

            usage = response.usage
            if hasattr(usage, "cache_read_input_tokens") and usage.cache_read_input_tokens:
                logger.info(
                    "Cache HIT: %d cached, %d uncached, %d output tokens",
                    usage.cache_read_input_tokens,
                    usage.input_tokens,
                    usage.output_tokens,
                )
            else:
                logger.info(
                    "Cache MISS: %d input, %d output tokens",
                    usage.input_tokens,
                    usage.output_tokens,
                )

            if response.stop_reason == "tool_use":
                _histories[telegram_id].append(
                    {"role": "assistant", "content": response.content}
                )

                # ── Parallel tool execution via asyncio.gather ────────────────
                # Alle tool-kald i samme runde eksekveres samtidigt.
                # Speedup ved N parallelle kald: N×latens → max(latens).
                tool_blocks = [b for b in response.content if b.type == "tool_use"]

                async def _run_tool(block) -> dict:
                    logger.info("Tool call: %s(%s)", block.name, block.input)
                    try:
                        raw_result = await _dispatch(block.name, block.input, plex_username)
                        result     = _trim_tool_result(raw_result)
                        logger.info("TOOL DATA MODTAGET [%s]: %s", block.name, str(result)[:1000])
                    except Exception as e:
                        logger.error("Tool dispatch error '%s': %s", block.name, e)
                        result = json.dumps({"error": str(e)}, ensure_ascii=False)
                    return {
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result,
                    }

                tool_results = await asyncio.gather(*(_run_tool(b) for b in tool_blocks))

                _histories[telegram_id].append({"role": "user", "content": list(tool_results)})
                _trim(telegram_id)
                continue

            # ── max_tokens: svar afhugget — returner hvad vi har + note ──────
            if response.stop_reason == "max_tokens":
                partial = next(
                    (b.text for b in response.content if hasattr(b, "text")), ""
                )
                logger.warning(
                    "max_tokens nået for telegram_id=%s (%d output tokens)",
                    telegram_id, usage.output_tokens,
                )
                reply = (
                    partial
                    + "\n\n_(Svaret blev afbrudt for at spare plads — "
                    "spørg endelig hvis du vil have resten med!)_"
                )
                _histories[telegram_id].append({"role": "assistant", "content": reply})
                _trim(telegram_id)
                return reply

            reply = next(
                (b.text for b in response.content if hasattr(b, "text")),
                "Av, noget gik galt — prøv igen om lidt! 🔧",
            )
            _histories[telegram_id].append({"role": "assistant", "content": reply})
            _trim(telegram_id)
            logger.info("BUDDY SENDTE: %s", reply)
            return reply

    except anthropic.APIError as e:
        logger.error("Anthropic error for user %s: %s", telegram_id, e)
        _histories.pop(telegram_id, None)
        return "Av, noget gik galt hos mig — prøv igen om lidt! 🔧"


def clear_history(telegram_id: int) -> None:
    _histories.pop(telegram_id, None)