"""
features/watchlist/messages.py - Danske tekst-templates for watchlist UI.

Holder al Markdown-formaterede tekst-strenge ét sted så det er nemt at
opdatere copy uden at rode i handler-logik.

CHANGES (v0.2.0 — vis ratings i listen):
  - FIX: format_watchlist_item() viser nu rating med ⭐ emoji
    (samme stil som watch flow's _format_results_message i main.py).
  - Tidligere: rating blev gemt i DB men aldrig vist i UI.
  - Format: "🎬 *Titel* (År) ⭐ 8.7"
  - Hvis rating mangler, udelades den graceful (intet "⭐ N/A").

CHANGES (v0.1.0 — initial):
  - format_watchlist_header() — overskrift med titel-count
  - format_watchlist_item() — én linje per titel
  - format_empty_watchlist() — tom-tilstand
  - format_sync_status() — "synkroniseret for X siden" linje
  - format_loading_message() — under sync
  - format_error_message() — fejl-besked
"""

from datetime import datetime, timezone


# ══════════════════════════════════════════════════════════════════════════════
# Tom watchlist
# ══════════════════════════════════════════════════════════════════════════════

EMPTY_WATCHLIST = (
    "📺 *Min watchlist*\n\n"
    "_Du har ikke gemt nogen titler endnu._\n\n"
    "Når du finder en spændende film eller serie, "
    "kan du gemme den med 📌-knappen — så ligger den klar her til senere. 🎬\n\n"
    "_Tip: Watchlist'en er synkroniseret med din Plex Discover liste._"
)


# ══════════════════════════════════════════════════════════════════════════════
# Loading
# ══════════════════════════════════════════════════════════════════════════════

LOADING_FIRST_TIME = (
    "📺 *Min watchlist*\n\n"
    "🔄 Henter din watchlist fra Plex..."
)

LOADING_SYNC = (
    "📺 *Min watchlist*\n\n"
    "🔄 Synkroniserer med Plex..."
)


# ══════════════════════════════════════════════════════════════════════════════
# Header
# ══════════════════════════════════════════════════════════════════════════════

def format_watchlist_header(total_count: int) -> str:
    """Overskrift med antal titler."""
    if total_count == 0:
        return "📺 *Min watchlist*"
    return f"📺 *Min watchlist* ({total_count})"


# ══════════════════════════════════════════════════════════════════════════════
# Item formatting
# ══════════════════════════════════════════════════════════════════════════════

def format_watchlist_item(item: dict) -> str:
    """
    Formatér én watchlist-titel som Markdown.

    Args:
      item: dict med {tmdb_id, media_type, title, year, rating}

    Eksempel output (med rating):
      🎬 *The Matrix* (1999) ⭐ 8.7
         /info_movie_603

    Eksempel output (uden rating):
      🎬 *The Matrix* (1999)
         /info_movie_603

    v0.2.0: Tilføjet rating-visning med ⭐ emoji (matcher watch flow stil).
    """
    media_emoji = "🎬" if item.get("media_type") == "movie" else "📺"
    title       = item.get("title") or f"#{item.get('tmdb_id', '?')}"
    year        = item.get("year")
    rating      = item.get("rating")
    tmdb_id     = item.get("tmdb_id")
    media_type  = item.get("media_type", "movie")

    year_str = f" ({year})" if year else ""

    # v0.2.0: vis rating hvis tilgængelig (samme stil som watch flow)
    # Cast til float defensivt — rating kan være Decimal fra PostgreSQL NUMERIC
    rating_str = ""
    if rating is not None:
        try:
            rating_float = float(rating)
            if 0.0 < rating_float <= 10.0:
                rating_str = f" ⭐ {rating_float:.1f}"
        except (ValueError, TypeError):
            pass  # Hvis rating ikke kan castes, vis ingen stjerne

    info_link = f"   /info_{media_type}_{tmdb_id}" if tmdb_id else ""

    return f"{media_emoji} *{_escape_md(title)}*{year_str}{rating_str}\n{info_link}"


def _escape_md(text: str) -> str:
    """Minimal Markdown-escape for titler."""
    if not text:
        return ""
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Sync status footer
# ══════════════════════════════════════════════════════════════════════════════

def format_sync_status(
    sync_status: str,
    last_synced_at: datetime | None,
) -> str:
    """
    Formatér sync-status linje til footer.

    Args:
      sync_status:    'synced', 'cached', eller 'error'
      last_synced_at: timestamp eller None
    """
    if sync_status == "error":
        return "_⚠️ Kunne ikke synkronisere med Plex — viser cached data._"

    if last_synced_at is None:
        return "_Synkroniseret med Plex._"

    delta = datetime.now(timezone.utc) - last_synced_at
    seconds = int(delta.total_seconds())

    if seconds < 60:
        ago = "lige nu"
    elif seconds < 3600:
        minutes = seconds // 60
        ago = f"for {minutes} min siden"
    elif seconds < 86400:
        hours = seconds // 3600
        ago = f"for {hours} time{'r' if hours > 1 else ''} siden"
    else:
        days = seconds // 86400
        ago = f"for {days} dag{'e' if days > 1 else ''} siden"

    if sync_status == "synced":
        return f"_🔄 Synkroniseret {ago}._"
    else:  # cached
        return f"_💾 Cached fra Plex sync {ago} (opdateres ved næste åbn)._"


# ══════════════════════════════════════════════════════════════════════════════
# Fuld besked-builder
# ══════════════════════════════════════════════════════════════════════════════

def format_full_watchlist_message(
    items: list[dict],
    sync_status: str,
    last_synced_at: datetime | None,
) -> str:
    """
    Byg den fulde watchlist-besked klar til Telegram.

    Args:
      items:          Liste af watchlist-items med metadata
      sync_status:    'synced', 'cached', eller 'error'
      last_synced_at: timestamp for sidste sync
    """
    if not items:
        # Tom watchlist
        return f"{EMPTY_WATCHLIST}\n\n{format_sync_status(sync_status, last_synced_at)}"

    parts = [format_watchlist_header(len(items)), ""]

    for item in items:
        parts.append(format_watchlist_item(item))
        parts.append("")  # Tom linje mellem items

    # Fjern sidste tomme linje
    if parts and parts[-1] == "":
        parts.pop()

    parts.append("")
    parts.append(format_sync_status(sync_status, last_synced_at))

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Sync result-besked (bruges efter manuel "🔄 Sync nu")
# ══════════════════════════════════════════════════════════════════════════════

def format_sync_result(sync_result: dict) -> str:
    """
    Formatér sync-result som besked til brugeren.

    Args:
      sync_result: dict fra sync_user_watchlist()
    """
    if sync_result.get("error"):
        return (
            f"⚠️ *Synkronisering fejlede*\n\n"
            f"`{sync_result['error']}`\n\n"
            f"Prøv igen om lidt."
        )

    added = sync_result.get("added", 0)
    removed = sync_result.get("removed", 0)
    total = sync_result.get("total", 0)

    if added == 0 and removed == 0:
        return (
            f"✅ *Allerede synkroniseret*\n\n"
            f"_Din watchlist har {total} titler — alt er up-to-date._"
        )

    lines = ["✅ *Synkronisering færdig*", ""]

    if added > 0:
        lines.append(f"  ➕ *{added}* nye titler tilføjet")
    if removed > 0:
        lines.append(f"  ➖ *{removed}* titler fjernet (slettet i Plex)")

    lines.append("")
    lines.append(f"_Total: {total} titler i watchlist._")

    return "\n".join(lines)