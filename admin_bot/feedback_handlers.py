"""
admin_bot/feedback_handlers.py - Command handlers for the Buddy Admin bot.

CHANGES (v0.2.1 — Polish: configurable admin display name):
  - NY: ADMIN_DISPLAY_NAME konstant læses fra env-var (default 'admin').
    Sendes til format_user_received_reply() når admin svarer på feedback.
    Tidligere var "Jesper" hardcoded i bruger-beskeden — nu kan navnet
    konfigureres via Railway env-var uden code-deploy.

CHANGES (v0.2.0 — Batch B bulk actions + deep-link):
  - NY: Bulk-parsing i /seen og /resolve. Begge kommandoer accepterer nu
    enkelt ID, range (1-20), liste (5,7,9), eller kombineret (1,3-5,8).
    Eksempler:
      /seen 1-20      → Markér 20 records som set
      /resolve 5,7,9  → Markér 3 records som løst
      /resolve 1-3,8  → Kombineret (1,2,3,8)
  - NY: Deep-link support i /start. Hvis admin starter botten via et link
    der har 'start=reply_<id>' parameter (sendt fra Buddy main's inline-knap),
    åbnes hint om at bruge /reply <id> direkte.
  - REFACTOR: cmd_resolve og cmd_seen bruger nu admin_database.parse_id_range()
    + update_feedback_status_bulk() for bulk-operations.

CHANGES (v0.1.0 — initial):
  - cmd_start(): Velkomstbesked med kommando-oversigt.
  - cmd_help(): Detaljeret hjælp til alle kommandoer.
  - cmd_list(): Vis seneste feedback (med valgfri filter).
  - cmd_view(): Vis fuld detalje + screenshots for én feedback.
  - cmd_reply(): Send svar til testeren via Buddy main-bot.
  - cmd_resolve(): Marker som løst.
  - cmd_seen(): Marker som set (uden at svare).
  - cmd_stats(): Vis statistik (bonus).

DESIGN-PRINCIPPER:
  - Kun ADMIN_TELEGRAM_ID må bruge kommandoer (early-exit guard på alle).
  - Cross-bot kommunikation: vi bruger en separat Bot-instans med BUDDY_BOT_TOKEN
    til at sende svar til testere (de modtager svaret i deres normale Buddy-chat).
  - Error handling: alle DB-kald wrapped i try/except med tydelige fejl-beskeder.
  - Markdown V1 (samme som Buddy main).
"""

import logging
import os

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import admin_database as db
from admin_config import ADMIN_TELEGRAM_ID, BUDDY_BOT_TOKEN
from feedback_service import (
    format_feedback_detail,
    format_feedback_summary,
    format_stats,
    format_user_received_reply,
    get_feedback_type,
    list_feedback_type_ids,
    validate_feedback_type,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Admin-display navn (v0.2.1 — Polish: konfigurerbart navn)
# ══════════════════════════════════════════════════════════════════════════════

# Navnet der vises i bruger-beskeden "Svar fra <ADMIN_DISPLAY_NAME>".
# Sættes via env-var ADMIN_DISPLAY_NAME. Default 'admin' hvis ikke sat.
ADMIN_DISPLAY_NAME: str = (os.getenv("ADMIN_DISPLAY_NAME") or "admin").strip()


# ══════════════════════════════════════════════════════════════════════════════
# Helper — Buddy bot client til at sende svar til testere
# ══════════════════════════════════════════════════════════════════════════════

# Lazy-init: Vi bruger Buddys token til at sende svar til testere.
# Det sker via en separat Bot-instans (ikke vores admin-app).
# Telegram tillader at samme token bruges fra flere processes — så længe
# kun ÉN process polller for updates med tokenen (= main-buddy).
_buddy_bot: Bot | None = None


def _get_buddy_bot() -> Bot:
    """Hent (lazy-initialized) Buddy main-bot client til at sende svar."""
    global _buddy_bot
    if _buddy_bot is None:
        _buddy_bot = Bot(token=BUDDY_BOT_TOKEN)
    return _buddy_bot


# ══════════════════════════════════════════════════════════════════════════════
# Admin guard — kun ADMIN_TELEGRAM_ID må bruge kommandoer
# ══════════════════════════════════════════════════════════════════════════════

def _is_admin(update: Update) -> bool:
    """True hvis afsenderen er admin."""
    user = update.effective_user
    return user is not None and user.id == ADMIN_TELEGRAM_ID


async def _reject_non_admin(update: Update) -> None:
    """Send afvisning til ikke-admin brugere."""
    try:
        await update.message.reply_text(
            "🚫 Denne bot er kun for admin-brug.\n"
            "Hvis du vil bruge Buddy, skal du tale med Buddy main-bot."
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# /start og /help
# ══════════════════════════════════════════════════════════════════════════════

WELCOME_TEXT = (
    "🎬 *Buddy Admin Bot*\n"
    "═══════════════════════\n\n"
    "Velkommen tilbage, Jesper! 👋\n\n"
    "Denne bot lader dig administrere feedback fra Buddy-testere.\n\n"
    "*Hovedkommandoer:*\n"
    "  📋 `/list` — vis seneste feedback\n"
    "  🔍 `/view <id>` — fuld detalje\n"
    "  💬 `/reply <id> <besked>` — svar tester\n"
    "  ✅ `/resolve <id>` — markér som løst\n"
    "  👁 `/seen <id>` — markér som set\n"
    "  📊 `/stats` — statistik\n\n"
    "Skriv `/help` for fuld dokumentation."
)

HELP_TEXT = (
    "📖 *Buddy Admin Bot — Kommandoer*\n"
    "═══════════════════════════════\n\n"

    "📋 */list* — Vis 10 seneste aktive feedback\n"
    "  • `/list` — alle aktive (ny/set/besvaret)\n"
    "  • `/list new` — kun nye (uset)\n"
    "  • `/list resolved` — løste\n"
    "  • `/list bug` — kun bugs\n"
    "  • `/list idea` — kun idéer\n"
    "  • `/list 50` — vis 50 i stedet for 10\n"
    "  • Kombinér: `/list bug new 20`\n\n"

    "🔍 */view <id>* — Vis fuld detalje + screenshots\n"
    "  • Eksempel: `/view 42`\n"
    "  • Markerer automatisk som 'set'\n\n"

    "💬 */reply <id> <besked>* — Send svar til tester\n"
    "  • Eksempel: `/reply 42 Tak for rapporten — fix er på vej!`\n"
    "  • Testeren modtager beskeden via Buddy main-bot\n"
    "  • Markeres automatisk som 'replied'\n\n"

    "✅ */resolve <id|range|liste>* — Markér som løst (NU MED BULK!)\n"
    "  • `/resolve 42` — enkelt\n"
    "  • `/resolve 1-20` — range\n"
    "  • `/resolve 5,7,9` — liste\n"
    "  • `/resolve 1,3-5,8` — kombineret\n\n"

    "👁 */seen <id|range|liste>* — Markér som set (NU MED BULK!)\n"
    "  • Samme syntax som `/resolve`\n"
    "  • Eksempel: `/seen 1-20`\n\n"

    "📊 */stats* — Vis statistik\n"
    "  • Total optælling, fordeling pr. type og status\n\n"

    "❓ */help* — Vis denne hjælp"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Velkomst-besked når admin trykker /start.

    v0.2.0: Hvis bruger starter via deep-link (https://t.me/<bot>?start=reply_<id>),
    parser vi parameteret og giver et hint om at bruge /reply <id>.
    """
    if not _is_admin(update):
        await _reject_non_admin(update)
        return

    # v0.2.0: Tjek om der er en deep-link parameter
    args = context.args
    if args and args[0].startswith("reply_"):
        try:
            feedback_id = int(args[0].split("_", 1)[1])
            await update.message.reply_text(
                f"💬 *Klar til at svare på feedback \\#{feedback_id}*\n\n"
                f"Skriv din besked til testeren her:\n\n"
                f"`/reply {feedback_id} <din besked>`\n\n"
                f"_Eller brug `/view {feedback_id}` for at se feedback først._",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        except (ValueError, IndexError):
            pass

    # Normal velkomstbesked
    try:
        await update.message.reply_text(
            WELCOME_TEXT,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning("cmd_start Markdown-fejl: %s", e)
        plain = WELCOME_TEXT.replace("*", "").replace("`", "")
        await update.message.reply_text(plain)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vis fuld hjælp."""
    if not _is_admin(update):
        await _reject_non_admin(update)
        return

    try:
        await update.message.reply_text(
            HELP_TEXT,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning("cmd_help Markdown-fejl: %s", e)
        plain = HELP_TEXT.replace("*", "").replace("`", "")
        await update.message.reply_text(plain)


# ══════════════════════════════════════════════════════════════════════════════
# /list — Liste feedback med valgfri filtre
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Vis seneste feedback med valgfri filter.

    Brug:
      /list                       — 10 seneste aktive
      /list new                   — kun nye
      /list bug                   — kun bugs
      /list resolved              — løste
      /list bug new               — bugs der er nye
      /list bug new 25            — bugs, nye, 25 records
    """
    if not _is_admin(update):
        await _reject_non_admin(update)
        return

    args = context.args

    # Parse arguments
    status_filter: str | None = "active"  # default
    type_filter: str | None = None
    limit: int = 10

    for arg in args:
        arg_lower = arg.lower()
        if arg_lower in ("new", "seen", "replied", "resolved", "active", "all"):
            status_filter = None if arg_lower == "all" else arg_lower
        elif arg_lower in ("idea", "bug", "question", "praise"):
            type_filter = arg_lower
        elif arg.isdigit():
            limit = max(1, min(int(arg), 100))

    try:
        records = await db.list_feedback(
            status_filter=status_filter,
            type_filter=type_filter,
            limit=limit,
        )
    except Exception as e:
        logger.error("cmd_list DB-fejl: %s", e)
        await update.message.reply_text(f"❌ DB-fejl: {e}")
        return

    if not records:
        filter_desc = []
        if status_filter and status_filter != "active":
            filter_desc.append(f"status={status_filter}")
        if type_filter:
            filter_desc.append(f"type={type_filter}")
        filter_str = " ".join(filter_desc) if filter_desc else "aktive"

        await update.message.reply_text(
            f"📭 *Ingen feedback fundet* ({filter_str})\n\n"
            f"Brug `/list all` for at se alt — eller `/help` for filtrerings-muligheder.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Header
    filter_label_parts = []
    if type_filter:
        ft = get_feedback_type(type_filter)
        if ft:
            filter_label_parts.append(ft["label"])
    if status_filter == "new":
        filter_label_parts.append("🆕 nye")
    elif status_filter == "seen":
        filter_label_parts.append("👁 set")
    elif status_filter == "replied":
        filter_label_parts.append("💬 besvaret")
    elif status_filter == "resolved":
        filter_label_parts.append("✅ løst")
    elif status_filter == "active":
        filter_label_parts.append("aktive")
    elif status_filter is None:
        filter_label_parts.append("alle")

    header = (
        f"📋 *Feedback-liste* "
        f"\\({', '.join(filter_label_parts)}\\) — {len(records)} records\n"
        f"═══════════════════════\n"
    )

    summaries = [format_feedback_summary(r) for r in records]
    body = "\n\n".join(summaries)

    full_text = f"{header}\n{body}\n\n_Brug `/view <id>` for fuld detalje._"

    # Telegram begrænser beskeder til 4096 tegn — split hvis nødvendigt
    if len(full_text) <= 3800:
        try:
            await update.message.reply_text(
                full_text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning("cmd_list Markdown-fejl: %s — sender plain", e)
            plain = (
                full_text.replace("*", "")
                .replace("_", "")
                .replace("\\#", "#")
                .replace("\\(", "(")
                .replace("\\)", ")")
            )
            await update.message.reply_text(plain)
    else:
        # Split ved tom linje før 3800
        split_at = full_text.rfind("\n\n", 0, 3800)
        if split_at == -1:
            split_at = 3800
        try:
            await update.message.reply_text(full_text[:split_at], parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text(full_text[split_at:], parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning("cmd_list split Markdown-fejl: %s", e)
            await update.message.reply_text(full_text[:split_at])
            await update.message.reply_text(full_text[split_at:])

    logger.info(
        "cmd_list: returned %d records (status=%s, type=%s, limit=%d)",
        len(records), status_filter, type_filter, limit,
    )


# ══════════════════════════════════════════════════════════════════════════════
# /view — Fuld detalje for én feedback
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Vis fuld detalje for én feedback-record.

    Markerer automatisk som 'seen' hvis status='new'.
    Sender screenshots som separat besked hvis der er nogen.
    """
    if not _is_admin(update):
        await _reject_non_admin(update)
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "📖 Brug: `/view <id>`\n\nEksempel: `/view 42`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    feedback_id = int(args[0])

    try:
        record = await db.get_feedback(feedback_id)
    except Exception as e:
        logger.error("cmd_view DB-fejl: %s", e)
        await update.message.reply_text(f"❌ DB-fejl: {e}")
        return

    if not record:
        await update.message.reply_text(
            f"⚠️ Ingen feedback med ID `{feedback_id}` fundet.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Auto-markér som 'seen' hvis stadig 'new'
    if record["status"] == "new":
        try:
            await db.update_feedback_status(feedback_id, "seen")
            record["status"] = "seen"  # opdater lokal kopi til detalje-visning
        except Exception as e:
            logger.warning("cmd_view auto-seen fejl: %s", e)

    # Send detalje-besked
    detail_text = format_feedback_detail(record)
    try:
        await update.message.reply_text(
            detail_text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning("cmd_view Markdown-fejl: %s", e)
        plain = (
            detail_text.replace("*", "")
            .replace("_", "")
            .replace("`", "")
            .replace("\\#", "#")
            .replace("\\(", "(")
            .replace("\\)", ")")
            .replace("\\-", "-")
        )
        await update.message.reply_text(plain)

    # Send screenshots hvis nogen
    file_ids = record.get("screenshot_file_ids", []) or []
    if file_ids:
        await _send_feedback_screenshots(update, context, feedback_id, file_ids)


async def _send_feedback_screenshots(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    feedback_id: int,
    file_ids: list[str],
) -> None:
    """
    Send screenshots for en feedback til admin.

    NB: file_ids blev oprindeligt uploadet til Buddy main-bot, ikke admin-bot.
    Vi skal derfor bruge BUDDY_BOT_TOKEN til at hente filerne.

    Strategi: vi bruger admin-bot's send_photo med file_ids — det FUNGERER
    KUN hvis filen er blevet sendt til admin-botten før (file_id er bot-specifik).

    Backup-strategi: send via Buddy main-bot ved at chat_id sættes til ADMIN.
    Den ruter beskeden til admin-bottens chat hvis admin har samme telegram_id.
    """
    chat_id = update.effective_chat.id

    # Strategi: Brug Buddy-bot client til at sende fra dens kontekst
    # (file_ids er gyldige i Buddys bot-domæne).
    buddy_bot = _get_buddy_bot()

    try:
        if len(file_ids) == 1:
            await buddy_bot.send_photo(
                chat_id=chat_id,
                photo=file_ids[0],
                caption=f"📷 Screenshot fra feedback #{feedback_id}",
            )
        else:
            # Send som media group (max 10 per group)
            media_group = [
                InputMediaPhoto(media=fid)
                for fid in file_ids[:10]
            ]
            if media_group:
                media_group[0] = InputMediaPhoto(
                    media=file_ids[0],
                    caption=f"📷 {len(file_ids)} screenshots fra feedback #{feedback_id}",
                )
            await buddy_bot.send_media_group(
                chat_id=chat_id,
                media=media_group,
            )

            # Hvis flere end 10, send resten i ny gruppe
            if len(file_ids) > 10:
                extra = [InputMediaPhoto(media=fid) for fid in file_ids[10:20]]
                if extra:
                    await buddy_bot.send_media_group(
                        chat_id=chat_id,
                        media=extra,
                    )
    except Exception as e:
        logger.error("send_feedback_screenshots fejl: %s", e)
        try:
            await update.message.reply_text(
                f"⚠️ Kunne ikke sende screenshots: {e}\n"
                f"_(File-IDs hører til Buddy-botten — bruger Buddy-token til at sende)_"
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# /reply — Send svar til tester via Buddy main-bot
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Send admin-svar til testeren.

    Brug:
      /reply <id> <besked>

    Beskeden sendes via Buddy main-bot så testeren ser den i deres
    normale Buddy-chat — ikke fra admin-bot direkte.
    """
    if not _is_admin(update):
        await _reject_non_admin(update)
        return

    args = context.args
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "📖 *Brug:* `/reply <id> <besked>`\n\n"
            "Eksempel:\n"
            "`/reply 42 Tak for rapporten! Fix er deployed nu.`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    feedback_id = int(args[0])
    reply_text  = " ".join(args[1:]).strip()

    if not reply_text:
        await update.message.reply_text("⚠️ Beskeden må ikke være tom.")
        return

    if len(reply_text) > 3000:
        await update.message.reply_text(
            "⚠️ Beskeden er for lang (max 3000 tegn).\n"
            f"Din besked: {len(reply_text)} tegn."
        )
        return

    # Hent feedback
    try:
        record = await db.get_feedback(feedback_id)
    except Exception as e:
        logger.error("cmd_reply DB-fejl: %s", e)
        await update.message.reply_text(f"❌ DB-fejl: {e}")
        return

    if not record:
        await update.message.reply_text(
            f"⚠️ Ingen feedback med ID `{feedback_id}` fundet.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    target_telegram_id = record.get("telegram_id")
    if not target_telegram_id:
        await update.message.reply_text(
            f"❌ Feedback #{feedback_id} har ikke et telegram_id — kan ikke svare."
        )
        return

    # Byg bruger-besked via Buddy
    # v0.2.1: Send ADMIN_DISPLAY_NAME med så "Svar fra X" bruger den
    # konfigurerede værdi i stedet for hardcoded navn.
    user_message = format_user_received_reply(
        feedback_id        = feedback_id,
        feedback_type      = record.get("feedback_type", ""),
        original_message   = record.get("message", ""),
        admin_reply        = reply_text,
        admin_display_name = ADMIN_DISPLAY_NAME,
    )

    # Send via Buddy main-bot
    buddy_bot = _get_buddy_bot()

    # v0.2.0 fix: Tilføj '💬 Svar tilbage' inline-knap så bruger kan svare igen.
    # Knappen sender callback 'fb_reply_to:<id>' som Buddy main håndterer.
    reply_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "💬 Svar tilbage",
            callback_data=f"fb_reply_to:{feedback_id}",
        )
    ]])

    try:
        await buddy_bot.send_message(
            chat_id=target_telegram_id,
            text=user_message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_keyboard,
        )
    except Exception as e:
        # Fallback uden Markdown
        logger.warning("cmd_reply Markdown fejl: %s — prøver plain", e)
        try:
            plain = (
                user_message.replace("*", "")
                .replace("_", "")
                .replace("`", "")
                .replace("\\#", "#")
                .replace("\\(", "(")
                .replace("\\)", ")")
                .replace("\\-", "-")
            )
            await buddy_bot.send_message(
                chat_id=target_telegram_id,
                text=plain,
                reply_markup=reply_keyboard,
            )
        except Exception as e2:
            logger.error("cmd_reply send fejl: %s", e2)
            await update.message.reply_text(
                f"❌ Kunne ikke sende besked til bruger: `{e2}`\n\n"
                f"Telegram-ID: `{target_telegram_id}`\n"
                f"Status er IKKE opdateret — prøv igen.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    # Gem svar i DB (atomisk: opdaterer admin_reply + admin_replied_at + status='replied')
    try:
        await db.add_admin_reply(feedback_id, reply_text)
    except Exception as e:
        logger.error("cmd_reply DB-write fejl: %s", e)
        await update.message.reply_text(
            f"⚠️ Beskeden blev sendt, men status kunne ikke gemmes: `{e}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Bekræft til admin
    target_display = record.get("telegram_username") or record.get("telegram_name") or str(target_telegram_id)
    await update.message.reply_text(
        f"✅ *Svar sendt til {target_display}*\n"
        f"_(Feedback \\#{feedback_id} er nu markeret som besvaret)_",
        parse_mode=ParseMode.MARKDOWN,
    )

    logger.info(
        "cmd_reply: feedback_id=%d sent to telegram_id=%s (%d chars)",
        feedback_id, target_telegram_id, len(reply_text),
    )


# ══════════════════════════════════════════════════════════════════════════════
# /resolve — Markér som løst
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_resolve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Marker en eller flere feedback som 'resolved' uden at sende svar.

    v0.2.0: Understøtter nu bulk-operations:
      /resolve 5         → enkelt
      /resolve 1-20      → range
      /resolve 5,7,9     → liste
      /resolve 1,3-5,8   → kombineret
    """
    if not _is_admin(update):
        await _reject_non_admin(update)
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "📖 *Brug:* `/resolve <id|range|liste>`\n\n"
            "*Eksempler:*\n"
            "  `/resolve 42` — enkelt\n"
            "  `/resolve 1-20` — range\n"
            "  `/resolve 5,7,9` — liste\n"
            "  `/resolve 1,3-5,8` — kombineret",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Saml alle args (kan være "1-3, 5" eller "1,3,5" osv.)
    spec = " ".join(args).strip()

    feedback_ids = db.parse_id_range(spec)
    if not feedback_ids:
        await update.message.reply_text(
            f"⚠️ Kunne ikke parse '{spec}'.\n\n"
            f"Brug fx `/resolve 5` eller `/resolve 1-20`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Sikkerhedstjek: max 100 IDs ad gangen
    if len(feedback_ids) > 100:
        await update.message.reply_text(
            f"⚠️ For mange IDs ({len(feedback_ids)}). Max 100 per kommando.",
        )
        return

    try:
        if len(feedback_ids) == 1:
            updated = await db.update_feedback_status(feedback_ids[0], "resolved")
            count = 1 if updated else 0
        else:
            count = await db.update_feedback_status_bulk(feedback_ids, "resolved")
    except Exception as e:
        logger.error("cmd_resolve DB-fejl: %s", e)
        await update.message.reply_text(f"❌ DB-fejl: {e}")
        return

    if count == 0:
        await update.message.reply_text(
            f"⚠️ Ingen feedback fundet med de angivne IDs.",
        )
        return

    # Bekræft
    if len(feedback_ids) == 1:
        await update.message.reply_text(
            f"✅ Feedback \\#{feedback_ids[0]} markeret som *løst*.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        ids_preview = ", ".join(f"\\#{i}" for i in feedback_ids[:5])
        if len(feedback_ids) > 5:
            ids_preview += f" \\(+{len(feedback_ids) - 5} flere\\)"
        await update.message.reply_text(
            f"✅ *{count} feedback markeret som løst*\n"
            f"_(af {len(feedback_ids)} angivne IDs)_\n\n"
            f"IDs: {ids_preview}",
            parse_mode=ParseMode.MARKDOWN,
        )

    logger.info("cmd_resolve: %d/%d records marked resolved", count, len(feedback_ids))


async def cmd_seen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Marker en eller flere feedback som 'seen' uden at sende svar.

    v0.2.0: Understøtter nu bulk-operations (samme syntax som /resolve).
    """
    if not _is_admin(update):
        await _reject_non_admin(update)
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "📖 *Brug:* `/seen <id|range|liste>`\n\n"
            "*Eksempler:*\n"
            "  `/seen 42` — enkelt\n"
            "  `/seen 1-20` — range\n"
            "  `/seen 5,7,9` — liste\n"
            "  `/seen 1,3-5,8` — kombineret",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    spec = " ".join(args).strip()

    feedback_ids = db.parse_id_range(spec)
    if not feedback_ids:
        await update.message.reply_text(
            f"⚠️ Kunne ikke parse '{spec}'.\n\n"
            f"Brug fx `/seen 5` eller `/seen 1-20`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if len(feedback_ids) > 100:
        await update.message.reply_text(
            f"⚠️ For mange IDs ({len(feedback_ids)}). Max 100 per kommando.",
        )
        return

    try:
        if len(feedback_ids) == 1:
            updated = await db.update_feedback_status(feedback_ids[0], "seen")
            count = 1 if updated else 0
        else:
            count = await db.update_feedback_status_bulk(feedback_ids, "seen")
    except Exception as e:
        logger.error("cmd_seen DB-fejl: %s", e)
        await update.message.reply_text(f"❌ DB-fejl: {e}")
        return

    if count == 0:
        await update.message.reply_text(
            f"⚠️ Ingen feedback fundet med de angivne IDs.",
        )
        return

    if len(feedback_ids) == 1:
        await update.message.reply_text(
            f"👁 Feedback \\#{feedback_ids[0]} markeret som *set*.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        ids_preview = ", ".join(f"\\#{i}" for i in feedback_ids[:5])
        if len(feedback_ids) > 5:
            ids_preview += f" \\(+{len(feedback_ids) - 5} flere\\)"
        await update.message.reply_text(
            f"👁 *{count} feedback markeret som set*\n"
            f"_(af {len(feedback_ids)} angivne IDs)_\n\n"
            f"IDs: {ids_preview}",
            parse_mode=ParseMode.MARKDOWN,
        )

    logger.info("cmd_seen: %d/%d records marked seen", count, len(feedback_ids))


# ══════════════════════════════════════════════════════════════════════════════
# /stats — Statistik (bonus)
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vis feedback-statistik."""
    if not _is_admin(update):
        await _reject_non_admin(update)
        return

    try:
        stats = await db.count_feedback_by_status()
    except Exception as e:
        logger.error("cmd_stats DB-fejl: %s", e)
        await update.message.reply_text(f"❌ DB-fejl: {e}")
        return

    report = format_stats(stats)

    try:
        await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning("cmd_stats Markdown-fejl: %s", e)
        plain = report.replace("*", "").replace("_", "").replace("`", "")
        await update.message.reply_text(plain)


# ══════════════════════════════════════════════════════════════════════════════
# Global error handler
# ══════════════════════════════════════════════════════════════════════════════

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log uventede fejl."""
    import traceback
    logger.error(
        "Uventet fejl i admin-bot:\n%s",
        "".join(traceback.format_exception(
            type(context.error), context.error, context.error.__traceback__
        )),
    )

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"❌ Uventet fejl: `{context.error}`\n\n"
                f"Tjek Railway-loggen for detaljer.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass