"""
ai_handler.py - Manages all communication with the Anthropic Claude API.
Maintains per-user conversation history in memory for multi-turn dialogue.
"""

import logging
from collections import defaultdict

import anthropic

from config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

# ── Anthropic client ──────────────────────────────────────────────────────────

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "Du er en eksplosiv, humoristisk og super hjælpsom medie-overlord. "
    "Du taler dansk. "
    "Lige nu har du ingen værktøjer, men du glæder dig til at få dem."
)

# ── In-memory conversation history per user ───────────────────────────────────
# Format: { telegram_id: [ {"role": "user"|"assistant", "content": "..."}, ... ] }
# This is reset when the bot restarts. Persistent history lives in PostgreSQL.

_histories: dict[int, list[dict]] = defaultdict(list)

# Maximum number of messages to keep per user in memory (older ones are pruned).
_MAX_HISTORY = 20


def _trim_history(telegram_id: int) -> None:
    """Keep only the most recent _MAX_HISTORY messages for a user."""
    history = _histories[telegram_id]
    if len(history) > _MAX_HISTORY:
        _histories[telegram_id] = history[-_MAX_HISTORY:]


# ── Public API ────────────────────────────────────────────────────────────────

async def get_ai_response(telegram_id: int, user_message: str) -> str:
    """
    Send a message to Claude and return the assistant's reply.

    Maintains conversation history per user so Claude has context
    across multiple turns in the same session.

    Args:
        telegram_id:   The Telegram user ID (used as history key).
        user_message:  The raw text message from the user.

    Returns:
        The assistant's reply as a plain string.
    """
    # Append the new user message to history.
    _histories[telegram_id].append({"role": "user", "content": user_message})
    _trim_history(telegram_id)

    try:
        response = _client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=_histories[telegram_id],
        )

        reply = response.content[0].text

        # Append assistant reply to history for next turn.
        _histories[telegram_id].append({"role": "assistant", "content": reply})
        _trim_history(telegram_id)

        return reply

    except anthropic.APIError as e:
        logger.error("Anthropic API error for user %s: %s", telegram_id, e)
        return "⚠️ Jeg kunne desværre ikke kontakte AI-hjernen lige nu. Prøv igen om lidt."


def clear_history(telegram_id: int) -> None:
    """Clear the in-memory conversation history for a user (e.g. on /start)."""
    _histories.pop(telegram_id, None)
