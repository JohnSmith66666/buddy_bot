"""
ai_handler.py - Agentic loop for Buddy.

CHANGES vs previous version:
  - Prompt caching aktiveret: system prompt og tools caches hos Anthropic.
    Reducerer input-tokens med ~90% for gentagne kald (kun charged for cache misses).
  - _MAX_HISTORY reduceret fra 20 til 10.
  - Tool-resultater trimmes til max 2000 chars før de sendes til Claude.
    Forhindrer enorme Tautulli/Plex JSON-payloads i at spise tokens.
  - check_franchise_status routed til check_franchise_on_plex() i plex_service.
    Erstatter den gamle get_plex_collection-routing for franchise-søgninger.
  - FIX: SEARCH_SIGNAL konstant genindsat — manglede efter seneste omskrivning
    og forårsagede ImportError ved opstart.
"""

import json
import logging
from collections import defaultdict

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from prompts import SYSTEM_PROMPT
from services.plex_service import (
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
from tools import TOOLS

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

_histories: dict[int, list[dict]] = defaultdict(list)
_MAX_HISTORY = 10
_MAX_TOOL_RESULT_CHARS = 2000

# Signal som Claude returnerer for at trigge bestillingsflow i main.py
SEARCH_SIGNAL = "SHOW_SEARCH_RESULTS:"


def _trim(telegram_id: int) -> None:
    h = _histories[telegram_id]
    if len(h) > _MAX_HISTORY:
        _histories[telegram_id] = h[-_MAX_HISTORY:]


def _slim_data(data, max_list_items: int = 10):
    """
    Rekursivt trim data-strukturen FØR JSON-serialisering.
    Lister cappes til max_list_items — aldrig hård string-truncation.
    Garanterer altid gyldig JSON output.
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
    Trim tool result til gyldig, kompakt JSON.
    Bruger strukturel trimming (ikke hård string-truncation) så JSON altid er valid.
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
        # Andet pas: aggressiv trimming til 5 items
        slimmed2 = _slim_data(data, max_list_items=5)
        return json.dumps(slimmed2, ensure_ascii=False, separators=(",", ":"))
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Could not parse tool result as JSON: %s", e)
        return result


async def _dispatch(tool_name: str, tool_input: dict, plex_username: str | None) -> str:
    j = lambda x: json.dumps(x, ensure_ascii=False)

    # ── TMDB ──────────────────────────────────────────────────────────────────
    if tool_name == "search_media":
        return j(await search_media(tool_input["query"], tool_input.get("media_type", "both")))
    if tool_name == "get_media_details":
        return j(await get_media_details(tool_input["tmdb_id"], tool_input["media_type"]))
    if tool_name == "get_trending":
        return j(await get_trending())
    if tool_name == "get_recommendations":
        return j(await get_recommendations(tool_input["tmdb_id"], tool_input["media_type"]))
    if tool_name == "get_watch_providers":
        return j(await get_watch_providers(tool_input["tmdb_id"], tool_input["media_type"]))
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
    if tool_name == "search_plex_by_actor":
        return j(await search_by_actor(
            tool_input["actor_name"],
            tool_input.get("media_type", "movie"),
            plex_username,
        ))

    # ── Tautulli ──────────────────────────────────────────────────────────────
    if tool_name == "get_popular_on_plex":
        return j(await get_popular_on_plex(
            stats_count=tool_input.get("stats_count", 10),
            time_range=tool_input.get("days", 30),
        ))
    if tool_name == "get_user_watch_stats":
        if not plex_username:
            return j({"error": "Intet Plex-brugernavn fundet."})
        return j(await get_user_watch_stats(
            plex_username,
            query_days=tool_input.get("days", 365),
        ))
    if tool_name == "get_user_history":
        if not plex_username:
            return j({"error": "Intet Plex-brugernavn fundet."})
        return j(await get_user_history(
            plex_username,
            length=tool_input.get("length", 10),
            query=tool_input.get("query"),
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

    Prompt caching:
      - system prompt caches med cache_control: {"type": "ephemeral"}
      - tools-listen caches ligeledes
      Anthropic cacher disse i 5 minutter. Ved cache hit betales kun 10% af
      normal input-pris. Cache miss koster 25% ekstra men betales kun én gang.
    """
    _histories[telegram_id].append({"role": "user", "content": user_message})
    _trim(telegram_id)

    system_text = SYSTEM_PROMPT
    if plex_username:
        system_text += f"\n\nDen aktuelle bruger hedder '{plex_username}' på Plex."

    system_with_cache = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

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
                max_tokens=1024,
                system=system_with_cache,
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
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info("Tool call: %s(%s)", block.name, block.input)
                        try:
                            raw_result = await _dispatch(block.name, block.input, plex_username)
                            result     = _trim_tool_result(raw_result)
                        except Exception as e:
                            logger.error("Tool dispatch error '%s': %s", block.name, e)
                            result = json.dumps({"error": str(e)}, ensure_ascii=False)
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     result,
                        })
                _histories[telegram_id].append({"role": "user", "content": tool_results})
                _trim(telegram_id)
                continue

            reply = next(
                (b.text for b in response.content if hasattr(b, "text")),
                "Av, noget gik galt — prøv igen om lidt! 🔧",
            )
            _histories[telegram_id].append({"role": "assistant", "content": reply})
            _trim(telegram_id)
            return reply

    except anthropic.APIError as e:
        logger.error("Anthropic error for user %s: %s", telegram_id, e)
        _histories.pop(telegram_id, None)
        return "Av, noget gik galt hos mig — prøv igen om lidt! 🔧"


def clear_history(telegram_id: int) -> None:
    _histories.pop(telegram_id, None)