"""
PATCH til services/tmdb_service.py — v1.0.4 (filmografi token-fix)

PROBLEM:
  get_person_filmography() returnerede 7 felter per film:
    tmdb_id, title, original_title, release_date,
    vote_average, vote_count, character, popularity
  = ~150 chars per film.

  Med 91 Tarantino-film = ~13.700 chars → _trim_tool_result() kapper til
  10 items (_slim_data max_list_items=10). Kun de 10 mest populære film
  overlever — Jackie Brown, Kill Bill Vol.2, Inglourious Basterds etc.
  trimmes væk, og Buddy gætter forkerte ID'er fra træningsdata.

FIX:
  Returner kun de 3 felter Buddy faktisk bruger:
    tmdb_id, title (dansk), original_title (engelsk), release_date
  = ~65 chars per film.

  91 film × 65 chars = ~5.900 chars → alle passer inden for 6000-grænsen.
  Buddy behøver ikke vote_average/count/character til at lave Plex-tjek.

INSTRUKTION:
  Find denne blok i services/tmdb_service.py og erstat den:

--- FIND (erstat hele listen-comprehension) ---
    movie_credits = sorted(seen.values(), key=lambda x: x.get("popularity", 0), reverse=True)
    movie_credits = [
        {
            "tmdb_id":        m.get("id"),
            "title":          m.get("title") or m.get("original_title"),
            "original_title": m.get("original_title") or m.get("title"),
            "release_date":   m.get("release_date", "Ukendt"),
            "vote_average":   round(m.get("vote_average", 0), 1),
            "vote_count":     m.get("vote_count", 0),
            "character":      m.get("character", ""),
            "popularity":     round(m.get("popularity", 0), 1),
        }
        for m in movie_credits
    ]

--- ERSTAT MED ---
    movie_credits = sorted(seen.values(), key=lambda x: x.get("popularity", 0), reverse=True)
    movie_credits = [
        {
            "tmdb_id":        m.get("id"),
            "title":          m.get("title") or m.get("original_title"),
            "original_title": m.get("original_title") or m.get("title"),
            "release_date":   m.get("release_date", "Ukendt"),
        }
        for m in movie_credits
    ]

Ingen andre ændringer i filen.
"""

# Eksempel på korrekt output efter fix (Tarantino):
# {"tmdb_id":680,"title":"Pulp Fiction","original_title":"Pulp Fiction","release_date":"1994-09-10"}
# {"tmdb_id":500,"title":"Håndlangerne","original_title":"Reservoir Dogs","release_date":"1992-09-02"}
# {"tmdb_id":15551,"title":"Jackie Brown","original_title":"Jackie Brown","release_date":"1997-12-25"}
# {"tmdb_id":24,"title":"Kill Bill: Vol. 1","original_title":"Kill Bill: Vol. 1","release_date":"2003-10-10"}
# ...alle 91 film passer nu inden for 6000 chars