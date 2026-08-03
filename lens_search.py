"""Google Lens reverse image search via SerpApi, with a Catbox fallback
for source images whose URL isn't already publicly reachable (Discord's
CDN URLs are used as-is)."""

from __future__ import annotations

import aiohttp

SERPAPI_URL = "https://serpapi.com/search"
CATBOX_URL = "https://catbox.moe/user/api.php"

DISCORD_CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)


class LensSearchError(Exception):
    """A reverse image search could not be completed."""


class LensQuotaExceeded(LensSearchError):
    """The configured SerpApi account has run out of searches."""


class LensAuthError(LensSearchError):
    """The SerpApi key is missing or rejected."""


async def ensure_public_url(session: aiohttp.ClientSession, image_url: str) -> str:
    if any(host in image_url for host in DISCORD_CDN_HOSTS):
        return image_url
    return await _upload_to_catbox(session, image_url)


async def _upload_to_catbox(session: aiohttp.ClientSession, image_url: str) -> str:
    data = {"reqtype": "urlupload", "url": image_url}
    try:
        async with session.post(CATBOX_URL, data=data, timeout=REQUEST_TIMEOUT) as resp:
            text = (await resp.text()).strip()
            status = resp.status
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise LensSearchError("couldn't fetch that image URL") from exc

    if status != 200 or not text.startswith("http"):
        raise LensSearchError("couldn't fetch that image URL")
    return text


async def search_google_lens(
    session: aiohttp.ClientSession, image_url: str, api_key: str
) -> dict:
    params = {
        "engine": "google_lens",
        # `type` is required by SerpApi; visual_matches is the tab that
        # yields "where did this image come from" style results.
        "type": "visual_matches",
        "url": image_url,
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
    if status == 429 or "run out" in lowered or "exceeded" in lowered or "quota" in lowered:
        return LensQuotaExceeded(error)
    if status == 401 or "invalid api key" in lowered:
        return LensAuthError(error)
    return LensSearchError(error)


def top_matches(payload: dict, limit: int = 4) -> list[dict]:
    matches = payload.get("visual_matches") or []
    return matches[:limit]
