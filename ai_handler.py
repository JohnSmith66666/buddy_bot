"""
ai_handler.py - Agentic loop for Buddy.

CHANGES vs previous version (v1.1.0 — P1-4 Anthropic API retries):
  - PERFORMANCE/ROBUSTHED: Tilføjet automatisk retry-logik for transient
    Anthropic API-fejl (429 rate limit, 502 bad gateway, 503 service
    unavailable, 504 gateway timeout, samt netværksfejl).

  - MEKANISME: Exponential backoff med op til 2 retries per request:
    * Forsøg 1: Direkte kald
    * Forsøg 2: 1 sekunds wait
    * Forsøg 3: 2 sekunders wait
    * Total worst-case: ~3 sekunder ekstra ved værste tilfælde

  - SCOPE: Retries gælder KUN for messages.create() — ikke for tool-dispatch
    eller intern logik. Det er den klare grænse hvor transient fejl er mest
    sandsynlige (eksterne API-kald).

  - IMPACT: ~80% af transient API-fejl forventes at lykkes ved retry.
    Brugeren oplever en lille forsinkelse i stedet for fejlbesked.

  - LOGGING: Alle retry-forsøg logges som WARNING med error type og
    forsøgsnummer, så vi kan tracke hyppighed og justere strategi.

UNCHANGED (v1.0.8 — recommend_from_seed dispatch):
  - Tilføjet dispatch case for recommend_from_seed (combined tool).

UNCHANGED (v1.0.7 — max_tokens fix):
  - max_tokens: 1500 → 4000 for store filmografier.

UNCHANGED (v1.0.6 — P0 patch):
  - _MAX_TOOL_RESULT_CHARS = 12000.
  - _slim_data() default max_list_items=40.

UNCHANGED:
  - INFO_SIGNAL, TRAILER_SIGNAL, SEARCH_SIGNAL — uændret.
  - Prompt caching arkitektur — uændret.
  - Parallel tool execution via asyncio.gather — uændret.
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
from prompts import get_system_prompt
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
    recommend_from_seed,
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
_last_activity: dict[int, float] = {}
_SESSION_TIMEOUT = 10 * 60
_MAX_HISTORY = 6

_MAX_TOOL_RESULT_CHARS = 12000

SEARCH_SIGNAL  = "SHOW_SEARCH_RESULTS:"
TRAILER_SIGNAL = "SHOW_TRAILER:"
INFO_SIGNAL    = "SHOW_INFO:"

# ── P1-4: Retry config ────────────────────────────────────────────────────────
# Status codes der typisk er transient og kan løses med retry:
#   429 = Too Many Requests (rate limit)
#   502 = Bad Gateway (cloudflare/cdn issue)
#   503 = Service Unavailable (Anthropic kapacitet)
#   504 = Gateway Timeout
_RETRY_STATUS_CODES = {429, 502, 503, 504}
_MAX_RETRIES        = 2     # 2 retries = max 3 forsøg total
_RETRY_BASE_DELAY   = 1.0   # 1s, 2s, 4s ved exponential backoff


def _dansk_dato() -> str:
    if _TZ_COPENHAGEN:
        return datetime.now(_TZ_COPENHAGEN).isoformat()
    return datetime.utcnow().isoformat()


def _trim(telegram_id: int) -> None:
    hist = _histories[telegram_id]
    if len(hist) > _MAX_HISTORY:
        _histories[telegram_id] = hist[-_MAX_HISTORY:]


def _slim_data(data, max_list_items: int = 40):
    """
    Rekursivt trim store lister og fjern None-værdier for at spare tokens.
    To pas: max 40 items i første pas, derefter max 5 hvis stadig for lang.
    """
    if isinstance(data, dict):
        return {k: _slim_data(v, max_list_items) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [_slim_data(i, max_list_items) for i in data[:max_list_items]]
    return data


def _trim_tool_result(result: str) -> str:
    """
    Trim et tool-resultat til max _MAX_TOOL_RESULT_CHARS tegn.
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


# ══════════════════════════════════════════════════════════════════════════════
# P1-4: Retry helper for Anthropic API calls
# ══════════════════════════════════════════════════════════════════════════════

async def _call_with_retry(
    create_kwargs: dict,
    telegram_id: int,
):
    """
    Kald _client.messages.create med automatisk retry ved transient fejl.

    Retry-strategi:
      - Op til _MAX_RETRIES retries (3 forsøg total)
      - Exponential backoff: 1s, 2s mellem forsøg
      - Kun retry på status codes i _RETRY_STATUS_CODES (429, 502, 503, 504)
      - Også retry på APIConnectionError (netværksfejl)
      - Andre fejl raises straks (auth, validation, etc.)

    Args:
      create_kwargs: dict med argumenter til messages.create()
      telegram_id:   bruges til logging

    Returns:
      Anthropic Message response

    Raises:
      anthropic.APIError: hvis alle retries fejler eller fejlen ikke er retriable
    """
    last_exception = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            return await _client.messages.create(**create_kwargs)

        except anthropic.APIConnectionError as e:
            # Netværksfejl - altid retriable
            last_exception = e
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Anthropic APIConnectionError for tg_id=%s, retry %d/%d efter %.1fs: %s",
                    telegram_id, attempt + 1, _MAX_RETRIES, wait, e,
                )
                await asyncio.sleep(wait)
                continue
            raise

        except anthropic.APIStatusError as e:
            # HTTP-status fejl - retry kun på transient codes
            last_exception = e
            status = getattr(e, "status_code", 0)

            if status in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES:
                wait = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Anthropic %d for tg_id=%s, retry %d/%d efter %.1fs: %s",
                    status, telegram_id, attempt + 1, _MAX_RETRIES, wait, e,
                )
                await asyncio.sleep(wait)
                continue

            # Ikke-retriable fejl (auth, validation, 400, etc.) — raise straks
            raise

    # Skulle aldrig nå hertil, men som safety net
    if last_exception:
        raise last_exception


# ══════════════════════════════════════════════════════════════════════════════
# Tool dispatch
# ══════════════════════════════════════════════════════════════════════════════

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
            tool_input["query"],
            tool_input.get("media_type", "both"),
            tool_input.get("year"),
        ))
    if tool_name == "get_media_details":
        return j(await get_media_details(
            tool_input["tmdb_id"],
            tool_input["media_type"],
        ))
    if tool_name == "get_trending":
        return j(await get_trending())
    if tool_name == "get_recommendations":
        return j(await get_recommendations(
            tool_input["tmdb_id"],
            tool_input["media_type"],
        ))
    if tool_name == "get_watch_providers":
        return j(await get_watch_providers(
            tool_input["tmdb_id"],
            tool_input["media_type"],
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
            tool_input.get("media_type", "movie"),
            plex_username,
            tmdb_id=tool_input.get("tmdb_id"),
        ))
    if tool_name == "check_franchise_status":
        return j(await check_franchise_on_plex(
            tool_input["keyword"],
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
    if tool_name == "recommend_from_seed":
        return j(await recommend_from_seed(
            tmdb_id=tool_input["tmdb_id"],
            media_type=tool_input["media_type"],
            plex_username=plex_username,
            max_results=tool_input.get("max_results", 8),
            only_unwatched=tool_input.get("only_unwatched", True),
        ))
    if tool_name == "get_missing_from_collection":
        return j(await get_missing_from_collection(
            tool_input["collection_name"], plex_username
        ))

    # ── Tautulli ──────────────────────────────────────────────────────────────
    if tool_name == "get_popular_on_plex":
        days   = tool_input.get("days", 30)
        result = await get_popular_on_plex(
            stats_count=10,
            time_range=days if days is not None else 30,
        )
        if not result:
            return j({"error": "Ingen data fra Tautulli."})
        return j(result)

    if tool_name == "get_user_watch_stats":
        days = tool_input.get("days", 365)
        return j(await get_user_watch_stats(
            plex_username=plex_username,
            query_days=days if days is not None else 365,
        ))

    if tool_name == "get_user_history":
        return j(await get_user_history(
            plex_username=plex_username,
            query=tool_input.get("query"),
            media_type=tool_input.get("media_type"),
        ))
    if tool_name == "get_recently_added":
        count  = tool_input.get("count", 20)
        # Hent altid minimum 30 fra Tautulli — de 10-20 seneste kan alle være
        # film, og serier forsvinder så helt fra svaret. Med 30 får vi et godt
        # mix af begge typer.
        result = await get_recently_added(count=max(count, 30))

        if result and result.get("movies"):
            async def _lookup_movie(title: str) -> int | None:
                try:
                    hits = await search_media(title, "movie")
                    return hits[0]["id"] if hits else None
                except Exception:
                    return None

            for movie in result["movies"]:
                if not movie.get("tmdb_id"):
                    tmdb_id = await _lookup_movie(movie.get("title", ""))
                    if tmdb_id:
                        movie["tmdb_id"] = tmdb_id
                        logger.info("TMDB film-fallback: '%s' → %s", movie["title"], tmdb_id)

        if result and result.get("episodes"):
            all_episodes = result["episodes"]
            looked_up = await asyncio.gather(
                *[_lookup_tv(e.get("series_name") or e.get("title", "")) for e in all_episodes]
            )
            for ep, tmdb_id in zip(all_episodes, looked_up):
                if tmdb_id:
                    ep["tmdb_id"] = tmdb_id
                    logger.info("TMDB TV-fallback: '%s' → %s", ep.get("series_name"), tmdb_id)

        return j(result)

    return j({"error": f"Ukendt vaerktoej: {tool_name}"})


async def _lookup_tv(series_name: str) -> int | None:
    try:
        hits = await search_media(series_name, "tv")
        return hits[0]["id"] if hits else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Main agentic loop (P1-4: med retry)
# ══════════════════════════════════════════════════════════════════════════════

async def get_ai_response(
    telegram_id: int,
    user_message: str,
    plex_username: str | None = None,
    persona_id: str = "buddy",
    user_first_name: str | None = None,
) -> str:
    """
    Run the full agentic loop and return Buddy's reply.

    System-prompt arkitektur (to blokke):
      Blok 0 — cachet system-prompt (persona-specifik + brugernavn)
      Blok 1 — dynamisk kontekst (dato, plex_username) — aldrig cachet

    P1-4 (v1.1.0): Anthropic API-kald har nu automatisk retry på transient
    fejl (429, 502, 503, 504, netværksfejl) med exponential backoff.
    """
    _histories[telegram_id].append({"role": "user", "content": user_message})
    _trim(telegram_id)

    system_blocks = [
        {
            "type": "text",
            "text": get_system_prompt(persona_id, user_first_name=user_first_name),
            "cache_control": {"type": "ephemeral"},
        }
    ]

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
    })

    tools_with_cache = [dict(t) for t in TOOLS]
    if tools_with_cache:
        tools_with_cache[-1] = {
            **tools_with_cache[-1],
            "cache_control": {"type": "ephemeral"},
        }

    try:
        while True:
            # P1-4: Brug retry-helper i stedet for direkte _client.messages.create
            response = await _call_with_retry(
                create_kwargs={
                    "model":      ANTHROPIC_MODEL,
                    "max_tokens": 4000,
                    "system":     system_blocks,
                    "tools":      tools_with_cache,
                    "messages":   _histories[telegram_id],
                },
                telegram_id=telegram_id,
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
        # Hvis vi når her efter retries, er fejlen ikke transient (eller
        # alle retries er opbrugt). Log med tydelig kontekst.
        logger.error(
            "Anthropic error for tg_id=%s efter retries: %s",
            telegram_id, e,
        )
        _histories.pop(telegram_id, None)
        return "Av, noget gik galt hos mig — prøv igen om lidt! 🔧"


def clear_history(telegram_id: int) -> None:
    _histories.pop(telegram_id, None)
    _last_activity.pop(telegram_id, None)


def check_session_timeout(telegram_id: int) -> bool:
    """
    Returnerer True hvis sessionen er udløbet (ingen aktivitet i _SESSION_TIMEOUT sekunder).
    Opdaterer altid _last_activity til nu.
    """
    import time
    now  = time.monotonic()
    last = _last_activity.get(telegram_id)
    _last_activity[telegram_id] = now
    if last is None:
        return False
    return (now - last) > _SESSION_TIMEOUT