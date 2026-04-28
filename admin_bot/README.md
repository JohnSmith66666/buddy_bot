# 🎬 Buddy Admin Bot

Admin-bot til at administrere feedback fra Buddy-testere.

## 📋 Oversigt

Denne bot er en **separat Telegram-bot** der kører ved siden af Buddy main.
Den deler PostgreSQL-database med Buddy main, men har sin egen Telegram-token
og Railway-service.

```
┌──────────────────┐         ┌──────────────────┐
│  Buddy main      │         │  Buddy Admin     │
│  (test_buddy /   │         │  (Buddy_admin)   │
│   Buddy_beta)    │         │                  │
└────────┬─────────┘         └────────┬─────────┘
         │                            │
         │  begge læser/skriver       │
         │  til SAMME MAIN database   │
         ▼                            ▼
┌──────────────────────────────────────────────┐
│  PostgreSQL (MAIN)                           │
│  - feedback ← delt mellem begge bots         │
│  - users, tmdb_metadata, etc.                │
└──────────────────────────────────────────────┘
```

## 🚀 Setup på Railway

### 1. Opret Telegram-bot via @BotFather

1. Åbn Telegram → tal med `@BotFather`
2. Send `/newbot`
3. Vælg navn: `Buddy Admin` (eller `Buddy_admin`)
4. Vælg username: fx `Buddy_admin_bot`
5. **Gem token'et** — du får brug for den om lidt

### 2. Opret ny Railway service

1. Gå ind i dit Railway-projekt (samme projekt som buddy-main)
2. Klik `+ New` → `GitHub Repo` → vælg samme repo som buddy-main
3. **Vigtigt:** I Service Settings → indstil **Root Directory** til `admin_bot`
4. Service-navn: `buddy-admin` (eller `buddy-admin-main`)

### 3. Sæt environment variables

I Railway service-settings → `Variables`, tilføj:

| Variable | Værdi |
|---|---|
| `ADMIN_BOT_TOKEN` | Token fra @BotFather (step 1) |
| `BUDDY_BOT_TOKEN` | Buddy MAIN-bottens token (samme som buddy-main service bruger) |
| `DATABASE_URL` | **Reference til Buddy MAIN's PostgreSQL** (se nedenfor) |
| `ADMIN_TELEGRAM_ID` | Din Telegram-ID (731397952) |
| `ENVIRONMENT` | `production` |

**For DATABASE_URL** brug Railway's variable-reference:
- Klik `Add Reference` → vælg din PostgreSQL-service → vælg `DATABASE_URL`
- Det sikrer at admin-bot peger på samme DB som buddy-main

### 4. Verificér deploy

Efter deploy, tjek loggen for:

```
admin_bot.admin_database | Connecting to PostgreSQL (admin-bot) …
admin_bot.admin_database | Database ready — feedback table found.
admin_bot.admin_main | Buddy Admin bot started in 'production' environment.
admin_bot.admin_main | Admin bot — starting polling …
```

Hvis du ser `feedback table doesn't exist`-fejl, så har Buddy main endnu
ikke kørt mod denne database. Sørg for at buddy-main er deployed FØRST.

### 5. Test admin-bot

1. Find din nye admin-bot i Telegram (søg på dens username)
2. Send `/start` → du skulle få velkomstbeskeden
3. Send `/help` → fuld dokumentation
4. Send `/list` → se eksisterende feedback (eller "Ingen feedback fundet")

## 📖 Kommandoer

| Kommando | Beskrivelse |
|---|---|
| `/start` | Velkomstbesked + kommando-oversigt |
| `/help` | Detaljeret hjælp |
| `/list` | Vis 10 seneste aktive feedback |
| `/list new` | Filtrer på nye |
| `/list bug 50` | Bugs, max 50 records |
| `/view 42` | Fuld detalje for feedback #42 (+ screenshots) |
| `/reply 42 <besked>` | Send svar til tester via Buddy main-bot |
| `/resolve 42` | Markér som løst |
| `/seen 42` | Markér som set (uden svar) |
| `/stats` | Statistik over alle feedback |

## 🔄 Workflow

1. Tester sender feedback via Buddy main → gemmes i DB
2. Admin får notifikation i deres Buddy-chat (sendt af Buddy main)
3. Admin åbner admin-bot → `/list` for at se overblik
4. Admin bruger `/view <id>` for fuld detalje + screenshots
5. Admin svarer via `/reply <id> <besked>` → testeren får svaret i deres
   normale Buddy-chat (admin-botten bruger Buddy-token til at sende)
6. Admin markerer som `/resolve <id>` når sagen er afsluttet

## 🛠️ Lokal udvikling

Opret `.env` i `admin_bot/`:

```bash
ADMIN_BOT_TOKEN=8123456789:AAAA-bbb-CCC-ddd
BUDDY_BOT_TOKEN=8779836559:AAAA-bbb-CCC-ddd
DATABASE_URL=postgresql://user:pass@host:5432/dbname
ADMIN_TELEGRAM_ID=731397952
ENVIRONMENT=dev
```

Kør:

```bash
cd admin_bot
pip install -r requirements.txt
python admin_main.py
```

## 🔐 Sikkerhed

- Kun `ADMIN_TELEGRAM_ID` kan bruge kommandoer — alle andre afvises
- Admin-bottens token er separat fra Buddy main — kompromittering af én
  påvirker ikke den anden
- Buddy-token bruges KUN til at sende svar til testere (ikke til at læse
  Buddy-chats)

## 🆘 Troubleshooting

**`feedback table doesn't exist`**
→ Buddy main er ikke deployed endnu, eller `DATABASE_URL` peger på forkert DB.
   Tjek at admin-bot's DATABASE_URL = buddy-main's DATABASE_URL.

**`Conflict: terminated by other getUpdates request`**
→ Du har 2 admin-bot processes kørende samtidigt. Stop én af dem.

**Admin-bot kan ikke sende screenshots**
→ Forventet — file_ids fra Buddy-bot virker kun hvis vi sender via
   BUDDY_BOT_TOKEN. Kode'n bruger automatisk Buddy-token til screenshots.

**`/reply` virker ikke**
→ Tjek at BUDDY_BOT_TOKEN er korrekt sat. Admin-botten bruger Buddy-token
   til at sende svar til testere.

## 📦 Dependencies

- `python-telegram-bot==21.6` (samme som Buddy main)
- `asyncpg==0.30.0` (samme som Buddy main)
- `python-dotenv==1.0.1` (samme som Buddy main)

Bevidst minimalistisk — ingen TMDB, Plex, Anthropic, etc. Admin-bot har
ikke brug for dem.