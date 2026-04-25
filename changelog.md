# Buddy Bot — Changelog

Versionering følger [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH-pre`.
- **MAJOR** bump ved breaking changes eller production-release (1.0.0)
- **MINOR** bump ved nye features
- **PATCH** bump ved bugfixes
- **-beta** indtil production-release med testbrugere

---

## [0.9.3-beta] — 2026-04-25

Stor pakke med tre uafhængige forbedringer: cache-optimering, dataintegritet-fix og persona-rens.

### Fjernet
- **Onkel Flemming-persona**: Fjernet helt. Buddy er nu den eneste persona — klar, kortfattet, professionel. Den lange snaksomme tone med catchphrases ("*hic*", "skål", "min dreng") gjorde svar 2-3× længere uden at tilføje værdi.
- **`/persona`-kommando**: Fjernet fra Telegram-menuen og fra `main.py` (`cmd_persona`, `persona_callback`). Persona-skift-flowet er ikke længere relevant med kun én persona.
- **`onkel_flemming.png`**: Filen kan slettes fra repo'et (brugeren bør gøre det manuelt — main.py refererer den ikke længere).
- `image_path`-feltet i `personas.py` er fjernet (Buddy har intet profilbillede).

### Tilføjet
- **Brugerens fornavn injectes i system-prompten**: `get_system_prompt()` tager nu et `user_first_name`-argument. Buddy tiltaler dermed brugeren direkte ved fornavn ("Klaret, Jesper!") uden at gætte. Hentes fra `update.effective_user.first_name` i `main.py` — ingen ekstra DB-opslag.
- **Ny `## SVARLÆNGDE — DISCIPLIN`-sektion i system-prompten**: Forbyder eksplicit indledningsfraser ("Selvfølgelig!", "Lad mig tjekke...", "Et øjeblik..."), selvkommentarer ("Jeg har slået op..."), og afsked-fraser ("Skål!", "God fornøjelse!"). Korte spørgsmål → korte svar (1-3 sætninger). Detaljer kun på forespørgsel.

### Fikset (kritisk dataintegritet — fra planlagt 0.9.2)
- **Ingen flere hallucinerede TMDB-ID'er i lister**: Buddy/Onkel Flemming gættede tidligere ID'er fra træningsdata når de skulle nævne manglende eller kommende film (f.eks. ved "hvad mangler vi af Marvel?"). Det resulterede i `/info_movie_<id>`-links der pegede på helt forkerte film. Ny regel #7 i `## REGLER FOR LISTER` forbyder eksplicit dette: alle ID'er i links SKAL stamme fra et tool-resultat i den aktuelle samtale. Hvis ID'et ikke er verificeret, udelades linket helt — eller `search_media` kaldes først.

### Ændret (cache-optimering — fra planlagt 0.9.2)
- **Cache-vending i `prompts.py`**: Persona-prompten indsættes nu i BUNDEN af system-prompten i stedet for toppen. Tidligere invaliderede et persona-skift hele cachen for de ~4000 tokens regler nedenunder. Nu genbruges body-cachen, og kun den lille persona-blok skal skrives.
- **Slankere dynamisk blok i `ai_handler.py`**: Den lange forklaring om dato-sammenligning er fjernet fra `dynamic_lines`. Reglen er allerede i `_SYSTEM_PROMPT_BODY` under "## Absolut tillid til værktøjer" og caches dér. Sparer ~120 tokens per request.
- `get_system_prompt()` returnerer nu `body + persona_prompt` i stedet for `persona_prompt + body`.
- VERSION CHECK log opdateret med `cache-optimeret: JA | id-hallucination-fix: JA | persona-rens: JA`-flags.

### Forventet effekt
- **Cache read ratio**: 39 % → forventet 50–60 % (cache-vending + slank dynamisk blok)
- **Output-tokens**: ~30-40 % færre per svar (svarlængde-disciplin + ingen Onkel Flemming-skåltale)
- **Svartid**: ~2-3 sek hurtigere per svar (færre output-tokens at generere)
- **Brugeroplevelse**: Personlige svar med fornavn, ingen forvirring fra hallucinerede links, hurtigere og mere professionelle svar

### Kendt begrænsning (ikke fikset i denne version)
- `check_franchise_status` rammer kun TMDB-collections der matcher søgeordet i navnet. Mega-franchises som MCU består af ~10 separate collections (Avengers, Spider-Man, Iron Man, Thor, Captain America osv.) plus stand-alone-film. Ved søgning på "Marvel" returneres kun de samlinger der har "Marvel" i navnet — Iron Man, Thor osv. ekskluderes. Permanent fix planlagt til 0.10.0.

### Migrations-noter
- Brugere der tidligere havde valgt Onkel Flemming i databasen vil automatisk falde tilbage til Buddy (database `persona_id`-kolonnen rummer stadig "flemming", men `get_persona()` falder tilbage til "buddy" når personaen ikke længere findes). Ingen DB-migration nødvendig.
- BotFather-kommandoliste bør opdateres manuelt: fjern `/persona` fra menuen.

---

## [0.9.1-beta] — 2026-04-25

### Fikset
- `_slim()` returnerer nu `tmdb_id` fra Plex GUID — alle `find_unwatched`/`get_collection`-lister har nu korrekte links
- Loading-besked slettes nu først når infokort er sendt (ikke halvvejs i processen)
- `get_recently_added`: serier bruger altid TMDB TV-søgning på `series_name` for korrekt serie-ID
- `watch.plex.tv` søge-URL bruger nu slug-opslag via Plex metadata API for direkte deep-link til film/serie

### Ændret
- VERSION CHECK log inkluderer nu versionsnummer

---

## [0.9.0-beta] — 2026-04-25

### Tilføjet
- **Persona-system**: `/persona`-kommando med inline keyboard — brugere kan skifte assistent-persona
- **Onkel Flemming**: første alternative persona — den lidt for fulde onkel med dårlige filmanbefalinger 🍺
- `personas.py`: ny fil med persona-definitioner (id, navn, emoji, beskrivelse, prompt, image_path)
- `get_system_prompt(persona_id)` i `prompts.py` — system-prompt er nu dynamisk per bruger
- `database.get_persona()` / `database.set_persona()` — persona gemmes i PostgreSQL
- Persona-billede sendes som velkomst ved skift (Onkel Flemming har eget portræt)
- Session timeout: `/start` køres automatisk efter 10 min inaktivitet
- Loading-besked (`🤖 Beregner svar med lynets hast... næsten...`) vises ved alle svar
- `ReplyKeyboardRemove()` lukker tastaturet automatisk ved svar
- BotFather kommandoliste: `/start`, `/persona`, `/skift_plex`

### Fikset
- `SYSTEM_PROMPT` er nu bagudkompatibel konstant + `get_system_prompt()` funktion

---

## [0.8.0-beta] — 2026-04-25

### Tilføjet
- **Nyligt tilføjet**: `get_recently_added` viser nu film med 🟢 `/info_movie_` links og serier med 🔵 `/info_tv_` links
- TMDB fallback-opslag for film og serier der mangler `tmdb_id` fra Tautulli (batch parallelt i `ai_handler.py`)
- Engelsk oversættelse af resumé via Claude Haiku når TMDB ikke har dansk tekst
- Engelsk titel-fallback for ikke-latinske sprog (koreansk, japansk, kinesisk osv.)
- Infokort Design B: skillelinjer `━━━━━━━━━━━━━━━━`, kursiv resumé, `⭐️`/`🎭`/`👥` ikoner

### Ændret
- Infokort: genre-separator ændret fra `,` til `·`
- Infokort: score og varighed vises på én linje adskilt af `·`
- Liste-ikoner: `✅` → `🟢` (film), `📡` → `🔵` (serier) i nyligt tilføjet

---

## [0.7.0-beta] — 2026-04-25

### Tilføjet
- **Plex deep-link**: `watch.plex.tv/movie/{slug}` via Plex metadata API slug-opslag
- `get_plex_watch_url(tmdb_id, media_type)` i `plex_service.py`
- `Accept: application/json` header til Plex metadata API
- Fallback til `watch.plex.tv/search?q={titel}` hvis slug-opslag fejler

### Fikset
- `_MAX_TOOL_RESULT_CHARS` hævet fra 2000 → 6000 — alle 29 Clooney-film når frem til Buddy
- `_slim_data` max_list_items hævet fra 10 → 40
- `missing_top_movies` fjernet fra `check_actor_on_plex` return — var upålidelig og hallucineringskilde

---

## [0.6.0-beta] — 2026-04-25

### Tilføjet
- **Franchise/samling**: `get_tmdb_collection_movies` henter nu ALLE matchende TMDB collections og merger dem (dansk + norsk Olsenbanden finder alle film)
- `_FRANCHISE_MAX_PER_LIST` hævet til 40
- Skuespiller-lister: emoji-kategorier `🎬 *Kategorinavn*` med fed tekst og luft

### Ændret
- Prompt: franchise viser alle `found_on_plex`, nævner ikke manglende automatisk
- Prompt: skuespiller viser alle `found_on_plex`, nævner ikke manglende automatisk
- Prompt: `check_plex_library` sender nu `tmdb_id` med for GUID-matching (Lag 0)

---

## [0.5.0-beta] — 2026-04-24

### Tilføjet
- **GUID-matching Lag 0**: `_check_sync` scanner hele biblioteket via TMDB GUID — finder "Boundless" når man søger "Den grænseløse"
- `check_actor_on_plex`: IMDb GUID som Lag 1b fallback
- `check_franchise_on_plex`: GUID-matching som primær metode
- `get_plex_metadata`: henter `machineIdentifier` og `ratingKey` til Plex deep-links
- Watchlist-funktion: `add_to_watchlist` via PlexAPI

### Fikset
- Plex deep-link URL-format: `%2Flibrary%2Fmetadata%2F` encoding

---

## [0.4.0-beta] — 2026-04-24

### Tilføjet
- **Netflix-look infokort**: `send_photo` med plakat, caption og inline keyboard
- `confirmation_service.py`: komplet bestillingsflow med infokort
- TMDB trailer-opslag med da-DK → en-US fallback
- IMDb rating fra Plex, TMDB rating som fallback
- `SHOW_INFO` signal-arkitektur: Buddy returnerer `SHOW_INFO:<id>:<type>` i stedet for tekst
- `/info_movie_<id>` og `/info_tv_<id>` link-handlers i `main.py`
- Plex-tjek integreret i infokort-visning

### Ændret
- `TRAILER_SIGNAL`, `SEARCH_SIGNAL`, `INFO_SIGNAL` — komplet signal-arkitektur

---

## [0.3.0-beta] — 2026-04-23

### Tilføjet
- Tautulli-integration: `get_recently_added`, `get_user_watch_stats`, `get_user_history`, `get_popular_on_plex`
- TMDB-integration: `search_media`, `get_media_details`, `get_trending`, `get_recommendations`
- Radarr/Sonarr bestillingsflow med webhook-support
- Anthropic Prompt Caching på system-prompt og tools
- `_slim_data()` til token-optimering af store JSON-payloads
- PostgreSQL via Railway: whitelist, onboarding-state, interaktionshistorik

---

## [0.2.0-beta] — 2026-04-22

### Tilføjet
- PlexAPI-integration: `check_library`, `find_unwatched`, `get_collection`, `search_by_actor`
- Fuzzy titel-matching (Lag 1-3)
- Admin-godkendelsesflow med Telegram inline keyboard
- Onboarding-flow: Plex-brugernavn validering

---

## [0.1.0-beta] — 2026-04-21

### Tilføjet
- Grundlæggende Telegram bot-arkitektur
- Claude API integration med tool use / function calling
- Samtalehistorik per bruger
- Whitelist-system
- Railway deployment (dev + main miljøer)