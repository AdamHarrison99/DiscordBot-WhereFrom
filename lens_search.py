"""Google Lens reverse image search via SerpApi.

SerpApi needs a publicly fetchable image URL. Discord CDN URLs already are
one, and so is any ordinary hotlinkable image URL, so URLs are passed
straight through. An earlier version re-uploaded non-Discord URLs to
Catbox first; that was dropped because it broke working URLs and Catbox
has uploads disabled (HTTP 412, "Uploads paused until I can resolve
storage issues"). If a re-host ever becomes necessary, note that
detecting the need costs a second SerpApi search, which matters on the
100-searches/month free tier.
"""

from __future__ import annotations

import aiohttp

SERPAPI_URL = "https://serpapi.com/search"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)


class LensSearchError(Exception):
    """A reverse image search could not be completed."""


class LensQuotaExceeded(LensSearchError):
    """The configured SerpApi account has run out of searches."""


class LensAuthError(LensSearchError):
    """The SerpApi key is missing or rejected."""


class LensNoResults(LensSearchError):
    """The search ran fine but Google Lens matched nothing.

    SerpApi reports this through the `error` field rather than an empty
    result set, so it has to be teased apart from real failures. Lens also
    returns this when it cannot fetch the image at all - notably for
    `upload.wikimedia.org` URLs, which silently yield nothing.
    """


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


def top_matches(payload: dict, limit: int = 4) -> list[dict]:
    matches = payload.get("visual_matches") or []
    return matches[:limit]
