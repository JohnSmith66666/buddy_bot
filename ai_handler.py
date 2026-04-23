"""
ai_handler.py - Agentic loop for Buddy.

Each call receives the user's plex_username so all Plex and Seerr
tool calls are automatically scoped to that user.
"""

import json
import logging
from collections import defaultdict

import anthropic

from config import ANTHROPIC_API_KEY
from prompts import SYSTEM_PROMPT
from services.plex_service import (
    check_library,
    find_unwatched,
    get_collection,
    get_missing_from_collection,
    get_on_deck,
    get_plex_metadata,
    get_similar_in_library,
)
from services.seerr_service import (
    get_all_requests,
    get_request_status,
    request_movie,
    request_tv,
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
_MAX_HISTORY = 20


def _trim(telegram_id: int) -> None:
    h = _histories[telegram_id]
    if len(h) > _MAX_HISTORY:
        _histories[telegram_id] = h[-_MAX_HISTORY:]


# ── Tool dispatcher ───────────────────────────────────────────────────────────

async def _dispatch(tool_name: str, tool_input: dict, plex_username: str | None) -> str:
    """Route a tool call to the correct service function."""
    j = lambda x: json.dumps(x, ensure_ascii=False)

    # TMDB — no user context needed
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

    # Plex — user-scoped
    if tool_name == "check_plex_library":
        return j(await check_library(tool_input["title"], tool_input.get("year"), tool_input["media_type"], plex_username))
    if tool_name == "get_plex_collection":
        return j(await get_collection(tool_input["keyword"], tool_input["media_type"], plex_username))
    if tool_name == "get_on_deck":
        return j(await get_on_deck(plex_username))
    if tool_name == "get_plex_metadata":
        return j(await get_plex_metadata(tool_input["title"], tool_input.get("year"), plex_username))
    if tool_name == "find_unwatched":
        return j(await find_unwatched(tool_input["media_type"], tool_input.get("genre"), plex_username))
    if tool_name == "get_similar_in_library":
        return j(await get_similar_in_library(tool_input["title"], plex_username))
    if tool_name == "get_missing_from_collection":
        return j(await get_missing_from_collection(tool_input["collection_name"], plex_username))

    # Seerr
    if tool_name == "request_movie":
        return j(await request_movie(tool_input["tmdb_id"], tool_input.get("category", "standard")))
    if tool_name == "request_tv":
        return j(await request_tv(tool_input["tmdb_id"], tool_input["season_numbers"], tool_input.get("category", "standard")))
    if tool_name == "get_all_requests":
        return j(await get_all_requests(plex_username))
    if tool_name == "get_request_status":
        return j(await get_request_status(tool_input["title"], plex_username))

    # Tautulli
    if tool_name == "get_popular_on_plex":
        return j(await get_popular_on_plex(
            stats_count=tool_input.get("stats_count", 10),
            time_range=tool_input.get("time_range", 30),
        ))

    if tool_name == "get_user_watch_stats":
        if not plex_username:
            return j({"error": "Intet Plex-brugernavn fundet — kan ikke hente personlig statistik."})
        return j(await get_user_watch_stats(
            plex_username,
            query_days=tool_input.get("query_days", tool_input.get("days", 365)),
        ))

    if tool_name == "get_user_history":
        if not plex_username:
            return j({"error": "Intet Plex-brugernavn fundet — kan ikke hente historik."})
        return j(await get_user_history(
            plex_username,
            length=tool_input.get("length", tool_input.get("count", 10)),
        ))

    if tool_name == "get_recently_added":
        return j(await get_recently_added(
            count=tool_input.get("count", 10),
        ))

    return j({"error": f"Ukendt vaerktoej: {tool_name}"})


# ── Public API ────────────────────────────────────────────────────────────────

async def get_ai_response(
    telegram_id: int,
    user_message: str,
    plex_username: str | None = None,
) -> str:
    """
    Run the full agentic loop and return Buddy's reply.
    plex_username is threaded through to every Plex and Seerr call.
    """
    _histories[telegram_id].append({"role": "user", "content": user_message})
    _trim(telegram_id)

    system = SYSTEM_PROMPT
    if plex_username:
        system += f"\n\nDen aktuelle bruger hedder '{plex_username}' på Plex."

    try:
        while True:
            response = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system,
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
                        logger.info("Tool call: %s(%s)", block.name, block.input)
                        try:
                            result = await _dispatch(block.name, block.input, plex_username)
                        except Exception as dispatch_err:
                            logger.error(
                                "Tool dispatch error for '%s': %s",
                                block.name,
                                dispatch_err,
                            )
                            result = json.dumps(
                                {"error": f"Vaerktoejet '{block.name}' fejlede: {dispatch_err}"},
                                ensure_ascii=False,
                            )

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
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