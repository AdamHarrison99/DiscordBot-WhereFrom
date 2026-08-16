"""Google web search via SerpApi, for the chat agent's lookup tool.

Shares SERPAPI_KEY - and its 100-searches/month free tier - with the reverse
image search, so the caller is expected to ration it.
"""

from __future__ import annotations

import aiohttp

SERPAPI_URL = "https://serpapi.com/search"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

DEFAULT_RESULTS = 5

# The model writes the query, so it needs a ceiling like any other user input.
MAX_QUERY_CHARS = 200

# Public channels, and nothing here needs the explicit results - unlike
# sauce_search, where booru sources are the point.
SAFE_SEARCH = "active"


class WebSearchError(Exception):
    """A web search could not be completed."""


class WebQuotaExceeded(WebSearchError):
    """The configured SerpApi account has run out of searches."""


class WebAuthError(WebSearchError):
    """The SerpApi key is missing or rejected."""


class WebNoResults(WebSearchError):
    """The search ran fine and Google matched nothing. Reported through the
    `error` field rather than an empty result set, as with Lens."""


class WebBadQuery(WebSearchError):
    """The model asked for a search with nothing to search for."""


def validate_query(query: str) -> str:
    cleaned = (query or "").strip()
    if not cleaned:
        raise WebBadQuery("no search query given")
    return cleaned[:MAX_QUERY_CHARS]


async def search_web(
    session: aiohttp.ClientSession,
    query: str,
    api_key: str,
    num_results: int = DEFAULT_RESULTS,
) -> dict:
    params = {
        "engine": "google",
        "q": validate_query(query),
        "num": num_results,
        "safe": SAFE_SEARCH,
        "api_key": api_key,
    }
    try:
        async with session.get(SERPAPI_URL, params=params, timeout=REQUEST_TIMEOUT) as resp:
            payload = await resp.json(content_type=None)
            status = resp.status
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise WebSearchError("couldn't reach the search service") from exc

    if not isinstance(payload, dict):
        raise WebSearchError("search service returned an unexpected response")

    error = payload.get("error")
    if error:
        raise _classify_error(error, status)
    if status != 200:
        raise WebSearchError(f"search service returned an error ({status})")

    return payload


def _classify_error(error: str, status: int) -> WebSearchError:
    lowered = error.lower()
    if "hasn't returned any results" in lowered or "no results" in lowered:
        return WebNoResults(error)
    if status == 429 or "run out" in lowered or "exceeded" in lowered or "quota" in lowered:
        return WebQuotaExceeded(error)
    if status == 401 or "invalid api key" in lowered:
        return WebAuthError(error)
    return WebSearchError(error)


def normalize_results(payload: dict) -> list[dict]:
    """Organic results as title, link, snippet, source - the shape the tool
    result text is built from. Not the reverse-image match shape; nothing here
    reaches build_embed."""
    results = []
    for result in payload.get("organic_results") or []:
        link = result.get("link")
        if not link:
            continue
        results.append(
            {
                "title": result.get("title") or "Untitled result",
                "link": link,
                "snippet": result.get("snippet") or "",
                "source": result.get("source") or result.get("displayed_link"),
                "date": result.get("date"),
            }
        )
    return results


def direct_answer(payload: dict) -> str:
    """Google's own answer, when it has one. Worth having: it costs nothing
    extra and is usually the whole answer for a factual question."""
    box = payload.get("answer_box")
    if isinstance(box, dict):
        for key in ("answer", "result", "snippet", "description"):
            value = box.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    graph = payload.get("knowledge_graph")
    if isinstance(graph, dict):
        description = graph.get("description")
        if isinstance(description, str) and description.strip():
            return description.strip()
    return ""
