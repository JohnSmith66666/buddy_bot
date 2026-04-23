"""
ai_handler.py - Agentic loop for Buddy.

Each call receives the user's plex_username so all Plex and Seerr
tool calls are automatically scoped to that user.

FIX: Now uses anthropic.AsyncAnthropic + await on messages.create()
so the event loop is never blocked during API calls.
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

# ── Async Anthropic client ────────────────────────────────────────────────────
# AsyncAnthropic ensures messages.create() is awaited and never blocks
# the event loop — critical in a python-telegram-bot async application.

_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

_histories: dict[int, list[dict]] = defaultdict(list)
_MAX_HISTORY = 20


def _trim(telegram_id: int) -> None:
    """Keep the per-user history within the configured limit."""
    h = _histories[telegram_id]
    if len(h) > _MAX_HISTORY:
        _histories[telegram_id] = h[-_MAX_HISTORY:]


# ── Tool dispatcher ───────────────────────────────────────────────────────────

async def _dispatch(tool_name: str, tool_input: dict, plex_username: str | None) -> str:
    """Route a tool call to the correct service function."""
    j = lambda x: json.dumps(x, ensure_ascii=False)

    try:
        # ── TMDB — no user context needed ─────────────────────────────────────
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

        # ── Plex — user-scoped ────────────────────────────────────────────────
        if tool_name == "check_plex_library":
            return j(await check_library(
                tool_input["title"],
                tool_input.get("year"),
                tool_input["media_type"],
                plex_username,
            ))
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

        # ── Seerr ─────────────────────────────────────────────────────────────
        if tool_name == "request_movie":
            return j(await request_movie(tool_input["tmdb_id"], tool_input.get("category", "standard")))
        if tool_name == "request_tv":
            return j(await request_tv(
                tool_input["tmdb_id"],
                tool_input["season_numbers"],
                tool_input.get("category", "standard"),
            ))
        if tool_name == "get_all_requests":
            return j(await get_all_requests(plex_username))
        if tool_name == "get_request_status":
            return j(await get_request_status(tool_input["title"], plex_username))

        # ── Unknown tool ──────────────────────────────────────────────────────
        logger.warning("Unknown tool requested: %s", tool_name)
        return j({"error": f"Ukendt værktøj: {tool_name}"})

    except Exception as exc:
        logger.exception("Tool '%s' raised an exception: %s", tool_name, exc)
        return j({"error": f"Fejl i {tool_name}: {exc}"})


# ── Public API ────────────────────────────────────────────────────────────────

async def get_ai_response(
    telegram_id: int,
    user_message: str,
    plex_username: str | None = None,
) -> str:
    """
    Run the full agentic loop and return Buddy's reply.

    Uses AsyncAnthropic so every API call is properly awaited —
    the event loop is never blocked regardless of Claude's response time.

    plex_username is threaded through to every Plex and Seerr call
    so all data is scoped to the individual user.
    """
    _histories[telegram_id].append({"role": "user", "content": user_message})
    _trim(telegram_id)

    # Inject plex context into the system prompt if username is known.
    system = SYSTEM_PROMPT
    if plex_username:
        system += f"\n\nDen aktuelle bruger hedder '{plex_username}' på Plex."

    try:
        while True:
            # FIXED: await on async client — never blocks the event loop.
            response = await _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system,
                tools=TOOLS,
                messages=_histories[telegram_id],
            )

            # ── Tool use round ────────────────────────────────────────────────
            if response.stop_reason == "tool_use":
                _histories[telegram_id].append(
                    {"role": "assistant", "content": response.content}
                )

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info("Tool call: %s(%s)", block.name, block.input)
                        result = await _dispatch(block.name, block.input, plex_username)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                _histories[telegram_id].append({"role": "user", "content": tool_results})
                _trim(telegram_id)
                continue  # Loop back to Claude with the tool results.

            # ── Final text reply ──────────────────────────────────────────────
            reply = next(
                (b.text for b in response.content if hasattr(b, "text")),
                "Av, noget gik galt — prøv igen om lidt! 🔧",
            )
            _histories[telegram_id].append({"role": "assistant", "content": reply})
            _trim(telegram_id)
            return reply

    except anthropic.APIError as e:
        logger.error("Anthropic API error for user %s: %s", telegram_id, e)
        return "Av, noget gik galt hos mig — prøv igen om lidt! 🔧"


def clear_history(telegram_id: int) -> None:
    """Wipe the in-memory conversation history for a user (e.g. on /start)."""
    _histories.pop(telegram_id, None)