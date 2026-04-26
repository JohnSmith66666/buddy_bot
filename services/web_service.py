"""
services/web_service.py - Web search via Tavily API.

Bruger direkte httpx-kald i stedet for tavily-python biblioteket
for at holde det asynkront og non-blocking (ingen sync wrappers).

Tavily API dokumentation: https://docs.tavily.com/docs/tavily-api/rest_api

Returneret struktur til Claude:
  {
    "query":   "hvad handler Kontant på DR1",
    "answer":  "Kontant er et forbrugerprogram på DR1 der...",  # AI-genereret opsummering
    "results": [
      {
        "title":   "Kontant - DR",
        "url":     "https://www.dr.dk/tv/se/kontant",
        "content": "Kort uddrag af siden...",  # max 300 tegn
        "score":   0.92,
      },
      ...
    ]
  }

answer er Tavilys egne LLM-opsummering og er ofte nok til at svare
brugeren direkte. results er backup-kontekst.
"""

import logging

import httpx

from config import TAVILY_API_KEY

logger = logging.getLogger(__name__)

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_RESULTS       = 5
_MAX_CONTENT_CHARS = 300   # Trimmer hvert resultat for token-økonomi


async def search_web(query: str, search_depth: str = "basic") -> dict:
    """
    Udfør en web-søgning via Tavily og returnér LLM-optimeret svar.

    Parametre:
      query:        Søgeforespørgslen — formulér som et naturligt spørgsmål
                    for bedre Tavily-resultater, f.eks.:
                    "hvad handler TV-programmet Kontant på DR1?"
      search_depth: "basic" (hurtig, billig) eller "advanced" (dybere crawl).
                    Standard er "basic" da det er tilstrækkeligt til plot-resuméer.

    Returnerer en dict med:
      - query:   Originale søgning
      - answer:  Tavilys LLM-genererede opsummering (kan være None)
      - results: Liste af top-resultater med title, url, content, score
      - error:   Fejlbesked hvis noget gik galt (results vil være tom)

    Token-optimering:
      - content-feltet trimmes til max _MAX_CONTENT_CHARS tegn per resultat
      - Max _MAX_RESULTS resultater returneres
      - Felter som raw_content, images og irrelevante metadata strippes
    """
    if not TAVILY_API_KEY:
        logger.error("TAVILY_API_KEY er ikke sat i miljøvariablerne.")
        return {
            "query":   query,
            "answer":  None,
            "results": [],
            "error":   "Web-søgning er ikke konfigureret (mangler TAVILY_API_KEY).",
        }

    payload = {
        "api_key":        TAVILY_API_KEY,
        "query":          query,
        "search_depth":   search_depth,
        "include_answer": True,          # Bed Tavily om en LLM-opsummering
        "include_images": False,         # Ingen billeder — sparer tokens
        "include_raw_content": False,    # Ingen rå HTML — sparer tokens
        "max_results":    _MAX_RESULTS,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(_TAVILY_SEARCH_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("Tavily HTTP fejl %s: %s", e.response.status_code, e)
            return {
                "query":   query,
                "answer":  None,
                "results": [],
                "error":   f"Søgning fejlede (HTTP {e.response.status_code}).",
            }
        except httpx.TimeoutException:
            logger.error("Tavily timeout for query: '%s'", query)
            return {
                "query":   query,
                "answer":  None,
                "results": [],
                "error":   "Søgningen tog for lang tid — prøv igen.",
            }
        except Exception as e:
            logger.error("Tavily uventet fejl: %s", e)
            return {
                "query":   query,
                "answer":  None,
                "results": [],
                "error":   f"Uventet fejl: {e}",
            }

    # Byg trimmet resultat-liste
    raw_results = data.get("results") or []
    trimmed_results = []
    for r in raw_results[:_MAX_RESULTS]:
        content = (r.get("content") or "").strip()
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS - 3] + "..."
        trimmed_results.append({
            "title":   (r.get("title") or "").strip(),
            "url":     r.get("url") or "",
            "content": content,
            "score":   round(r.get("score") or 0, 3),
        })

    answer = (data.get("answer") or "").strip() or None

    logger.info(
        "Tavily søgning: query='%s' → answer=%s, %d resultater",
        query, "ja" if answer else "nej", len(trimmed_results),
    )

    return {
        "query":   query,
        "answer":  answer,
        "results": trimmed_results,
    }