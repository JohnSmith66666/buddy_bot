"""
main.py - Buddy bot entry point.

CHANGES vs previous version (v0.9.8 — Annuller-knap photo-fix):
  - BUG FIX: handle_cancel_callback brugte query.edit_message_text() som
    fejler på photo-beskeder (infokort med poster). Resultat: brugeren
    fik "Hov, jeg fik vist popcorn galt i halsen!" ved tryk på ❌ Annuller.
    Fixet: Detekterer nu om beskeden er photo, og bruger
    edit_message_caption() i så fald. Robust fallback hvis edit fejler.
  - VERSION CHECK opdateret til v0.9.8-beta.

UNCHANGED (v0.9.7 — søgeresultater UX-fix):
  - Tilføjet handle_back_callback: håndterer ⬅️ Tilbage-knappen i søgeresultatlisten.
    Henter søgeterm og media_type fra pending_request og viser listen igen.
  - Registreret back:-handler i main() under bestillingsflow.

Tidligere ændringer (bevares):
  - v0.9.5: user_first_name sendes til get_ai_response.
  - v0.9.3: persona-rens, SHOW_INFO/TRAILER/SEARCH_RESULTS signal-arkitektur.
  - handle_watchlist_callback importeret fra confirmation_service.
  - escape_markdown for URL-underscores.
  - Webhook server på port 8080 med valgfri token-tjek.
"""

import asyncio
import logging
import re
import sys
import traceback
from aiohttp import web

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database
from admin_handlers import handle_approve_callback, notify_admin_new_user
from ai_handler import INFO_SIGNAL, SEARCH_SIGNAL, TRAILER_SIGNAL, check_session_timeout, clear_history, get_ai_response
from personas import get_persona
from services.confirmation_service import (
    execute_order,
    handle_watchlist_callback,
    show_confirmation,
    show_search_results,
)
from services.plex_service import validate_plex_user
from services.tmdb_service import get_media_details
from services.webhook_service import handle_radarr_webhook, handle_sonarr_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Matcher alle http(s)-URL'er — bruges til escape af underscores i tekst-beskeder
_URL_RE = re.compile(r"(https?://[^\s)\]>\"]+)")


def escape_markdown(text: str) -> str:
    """
    Escaper underscores inde i URL'er saa Telegrams Markdown-parser ikke
    misfortolker dem som kursiv-markoerer og kraesjer med 'Can't parse entities'.

    Kun tegn inde i URL'er beroeres - al anden Markdown-formatering
    (fed, kursiv, inline code osv.) i den omgivende tekst er uaendret.
    Backslash-escapingen sker via raw string replacement internt.
    """
    def _escape_url(match: re.Match) -> str:
        return match.group(1).replace("_", r"\_")

    return _URL_RE.sub(_escape_url, text)


# ── Guards ────────────────────────────────────────────────────────────────────

async def _guard(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    await database.upsert_user(user.id, user.username or user.first_name)
    if not await database.is_whitelisted(user.id):
        await notify_admin_new_user(update)
        return False
    return True


async def _needs_plex_setup(update: Update) -> bool:
    user = update.effective_user
    plex_username = await database.get_plex_username(user.id)
    if plex_username:
        return False
    onboarding_state = await database.get_onboarding_state(user.id)
    if onboarding_state == "awaiting_plex":
        return True
    await database.set_onboarding_state(user.id, "awaiting_plex")
    await update.message.reply_text(
        f"👋 Hej {user.first_name}!\n\n"
        "For at jeg kan give dig personlige svar, skal jeg kende dit "
        "Plex-brugernavn.\n\n"
        "Skriv det herunder - jeg tjekker det med det samme 🎬"
    )
    return True


# ── Plex onboarding ───────────────────────────────────────────────────────────

async def _handle_plex_input(update: Update, raw_input: str) -> None:
    user = update.effective_user
    await update.message.chat.send_action("typing")
    result = await validate_plex_user(raw_input.strip())
    if not result.get("valid"):
        await update.message.reply_text(
            f"❌ Jeg kan ikke finde *{raw_input}* på Plex-serveren.\n\n"
            "Tjek stavningen og prøv igen.",
            parse_mode="Markdown",
        )
        return
    verified = result["username"]
    await database.set_plex_username(user.id, verified)
    await update.message.reply_text(
        f"✅ Perfekt! Du er nu koblet til Plex som *{verified}*.\n\nHvad kan jeg hjælpe dig med? 🚀",
        parse_mode="Markdown",
    )
    logger.info("Onboarding complete — telegram_id=%s plex='%s'", user.id, verified)


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    clear_history(user.id)
    await database.log_message(user.id, "incoming", "/start")
    if await _needs_plex_setup(update):
        return
    reply = (
        f"👋 Hej {user.first_name}!\n\n"
        "Jeg er din personlige medie-assistent. Du kan bl.a. spørge mig om:\n"
        "• 🎬 Film og serier i dit Plex-bibliotek\n"
        "• ➕ Bestilling af ny film eller serie\n"
        "• 📺 Hvad der er på vej\n\n"
        "Hvad kan jeg hjælpe dig med?"
    )
    await update.message.reply_text(reply)
    await database.log_message(user.id, "outgoing", reply)


async def cmd_skift_plex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    user = update.effective_user
    await database.set_onboarding_state(user.id, "awaiting_plex")
    await database.log_message(user.id, "incoming", "/skift_plex")
    await update.message.reply_text(
        "Intet problem! 👌\nSkriv dit nye *Plex-brugernavn* herunder:",
        parse_mode="Markdown",
    )


# ── Inline Keyboard callbacks ─────────────────────────────────────────────────

async def handle_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bruger valgte et søgeresultat — vis Netflix-look infokort."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    token = query.data.split(":", 1)[1]
    plex_username = await database.get_plex_username(query.from_user.id)
    await show_confirmation(query, context, token, plex_username)


async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bruger bekræftede bestilling — send til Radarr/Sonarr."""
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    token = query.data.split(":", 1)[1]
    plex_username = await database.get_plex_username(query.from_user.id)
    await execute_order(query, token, plex_username)


async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bruger annullerede — ryd op.

    BUG FIX (v0.9.8): edit_message_text() fejler på photo-beskeder (infokort
    med poster). Vi detekterer derfor besked-typen først:
      - Photo (infokort) → edit_message_caption()
      - Tekst (søgeliste) → edit_message_text()
    Robust fallback hvis edit fejler: slet original og send ny besked.
    """
    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1]
    if token != "none":
        await database.get_pending_request(token)  # sletter fra DB

    cancel_text = "Bestillingen blev annulleret. 👍"
    is_photo = bool(getattr(query.message, "photo", None))

    try:
        if is_photo:
            await query.edit_message_caption(caption=cancel_text)
        else:
            await query.edit_message_text(cancel_text)
    except Exception as e:
        logger.warning("handle_cancel_callback edit fejl: %s — sender ny besked", e)
        try:
            await query.message.delete()
        except Exception:
            pass
        try:
            await query.message.chat.send_message(cancel_text)
        except Exception as e2:
            logger.error("handle_cancel_callback fallback fejl: %s", e2)


async def handle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bruger trykkede ⬅️ Tilbage i søgeresultatlisten.
    Henter søgeterm og media_type fra pending_request og viser listen igen.
    title-feltet genbruges til at gemme søgetermen.
    """
    query = update.callback_query
    await query.answer()

    if not await _guard(update):
        return

    token = query.data.split(":", 1)[1]
    pending = await database.get_pending_request(token)
    if not pending:
        await query.edit_message_text("Sessionen er udløbet — start forfra.")
        return

    search_query = pending["title"]       # title-feltet genbruges som søgeterm
    media_type   = pending["media_type"]

    # Slet den eksisterende besked og vis søgelisten på ny
    try:
        await query.message.delete()
    except Exception:
        pass

    await show_search_results(query.message, search_query, media_type)


# ── /info_movie_<id> og /info_tv_<id> handler ────────────────────────────────

async def handle_info_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fanger /info_movie_<tmdb_id> og /info_tv_<tmdb_id> kommandoer.
    Regex er fleksibel: underscores er valgfrie for at fange Buddys fejlskrivninger.
    Logger altid den fulde kommando for at debugge ID-parring fejl.
    """
    if not await _guard(update):
        return

    logger.info("HANDLER MODTOG: %s", update.message.text)

    if context.matches:
        match = context.matches[0]
    else:
        text  = (update.message.text or "").strip()
        match = re.match(r"^/info_?(movie|tv)_?(\d+)$", text)
        if not match:
            logger.warning("HANDLER: ingen match på '%s' — ignorerer", update.message.text)
            return

    media_type    = match.group(1)
    tmdb_id       = int(match.group(2))
    logger.info("Bruger trykkede på info-link: type=%s, id=%s", media_type, tmdb_id)

    user_id       = update.effective_user.id
    plex_username = await database.get_plex_username(user_id)

    await update.message.chat.send_action("typing")

    loading_msg = await update.message.reply_text(
        "🤖 Beregner svar med lynets hast... næsten...",
        reply_markup=ReplyKeyboardRemove(),
    )

    details = await get_media_details(tmdb_id, media_type)
    if not details:
        await loading_msg.delete()
        await update.message.reply_text("Kunne ikke hente info — prøv igen.")
        return

    title = details.get("title") or "Ukendt"
    year  = details.get("release_date", details.get("first_air_date", ""))[:4]

    import secrets as _sec
    token = _sec.token_hex(8)
    await database.save_pending_request(token, user_id, {
        "media_type": media_type,
        "tmdb_id":    tmdb_id,
        "title":      title,
        "year":       int(year) if year else None,
        "step":       "picked",
    })

    # Slet KUN brugerens kommando-besked — loading-beskeden lever til infokort er sendt
    try:
        await update.message.delete()
    except Exception:
        pass

    await show_confirmation(update.message, context, token, plex_username,
                            loading_msg=loading_msg)


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    user = update.effective_user
    text = (update.message.text or "").strip()
    await database.log_message(user.id, "incoming", text)

    onboarding_state = await database.get_onboarding_state(user.id)
    if onboarding_state == "awaiting_plex":
        await _handle_plex_input(update, text)
        return

    if await _needs_plex_setup(update):
        return

    # ── Session timeout: kør /start automatisk efter 10 min inaktivitet ──────
    if check_session_timeout(user.id):
        await cmd_start(update, context)
        return

    await update.message.chat.send_action("typing")
    plex_username = await database.get_plex_username(user.id)
    persona_id    = await database.get_persona(user.id)

    # Send loading-besked og slet den når svaret er klar
    loading_msg = await update.message.reply_text(
        "🤖 Beregner svar med lynets hast... næsten...",
        reply_markup=ReplyKeyboardRemove(),
    )

    reply = await get_ai_response(
        telegram_id=user.id,
        user_message=text,
        plex_username=plex_username,
        persona_id=persona_id,
        user_first_name=user.first_name,
    )

    # Slet loading-beskeden
    try:
        await loading_msg.delete()
    except Exception:
        pass

    # Rens backticks fra signaler — Buddy pakker dem nogle gange ind i Markdown
    # Eksempel: `SHOW_INFO:157336:movie` → SHOW_INFO:157336:movie
    # Det originale reply bruges stadig til normalt svar (bevarer Markdown)
    clean_reply = reply.replace("`", "").strip()

    def _find_signal(signal: str) -> str | None:
        """
        Scan alle linjer i clean_reply for signalet og returner linjen.
        Buddy placerer nogle gange signalet på linje 2 efter en tekstlinje
        — startswith() på hele svaret fanger ikke dette. Vi scanner linje
        for linje og returnerer den første linje der starter med signalet.
        """
        for line in clean_reply.splitlines():
            line = line.strip()
            if line.startswith(signal):
                return line
        return None

    # ── Signal: bestillingsflow ───────────────────────────────────────────────
    signal_line = _find_signal(SEARCH_SIGNAL)
    if signal_line:
        parts = signal_line[len(SEARCH_SIGNAL):].split(":", 1)
        query_term = parts[0].strip()
        media_type = parts[1].strip() if len(parts) > 1 else "both"
        await show_search_results(update.message, query_term, media_type)
        return

    # ── Signal: Netflix-look infokort ─────────────────────────────────────────
    # Format: SHOW_INFO:<tmdb_id>:<media_type>
    signal_line = _find_signal(INFO_SIGNAL)
    if signal_line:
        payload = signal_line[len(INFO_SIGNAL):].strip()
        parts   = payload.split(":")
        if len(parts) >= 2:
            tmdb_id_str = parts[0].strip()
            media_type  = parts[1].strip()
            try:
                from services.confirmation_service import _make_token
                token = _make_token()
                await database.save_pending_request(token, user.id, {
                    "media_type": media_type,
                    "tmdb_id":    int(tmdb_id_str),
                    "title":      "Slår op...",
                    "step":       "picked",
                })
                await show_confirmation(update.message, context, token,
                                        plex_username)
                return
            except Exception as e:
                logger.error("Fejl ved håndtering af SHOW_INFO: %s", e)
        else:
            logger.warning("SHOW_INFO signal kunne ikke parses: %r", reply)

    # ── Signal: trailer-knap ──────────────────────────────────────────────────
    # Format: SHOW_TRAILER:<beskedtekst>|<trailer_url>
    signal_line = _find_signal(TRAILER_SIGNAL)
    if signal_line:
        payload  = signal_line[len(TRAILER_SIGNAL):]
        # Del ved det SIDSTE pipe-tegn for at beskytte mod pipe i beskeden
        pipe_idx = payload.rfind("|")
        if pipe_idx != -1:
            besked_tekst = payload[:pipe_idx].strip()
            trailer_url  = payload[pipe_idx + 1:].strip()
            safe_reply = escape_markdown(besked_tekst)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Se Trailer", url=trailer_url)
            ]])
            await update.message.reply_text(
                safe_reply,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            logger.info("Trailer-knap sendt: %s", trailer_url)
            await database.log_message(user.id, "outgoing", besked_tekst)
            return

    # ── Normalt svar — brug det originale reply med Markdown intakt ───────────
    safe_reply = escape_markdown(reply)
    await update.message.reply_text(safe_reply, parse_mode="Markdown")
    await database.log_message(user.id, "outgoing", reply)


# ── Webhook HTTP server ───────────────────────────────────────────────────────

async def _webhook_radarr(request: web.Request) -> web.Response:
    # ── Secret token check ────────────────────────────────────────────────────
    if config.WEBHOOK_SECRET:
        token = request.rel_url.query.get("token", "")
        if token != config.WEBHOOK_SECRET:
            logger.warning("Radarr webhook: uautoriseret request fra %s", request.remote)
            return web.Response(status=401, text="Unauthorized")
    try:
        payload = await request.json()
        logger.info("Radarr webhook received: eventType=%s", payload.get("eventType"))
        asyncio.create_task(handle_radarr_webhook(payload))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error("Radarr webhook error: %s", e)
        return web.Response(status=400, text=str(e))


async def _webhook_sonarr(request: web.Request) -> web.Response:
    # ── Secret token check ────────────────────────────────────────────────────
    if config.WEBHOOK_SECRET:
        token = request.rel_url.query.get("token", "")
        if token != config.WEBHOOK_SECRET:
            logger.warning("Sonarr webhook: uautoriseret request fra %s", request.remote)
            return web.Response(status=401, text="Unauthorized")
    try:
        payload = await request.json()
        logger.info("Sonarr webhook received: eventType=%s", payload.get("eventType"))
        asyncio.create_task(handle_sonarr_webhook(payload))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error("Sonarr webhook error: %s", e)
        return web.Response(status=400, text=str(e))


async def _start_webhook_server() -> None:
    app = web.Application()
    app.router.add_post("/webhook/radarr", _webhook_radarr)
    app.router.add_post("/webhook/sonarr", _webhook_sonarr)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Webhook server started on port 8080")


# ── Global error handler ──────────────────────────────────────────────────────

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all handler for uventede Telegram-fejl.
    Logger fuld traceback og sender en venlig besked til brugeren hvis muligt.
    """
    logger.error(
        "Uventet fejl ved håndtering af update:\n%s",
        "".join(traceback.format_exception(
            type(context.error), context.error, context.error.__traceback__
        )),
    )

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Hov, jeg fik vist popcorn galt i halsen! 🍿\n"
                "Noget gik uventet galt i maskinrummet. Prøv igen om lidt."
            )
        except Exception as e:
            logger.error("Kunne ikke sende fejlbesked til bruger: %s", e)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def on_startup(application: Application) -> None:
    await database.setup_db()
    await database.setup_pending_requests()
    await _start_webhook_server()
    if not config.WEBHOOK_SECRET:
        logger.warning(
            "WEBHOOK_SECRET er ikke sat — webhooks accepteres uden token-tjek! "
            "Sæt WEBHOOK_SECRET i Railway for at sikre endpointene."
        )
    logger.info("Buddy started in '%s' environment.", config.ENVIRONMENT)
    logger.info(
        "VERSION CHECK — v0.9.8-beta | "
        "søgeresultater-UX: JA | foto-fix: JA | årstal-fallback: JA | "
        "tilbage-knap: JA | already-anmodet-check: JA | user_first_name: JA | "
        "annuller-photo-fix: JA"
    )


async def on_shutdown(application: Application) -> None:
    await database.close_db()
    logger.info("Buddy shut down cleanly.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("skift_plex", cmd_skift_plex))

    # Admin approval
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern=r"^approve_user:\d+$"))

    # Bestillingsflow
    app.add_handler(CallbackQueryHandler(handle_pick_callback,      pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(handle_confirm_callback,   pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_callback,    pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(handle_watchlist_callback, pattern=r"^watchlist:"))
    app.add_handler(CallbackQueryHandler(handle_back_callback,      pattern=r"^back:"))

    # Info-links fra lister — fleksibelt regex fanger både /info_movie_123 og /infomovie123
    app.add_handler(MessageHandler(
        (filters.COMMAND | filters.TEXT) & filters.Regex(r"^/info_?(movie|tv)_?(\d+)$"),
        handle_info_link,
    ))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(handle_error)

    logger.info("Starting polling …")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()