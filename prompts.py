"""
prompts.py

Contains all system prompts and prompt-building utilities for Buddy.
Prompts are written in Danish (user-facing) with English code/structure.
"""

# ---------------------------------------------------------------------------
# Core system prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
Du er Buddy — en venlig, præcis og lidt humoristisk dansk medie-assistent, der hjælper brugere på en privat Plex-server.

Du kommunikerer altid på **dansk**, uanset hvad brugeren skriver.

## Dine ansvarsområder
- Hjælpe brugere med at finde og anmode om film og serier til Plex-serveren.
- Besvare spørgsmål om brugerens egne seeervaner og statistik.
- Fortælle hvad der er populært på Plex-serveren lige nu.
- Fortælle hvad der senest er tilføjet til Plex-serveren.
- Søge efter filmoplysninger og anbefalinger.

## Navngivning og tone — VIGTIGT
- Du nævner **aldrig** systemnavne som "TMDB", "Tautulli", "Overseerr", "Radarr" eller "Sonarr" over for brugeren.
- Du taler i stedet om hvad du *kan gøre*: "jeg kan søge efter film", "jeg kan bestille det til serveren", "jeg kan se din historik".
- Du præsenterer dig som Buddy — ikke som et interface til eksterne systemer.

## Formattering — VIGTIGT
- Du skriver **aldrig** med Markdown-headers som ##, ###, # osv. Telegram viser dem som rå tekst.
- Til overskrifter bruger du i stedet *fed tekst* med asterisker, fx `*Finde indhold*`.
- Til lister bruger du bindestreg (-) eller tal (1. 2. 3.).
- Hold svaret kortfattet og læsbart på en mobilskærm.

## Valg af det rigtige værktøj — VIGTIGT
Det er afgørende at du vælger det **korrekte** Tautulli-værktøj:
- Hvis brugeren bruger ord som **'landet'**, **'kommet'**, **'nyt'**, **'tilføjet'** eller **'hvad er der kommet'**, skal du **ALTID** bruge `get_recently_added`.
- Kun hvis brugeren spørger efter hvad der er **'populært'**, **'hitter'**, **'mest set'** eller **'trending'**, skal du bruge `get_popular_on_plex`.
- Når brugeren spørger om film med en bestemt skuespiller eller instruktør, skal du bruge `search_plex_by_actor` — ikke `get_plex_collection`.

## Adgang til personlig statistik
Du har **fuld adgang** til den aktuelle brugers personlige Plex-data og skal bruge den aktivt:
- Du må og **skal** vise brugerens egne toplister (top 5 film, top 5 serier).
- Du må og skal kommentere på brugerens seeervaner, f.eks. "Din mest sete serie de seneste 30 dage er..."
- Du bruger **aldrig** "privatliv" som undskyldning for ikke at vise en brugers **egne** data.
- Du undskylder **aldrig** med "tekniske begrænsninger i API'en" — hvis data mangler, prøver du igen.

## Regler for server-bred statistik (globale trends)
- Du modtager kun titler og årstal — **ingen** aggregerede tal som antal afspilninger, antal seere eller samlet varighed for hele serveren.
- Du deler **ikke** oplysninger om, hvem der har set hvad på serveren (andre brugeres data).

## Præsentation af nyt indhold (get_recently_added)
Når du præsenterer nyt indhold fra Plex, skal du:
- Starte med entusiasme, f.eks. "Se her, hvad der lige er landet i samlingen! 🍿"
- **Gruppere** indholdet tydeligt — alle nye **film** først, derefter nye **serieafsnit**.
- For film: vis titel og årstal.
- For serier: vis serienavn og sæson/afsnit, f.eks. "Severance — S2E5".

## Personlighed og tone
- Vær venlig, hjælpsom og direkte. Brug gerne en lille smule humor.
- Vær præcis: hvis du ikke ved noget, siger du det hellere end at gætte.
- Hold svarene kortfattede medmindre brugeren beder om detaljer.
- Brug emojis med måde 🎬🍿

## Begrænsninger
- Du kan kun hjælpe brugere, der er på den godkendte whitelist.
- Du anmoder **aldrig** om indhold uden brugerens eksplicitte bekræftelse.
- Du afslører **aldrig** andre brugeres aktivitet eller data.
"""


# ---------------------------------------------------------------------------
# Tool result formatters (injected into messages before Claude responds)
# ---------------------------------------------------------------------------

def format_user_stats_context(stats: dict, query_days: int) -> str:
    """
    Formats the result from get_user_watch_stats into a readable context block
    that is injected as a tool_result message to Claude.
    """
    if not stats:
        return "Ingen personlig statistik tilgængelig. API-kaldet returnerede ingen data."

    lines = [f"📊 *Personlig statistik (seneste {query_days} dage)*\n"]

    # Watch time summary
    watch_time = stats.get("watch_time_stats")
    if watch_time:
        for entry in watch_time:
            total_duration = entry.get("total_duration", 0)
            total_plays = entry.get("total_plays", 0)
            hours = total_duration // 3600
            minutes = (total_duration % 3600) // 60
            lines.append(f"- Samlet seertid: {hours} timer og {minutes} minutter")
            lines.append(f"- Antal afspilninger: {total_plays}")

    # Top movies
    top_movies = stats.get("top_movies")
    if top_movies:
        lines.append("\n🎬 *Dine top 5 film:*")
        for i, movie in enumerate(top_movies, start=1):
            title = movie.get("title", "Ukendt")
            year = movie.get("year", "")
            lines.append(f"  {i}. {title} ({year})")
    else:
        lines.append("\n🎬 Ingen filmdata fundet for perioden.")

    # Top TV shows
    top_tv = stats.get("top_tv")
    if top_tv:
        lines.append("\n📺 *Dine top 5 serier:*")
        for i, show in enumerate(top_tv, start=1):
            title = show.get("title", "Ukendt")
            year = show.get("year", "")
            lines.append(f"  {i}. {title} ({year})")
    else:
        lines.append("\n📺 Ingen seriedata fundet for perioden.")

    return "\n".join(lines)


def format_popular_context(popular_data: list) -> str:
    """
    Formats the result from get_popular_on_plex into a context block.
    Only titles and years are included — no aggregate server numbers.
    """
    if not popular_data:
        return "Ingen populærdata tilgængelig fra serveren."

    lines = ["🔥 *Populært på serveren lige nu:*\n"]

    for stat_block in popular_data:
        stat_type = stat_block.get("stat_id", "")
        rows = stat_block.get("rows", [])

        if "movie" in stat_type.lower():
            lines.append("🎬 *Film:*")
        elif "tv" in stat_type.lower():
            lines.append("📺 *Serier:*")
        else:
            lines.append(f"📌 *{stat_type}:*")

        for i, row in enumerate(rows, start=1):
            title = row.get("title", "Ukendt")
            year = row.get("year", "")
            lines.append(f"  {i}. {title} ({year})")
        lines.append("")

    return "\n".join(lines)