"""
ai_handler.py - Agentic loop for Buddy.

Imports tools from tools.py and the system prompt from prompts.py,
keeping this file focused solely on the Claude API interaction loop.
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
from tools import TOOLS

logger = logging.getLogger(__name__)

# ── Anthropic client ──────────────────────────────────────────────────────────

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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

    # ── TMDB ──────────────────────────────────────────────────────────────────
    if tool_name == "search_media":
        return json.dumps(await search_media(
            query=tool_input["query"],
            media_type=tool_input.get("media_type", "both"),
        ), ensure_ascii=False)

    if tool_name == "get_media_details":
        return json.dumps(await get_media_details(
            tmdb_id=tool_input["tmdb_id"],
            media_type=tool_input["media_type"],
        ), ensure_ascii=False)

    if tool_name == "get_trending":
        return json.dumps(await get_trending(), ensure_ascii=False)

    if tool_name == "get_recommendations":
        return json.dumps(await get_recommendations(
            tmdb_id=tool_input["tmdb_id"],
            media_type=tool_input["media_type"],
        ), ensure_ascii=False)

    if tool_name == "get_watch_providers":
        return json.dumps(await get_watch_providers(
            tmdb_id=tool_input["tmdb_id"],
            media_type=tool_input["media_type"],
        ), ensure_ascii=False)

    if tool_name == "search_person":
        return json.dumps(await search_person(query=tool_input["query"]), ensure_ascii=False)

    if tool_name == "get_person_filmography":
        return json.dumps(await get_person_filmography(
            person_id=tool_input["person_id"],
        ), ensure_ascii=False)

    if tool_name == "get_now_playing":
        return json.dumps(await get_now_playing(), ensure_ascii=False)

    if tool_name == "get_upcoming":
        return json.dumps(await get_upcoming(), ensure_ascii=False)

    # ── Plex ──────────────────────────────────────────────────────────────────
    if tool_name == "check_plex_library":
        return json.dumps(await check_library(
            title=tool_input["title"],
            year=tool_input.get("year"),
            media_type=tool_input["media_type"],
        ), ensure_ascii=False)

    if tool_name == "get_plex_collection":
        return json.dumps(await get_collection(
            keyword=tool_input["keyword"],
            media_type=tool_input["media_type"],
        ), ensure_ascii=False)

    if tool_name == "get_on_deck":
        return json.dumps(await get_on_deck(), ensure_ascii=False)

    if tool_name == "get_plex_metadata":
        return json.dumps(await get_plex_metadata(
            title=tool_input["title"],
            year=tool_input.get("year"),
        ), ensure_ascii=False)

    if tool_name == "find_unwatched":
        return json.dumps(await find_unwatched(
            media_type=tool_input["media_type"],
            genre=tool_input.get("genre"),
        ), ensure_ascii=False)

    if tool_name == "get_similar_in_library":
        return json.dumps(await get_similar_in_library(
            title=tool_input["title"],
        ), ensure_ascii=False)

    if tool_name == "get_missing_from_collection":
        return json.dumps(await get_missing_from_collection(
            collection_name=tool_input["collection_name"],
        ), ensure_ascii=False)

    # ── Seerr ─────────────────────────────────────────────────────────────────
    if tool_name == "request_movie":
        return json.dumps(await request_movie(
            tmdb_id=tool_input["tmdb_id"],
            category=tool_input.get("category", "standard"),
        ), ensure_ascii=False)

    if tool_name == "request_tv":
        return json.dumps(await request_tv(
            tmdb_id=tool_input["tmdb_id"],
            season_numbers=tool_input["season_numbers"],
            category=tool_input.get("category", "standard"),
        ), ensure_ascii=False)

    if tool_name == "get_all_requests":
        return json.dumps(await get_all_requests(), ensure_ascii=False)

    if tool_name == "get_request_status":
        return json.dumps(await get_request_status(
            title=tool_input["title"],
        ), ensure_ascii=False)

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
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_json,
                        })
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
        return "Av, noget gik galt hos mig — proev igen om lidt! 🔧"


def clear_history(telegram_id: int) -> None:
    """Clear the in-memory conversation history for a user (e.g. on /start)."""
    _histories.pop(telegram_id, None)