"""Google Lens reverse image search via SerpApi, with a Catbox fallback
for source images whose URL isn't already publicly reachable (Discord's
CDN URLs are used as-is)."""

from __future__ import annotations

import aiohttp

SERPAPI_URL = "https://serpapi.com/search"
CATBOX_URL = "https://catbox.moe/user/api.php"

DISCORD_CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")


class LensSearchError(Exception):
    """A reverse image search could not be completed."""


class LensQuotaExceeded(LensSearchError):
    """The configured SerpApi account has run out of searches."""


async def ensure_public_url(session: aiohttp.ClientSession, image_url: str) -> str:
    if any(host in image_url for host in DISCORD_CDN_HOSTS):
        return image_url
    return await _upload_to_catbox(session, image_url)


async def _upload_to_catbox(session: aiohttp.ClientSession, image_url: str) -> str:
    data = {"reqtype": "urlupload", "url": image_url}
    try:
        async with session.post(CATBOX_URL, data=data) as resp:
            text = (await resp.text()).strip()
    except aiohttp.ClientError as exc:
        raise LensSearchError("Couldn't fetch that image URL.") from exc

    if resp.status != 200 or not text.startswith("http"):
        raise LensSearchError("Couldn't fetch that image URL.")
    return text


async def search_google_lens(session: aiohttp.ClientSession, image_url: str, api_key: str) -> dict:
    params = {
        "engine": "google_lens",
        "url": image_url,
        "api_key": api_key,
    }
    try:
        async with session.get(SERPAPI_URL, params=params) as resp:
            payload = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise LensSearchError("Couldn't reach the search service.") from exc

    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        if "run out" in error.lower() or "quota" in error.lower() or resp.status == 429:
            raise LensQuotaExceeded(error)
        raise LensSearchError(error)

    if resp.status != 200:
        raise LensSearchError(f"Search service returned an error ({resp.status}).")

    return payload


def top_matches(payload: dict, limit: int = 4) -> list[dict]:
    matches = payload.get("visual_matches") or []
    return matches[:limit]
