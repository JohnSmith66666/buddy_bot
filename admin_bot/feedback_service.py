"""
services/feedback_service.py - Shared feedback constants and formatting helpers.

CHANGES (v0.2.0 — Batch A polering):
  - FORBEDRET: escape_md() er nu fuldt robust mod alle Telegram MarkdownV1
    special-tegn (_, *, `, [). Tidligere kunne brugere skrive *test* eller
    _underscore_ og knække vores formattering.
  - NY: format_user_display(username, name, telegram_id) — bygger den bedst
    mulige bruger-display givet hvad vi har. Fallback-rækkefølge:
      1. "@username (Name)" hvis begge findes
      2. "@username" hvis kun username
      3. "Name" hvis kun first_name
      4. "user_<id>" som sidste fallback
    Bruges nu OVERALT i stedet for ad-hoc fallback-logik.
  - FORBEDRET: format_admin_notification() tager nu valgfri 'is_first_time'
    parameter. Hvis True, tilføjer "🆕 NY TESTER" tag øverst i notifikationen
    så Jesper instant ved at det er førstegangs-feedback.
  - REFACTOR: format_feedback_summary() og format_feedback_detail() bruger
    nu den nye format_user_display() helper.

UNCHANGED (v0.1.0 — initial):
  - FEEDBACK_TYPES dict
  - format_user_thanks()
  - format_user_received_reply()
  - format_feedback_summary() (men bruger ny helper)
  - format_feedback_detail() (men bruger ny helper)
  - format_stats()
  - format_timestamp()

DESIGN-PRINCIPPER:
  - Ren formattering — ingen DB-kald eller bot-API.
  - Genbruges af main.py (Buddy) OG admin_bot/feedback_handlers.py.
  - Dansk persona-stil bevares ('din feedback er registreret 🙏').
  - MarkdownV1 ikke MarkdownV2 (matcher resten af projektet).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


# ══════════════════════════════════════════════════════════════════════════════
# Feedback-kategorier
# ══════════════════════════════════════════════════════════════════════════════

FEEDBACK_TYPES: dict[str, dict] = {
    "idea": {
        "id":             "idea",
        "label":          "💡 Idé/Forslag",
        "emoji":          "💡",
        "danish_word":    "idé",
        "admin_tag":      "💡 IDÉ",
        "thanks_followup": "Den lægger jeg på listen — godt tænkt!",
    },
    "bug": {
        "id":             "bug",
        "label":          "🐛 Bug/Fejl",
        "emoji":          "🐛",
        "danish_word":    "bug",
        "admin_tag":      "🐛 BUG",
        "thanks_followup": "Beklager besværet! Jeg kigger på det hurtigst muligt.",
    },
    "question": {
        "id":             "question",
        "label":          "❓ Spørgsmål",
        "emoji":          "❓",
        "danish_word":    "spørgsmål",
        "admin_tag":      "❓ SPØRGSMÅL",
        "thanks_followup": "Jeg vender tilbage med et svar så snart jeg kan.",
    },
    "praise": {
        "id":             "praise",
        "label":          "🙏 Ros/Tak",
        "emoji":          "🙏",
        "danish_word":    "ros",
        "admin_tag":      "🙏 ROS",
        "thanks_followup": "Det varmer at høre — tak fordi du tog dig tiden! ❤️",
    },
}


def get_feedback_type(type_id: str) -> dict | None:
    """Returnér feedback-type config eller None hvis ukendt ID."""
    return FEEDBACK_TYPES.get(type_id)


def list_feedback_type_ids() -> list[str]:
    """Returnér alle feedback-type IDs i defineret rækkefølge."""
    return list(FEEDBACK_TYPES.keys())


def validate_feedback_type(type_id: str) -> bool:
    """True hvis type_id er en gyldig feedback-kategori."""
    return type_id in FEEDBACK_TYPES


# ══════════════════════════════════════════════════════════════════════════════
# Markdown-escape (FORBEDRET v0.2.0)
# ══════════════════════════════════════════════════════════════════════════════

# Telegram MarkdownV1 har 4 special-tegn der skal escapes hvis de skal vises
# som bogstaver: _ * ` [ ]
# Bemærk: ] er normalt ikke et problem alene, men vi escaper for sikkerhed.
#
# Hvis vi IKKE escaper, kan brugere skrive ting som:
#   - "*test*" → bliver fed tekst i Telegram
#   - "_underscore_" → bliver kursiv
#   - "`code`" → bliver monospace
#   - "[link](url)" → forsøger at parse som link og kan fejle
_MD_ESCAPE_RE = re.compile(r"([_*`\[\]])")


def escape_md(text: str | None) -> str:
    """
    Escape tegn der kan ødelægge Markdown V1 i bruger-input.

    v0.2.0: Robusthed forbedret. Tester nu mod ALLE 4 special-tegn:
      - underscore (_)
      - asterisk (*)
      - backtick (`)
      - square brackets ([ ])

    Bruges på alt indhold der kommer FRA brugeren (besked, navn, username).
    Egen formattering (overskrifter, bullets) bygges udenom escape-laget.

    Eksempler:
      escape_md("hello *world*")    → "hello \\*world\\*"
      escape_md("snake_case_name")  → "snake\\_case\\_name"
      escape_md("`code` block")     → "\\`code\\` block"
      escape_md(None)               → ""
      escape_md("")                 → ""
    """
    if not text:
        return ""
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


# ══════════════════════════════════════════════════════════════════════════════
# Bruger-display helper (NY v0.2.0)
# ══════════════════════════════════════════════════════════════════════════════

def format_user_display(
    username: str | None,
    name: str | None,
    telegram_id: int | str | None = None,
    style: str = "full",
) -> str:
    """
    Byg den bedst mulige bruger-display givet hvad vi har om brugeren.

    Telegram-brugere har varierende metadata:
      - Nogle har @username (offentligt synligt navn)
      - Nogle har first_name (deres profilnavn)
      - Nogle har begge dele
      - Få har kun telegram_id (sjældent men muligt)

    Args:
      username:    Telegram @username uden @ (eller None)
      name:        Telegram first_name (eller None)
      telegram_id: Numerisk Telegram ID (kun fallback)
      style:       'full'  → "@username (Name)"  — bedst når plads er der
                   'short' → "@username"          — kort til lister
                   'name'  → "Name" prioriteret  — venligt hvis begge findes

    Returns:
      Markdown-escaped bruger-display string. Aldrig tom.

    Eksempler:
      format_user_display("john", "John Smith", 731397952, "full")
        → "@john \\(John Smith\\)"
      format_user_display(None, "John", 731397952, "full")
        → "John"
      format_user_display(None, None, 731397952, "full")
        → "user\\_731397952"
    """
    has_username = bool(username and username.strip())
    has_name     = bool(name and name.strip())

    safe_username = escape_md(username) if has_username else None
    safe_name     = escape_md(name)     if has_name     else None

    # Style: name → prioriter first_name (venligst-virkende)
    if style == "name":
        if has_name:
            return safe_name
        if has_username:
            return f"@{safe_username}"
        return f"user\\_{telegram_id}" if telegram_id else "ukendt bruger"

    # Style: short → kun username eller fallback
    if style == "short":
        if has_username:
            return f"@{safe_username}"
        if has_name:
            return safe_name
        return f"user\\_{telegram_id}" if telegram_id else "ukendt"

    # Style: full (default) → "@username (Name)"
    if has_username and has_name:
        return f"@{safe_username} \\({safe_name}\\)"
    if has_username:
        return f"@{safe_username}"
    if has_name:
        return safe_name
    if telegram_id:
        return f"user\\_{telegram_id}"
    return "ukendt bruger"


# ══════════════════════════════════════════════════════════════════════════════
# Tidsstempel-formattering (dansk stil)
# ══════════════════════════════════════════════════════════════════════════════

_DK_MONTHS = [
    "januar", "februar", "marts", "april", "maj", "juni",
    "juli", "august", "september", "oktober", "november", "december",
]


def format_timestamp(dt: datetime | None) -> str:
    """
    Formatér timestamp på dansk: '28. apr 2026, 14:23'.

    Returnerer '?' hvis dt er None. Konverterer til lokal-tid (UTC fra DB).
    """
    if dt is None:
        return "?"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    month_short = _DK_MONTHS[dt.month - 1][:3]
    return f"{dt.day}. {month_short} {dt.year}, {dt.hour:02d}:{dt.minute:02d}"


# ══════════════════════════════════════════════════════════════════════════════
# Bruger-vendte beskeder (vises i Buddy)
# ══════════════════════════════════════════════════════════════════════════════

def format_user_thanks(feedback_type: str, feedback_id: int) -> str:
    """
    Tak-besked til brugeren efter indsendelse af feedback.

    Args:
      feedback_type: 'idea' | 'bug' | 'question' | 'praise'
      feedback_id:   ID på den oprettede record (vises som referencenummer)

    Returns:
      Markdown-formatteret besked (Buddy-persona stil).
    """
    ft = get_feedback_type(feedback_type)
    if ft is None:
        return f"✅ *Tak for din feedback!*\n\n_Reference: \\#{feedback_id}_"

    return (
        f"✅ *Tak for din {ft['danish_word']}!*\n\n"
        f"{ft['thanks_followup']}\n\n"
        f"_Reference: \\#{feedback_id}_"
    )


def format_user_received_reply(
    feedback_id: int,
    feedback_type: str,
    original_message: str,
    admin_reply: str,
) -> str:
    """
    Besked brugeren modtager når admin har svaret på deres feedback.

    Sendes via Buddy main-bot (admin-bot kender brugerens telegram_id og
    bruger BUDDY_BOT_TOKEN til at sende beskeden via Buddy).

    Format minder om en email-tråd: brugeren ser deres oprindelige besked
    citeret, derefter admins svar tydeligt markeret.
    """
    ft = get_feedback_type(feedback_type)
    type_label = ft["label"] if ft else "Feedback"

    quote = original_message.strip()
    if len(quote) > 200:
        quote = quote[:197] + "..."

    safe_quote = escape_md(quote)
    safe_reply = escape_md(admin_reply.strip())

    return (
        f"💬 *Du har fået svar på din feedback*\n"
        f"_(Reference: \\#{feedback_id} — {type_label})_\n\n"
        f"📝 *Din oprindelige besked:*\n"
        f"_{safe_quote}_\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✉️ *Svar fra Jesper:*\n\n"
        f"{safe_reply}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"_Vil du svare tilbage? Tryk på 💬 Feedback-knappen igen og lav "
        f"en ny indberetning — referér gerne til \\#{feedback_id}._"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Admin-vendte beskeder (vises i admin-bot)
# ══════════════════════════════════════════════════════════════════════════════

def format_admin_notification(
    feedback: dict,
    is_first_time: bool = False,
) -> str:
    """
    Formatér ny-feedback notifikation til admin.

    v0.2.0:
      - Tilføjet 'is_first_time' parameter.
        Hvis True, vises "🆕 NY TESTER" tag øverst.
      - Bruger nu format_user_display() for konsistent fallback.

    Args:
      feedback:      Dict fra database.get_feedback() eller submit_feedback().
      is_first_time: True hvis denne bruger sender feedback for første gang.
                     Tilføjer "🆕 NY TESTER" tag øverst i notifikationen.

    Returns:
      Markdown-formatteret tekst klar til at sende til admin.
    """
    ft         = get_feedback_type(feedback.get("feedback_type", ""))
    tag        = ft["admin_tag"] if ft else "📨 FEEDBACK"
    fb_id      = feedback.get("id", "?")
    username   = feedback.get("telegram_username")
    name       = feedback.get("telegram_name")
    user_id    = feedback.get("telegram_id", "?")
    message    = feedback.get("message", "") or ""
    file_ids   = feedback.get("screenshot_file_ids", []) or []
    created_at = feedback.get("created_at")

    user_line = format_user_display(username, name, user_id, style="full")

    safe_message = escape_md(message)

    # Header — tilføj "NY TESTER" badge hvis det er første feedback
    header_lines = []
    if is_first_time:
        header_lines.append("🆕 *NY TESTER* — første feedback nogensinde!")
        header_lines.append("")

    header_lines.append(f"{tag} \\#{fb_id}")

    lines = header_lines + [
        "",
        f"👤 *Fra:* {user_line}",
        f"🕐 *Tid:* {format_timestamp(created_at)}",
        "",
        f"💬 *Besked:*",
        safe_message,
    ]

    if file_ids:
        count = len(file_ids)
        screenshots_word = "screenshot" if count == 1 else "screenshots"
        lines.append("")
        lines.append(f"📷 *{count} {screenshots_word} vedhæftet*")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━",
        f"📋 `/view {fb_id}` — se fuld detalje",
        f"💬 `/reply {fb_id} <besked>` — svar bruger",
        f"✅ `/resolve {fb_id}` — markér som løst",
    ])

    return "\n".join(lines)


def format_feedback_summary(feedback: dict) -> str:
    """
    Kort 1-2 linjer opsummering brugt i /list.

    v0.2.0: Bruger format_user_display() for konsistent fallback.

    Format eksempel:
      🐛 #42 — @testbruger (28. apr 14:23) [new]
        "🍿-knappen virker ikke når jeg trykker..."

    Lange beskeder trimmes til 60 tegn.
    """
    ft       = get_feedback_type(feedback.get("feedback_type", ""))
    emoji    = ft["emoji"] if ft else "📨"
    fb_id    = feedback.get("id", "?")
    username = feedback.get("telegram_username")
    name     = feedback.get("telegram_name")
    user_id  = feedback.get("telegram_id", "?")
    status   = feedback.get("status", "new")
    message  = feedback.get("message", "") or ""
    created_at = feedback.get("created_at")

    user_short = format_user_display(username, name, user_id, style="short")

    preview = message.strip().replace("\n", " ")
    if len(preview) > 60:
        preview = preview[:57] + "..."

    status_emoji = {
        "new":      "🆕",
        "seen":     "👁",
        "replied":  "💬",
        "resolved": "✅",
    }.get(status, "·")

    safe_preview = escape_md(preview)

    line1 = (
        f"{emoji} *\\#{fb_id}* — {user_short} "
        f"\\({format_timestamp(created_at)}\\) {status_emoji}"
    )
    line2 = f"   _{safe_preview}_"

    return f"{line1}\n{line2}"


def format_feedback_detail(feedback: dict) -> str:
    """
    Fuld detalje brugt i /view <id>.

    v0.2.0: Bruger format_user_display() for konsistent fallback.

    Ligner format_admin_notification men inkluderer admin_reply hvis der
    findes ét, og status-historik.
    """
    ft         = get_feedback_type(feedback.get("feedback_type", ""))
    tag        = ft["admin_tag"] if ft else "📨 FEEDBACK"
    type_word  = ft["danish_word"] if ft else "feedback"
    fb_id      = feedback.get("id", "?")
    username   = feedback.get("telegram_username")
    name       = feedback.get("telegram_name")
    user_id    = feedback.get("telegram_id", "?")
    message    = feedback.get("message", "") or ""
    file_ids   = feedback.get("screenshot_file_ids", []) or []
    status     = feedback.get("status", "new")
    admin_reply = feedback.get("admin_reply")
    admin_replied_at = feedback.get("admin_replied_at")
    created_at = feedback.get("created_at")

    user_line = format_user_display(username, name, user_id, style="full")

    status_label = {
        "new":      "🆕 Ny — ikke set endnu",
        "seen":     "👁 Set af admin",
        "replied":  "💬 Besvaret",
        "resolved": "✅ Løst",
    }.get(status, status)

    safe_message = escape_md(message)

    lines = [
        f"{tag} \\#{fb_id}",
        f"_(detaljer for {type_word}\\-feedback)_",
        "",
        f"👤 *Fra:* {user_line}",
        f"🆔 *Telegram ID:* `{user_id}`",
        f"🕐 *Indsendt:* {format_timestamp(created_at)}",
        f"📊 *Status:* {status_label}",
        "",
        f"💬 *Besked:*",
        safe_message,
    ]

    if file_ids:
        count = len(file_ids)
        word  = "screenshot" if count == 1 else "screenshots"
        lines.append("")
        lines.append(f"📷 *{count} {word} vedhæftet* \\(sendes separat\\)")

    if admin_reply:
        safe_reply = escape_md(admin_reply)
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━",
            f"✉️ *Dit svar* \\(sendt {format_timestamp(admin_replied_at)}\\):",
            "",
            safe_reply,
        ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━",
        "*Kommandoer:*",
    ])

    if status != "replied" and status != "resolved":
        lines.append(f"💬 `/reply {fb_id} <besked>` — svar bruger")
    if status != "resolved":
        lines.append(f"✅ `/resolve {fb_id}` — markér som løst")
    if status == "new":
        lines.append(f"👁 `/seen {fb_id}` — markér som set \\(uden svar\\)")

    return "\n".join(lines)


def format_stats(stats: dict) -> str:
    """
    Format /stats output.

    Args:
      stats: Dict fra database.count_feedback_by_status().

    Returns:
      Markdown-formatteret stats-rapport.
    """
    total     = stats.get("total", 0)
    by_status = stats.get("by_status", {})
    by_type   = stats.get("by_type", {})

    if total == 0:
        return (
            "📊 *Feedback Statistik*\n\n"
            "_Ingen feedback modtaget endnu._\n\n"
            "Vent indtil testerne sender deres første rapporter."
        )

    lines = [
        "📊 *Feedback Statistik*",
        "═══════════════════════",
        "",
        f"📨 *Total modtaget:* {total}",
        "",
        "*Status-fordeling:*",
        f"  🆕 Ny: *{by_status.get('new', 0)}*",
        f"  👁 Set: *{by_status.get('seen', 0)}*",
        f"  💬 Besvaret: *{by_status.get('replied', 0)}*",
        f"  ✅ Løst: *{by_status.get('resolved', 0)}*",
        "",
        "*Type-fordeling:*",
        f"  💡 Idé: *{by_type.get('idea', 0)}*",
        f"  🐛 Bug: *{by_type.get('bug', 0)}*",
        f"  ❓ Spørgsmål: *{by_type.get('question', 0)}*",
        f"  🙏 Ros: *{by_type.get('praise', 0)}*",
    ]

    return "\n".join(lines)