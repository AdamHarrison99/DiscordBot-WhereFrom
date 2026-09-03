"""Google Lens reverse image search via SerpApi.

Image URLs are passed through as-is; SerpApi fetches them itself.
"""

from __future__ import annotations

import aiohttp

SERPAPI_URL = "https://serpapi.com/search"

# A backstop for a hung request, not the expected duration.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class LensSearchError(Exception):
    """A reverse image search could not be completed."""


class LensQuotaExceeded(LensSearchError):
    """The configured SerpApi account has run out of searches."""


class LensAuthError(LensSearchError):
    """The SerpApi key is missing or rejected."""


class LensNoResults(LensSearchError):
    """The search ran fine but Google Lens matched nothing.

    Also what an image Lens could not fetch at all looks like."""


class LensBadImageUrl(LensSearchError):
    """The supplied string isn't a usable http(s) image URL."""


def validate_image_url(image_url: str) -> str:
    url = (image_url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise LensBadImageUrl("that doesn't look like an image link")
    return url


async def search_google_lens(
    session: aiohttp.ClientSession, image_url: str, api_key: str
) -> dict:
    params = {
        "engine": "google_lens",
        # `type` is required by SerpApi; visual_matches is the tab that
        # yields "where did this image come from" style results.
        "type": "visual_matches",
        "url": validate_image_url(image_url),
        "api_key": api_key,
    }
    try:
        async with session.get(SERPAPI_URL, params=params, timeout=REQUEST_TIMEOUT) as resp:
            payload = await resp.json(content_type=None)
            status = resp.status
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise LensSearchError("couldn't reach the search service") from exc

    if not isinstance(payload, dict):
        raise LensSearchError("search service returned an unexpected response")

    error = payload.get("error")
    if error:
        raise _classify_error(error, status)
    if status != 200:
        raise LensSearchError(f"search service returned an error ({status})")

    return payload


def _classify_error(error: str, status: int) -> LensSearchError:
    lowered = error.lower()
    if "hasn't returned any results" in lowered or "no results" in lowered:
        return LensNoResults(error)
    if status == 429 or "run out" in lowered or "exceeded" in lowered or "quota" in lowered:
        return LensQuotaExceeded(error)
    if status == 401 or "invalid api key" in lowered:
        return LensAuthError(error)
    return LensSearchError(error)


def normalize_matches(payload: dict) -> list[dict]:
    """visual_matches in the shared embed shape. Lens scores no similarity."""
    matches = []
    for match in payload.get("visual_matches") or []:
        link = match.get("link")
        if not link:
            continue
        matches.append(
            {
                "title": match.get("title") or "Untitled match",
                "link": link,
                "source": match.get("source"),
                "thumbnail": match.get("thumbnail") or match.get("image"),
                "similarity": None,
            }
        )
    return matches
