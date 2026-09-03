"""SauceNAO reverse image search, for when Google Lens comes back empty.

It indexes the illustration sources Lens misses. Free tier: 100/day, 6 per 30s.
Matches come back in lens_search's shape.
"""

from __future__ import annotations

from urllib.parse import urlparse

import aiohttp

SAUCENAO_URL = "https://saucenao.com/search.php"

ALL_DATABASES = 999
JSON_OUTPUT = 2

# hide=0 disables SauceNAO's own filtering - see agentic/CLAUDE.md.
HIDE_NOTHING = 0

# Anything below this is almost always an unrelated image that happens to
# share a colour palette.
DEFAULT_MIN_SIMILARITY = 60.0

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class SauceError(Exception):
    """A SauceNAO lookup could not be completed."""


class SauceAuthError(SauceError):
    """The SauceNAO API key is missing or rejected."""


class SauceQuotaExceeded(SauceError):
    """Hit SauceNAO's 30-second or 24-hour search limit."""


def _first_present(data: dict, *keys: str) -> str | None:
    """SauceNAO names the same concept differently per index."""
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return None


def _site_name(link: str) -> str | None:
    host = urlparse(link).netloc
    return host[4:] if host.startswith("www.") else (host or None)


def normalize_match(result: dict) -> dict | None:
    """Convert one SauceNAO result to the shared match shape, or None if it
    carries no usable link."""
    header = result.get("header") or {}
    data = result.get("data") or {}

    urls = data.get("ext_urls") or []
    link = next((u for u in urls if u), None)
    if not link:
        return None

    title = _first_present(data, "title", "eng_name", "material", "source") or "Untitled match"
    author = _first_present(data, "author", "author_name", "member_name", "twitter_user_handle")
    if author:
        title = f"{title} - {author}"

    try:
        similarity = float(header.get("similarity"))
    except (TypeError, ValueError):
        similarity = 0.0

    return {
        "title": title,
        "link": link,
        "source": _site_name(link),
        "thumbnail": header.get("thumbnail"),
        "similarity": similarity,
    }


def _as_int(value: object) -> int | None:
    """SauceNAO returns these as ints or numeric strings depending on field."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _check_header(header: dict, api_key: str) -> None:
    """SauceNAO reports several failures inside a 200 response."""
    status = _as_int(header.get("status")) or 0
    if status < 0:
        raise SauceError(f"SauceNAO rejected the request (status {status})")
    if status > 0:
        raise SauceError(f"SauceNAO had a server error (status {status})")

    if _as_int(header.get("user_id")) == 0 and api_key:
        raise SauceAuthError("SauceNAO did not accept the API key")

    # Negative remaining means that window is spent. The log names which.
    for field, window in (("short_remaining", "30-second"), ("long_remaining", "daily")):
        remaining = _as_int(header.get(field))
        if remaining is not None and remaining < 0:
            raise SauceQuotaExceeded(f"SauceNAO {window} search limit reached")


async def search_saucenao(
    session: aiohttp.ClientSession,
    image_url: str,
    api_key: str,
    *,
    numres: int = 8,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> tuple[list[dict], tuple[int | None, int | None]]:
    """Returns (matches above `min_similarity` best-first, (short, long) quota
    remaining). An empty match list means no confident hit."""
    params = {
        "api_key": api_key,
        "db": ALL_DATABASES,
        "output_type": JSON_OUTPUT,
        "numres": numres,
        "hide": HIDE_NOTHING,
        "url": image_url,
    }

    try:
        async with session.get(SAUCENAO_URL, params=params, timeout=REQUEST_TIMEOUT) as resp:
            status = resp.status
            if status == 403:
                raise SauceAuthError("SauceNAO rejected the API key")
            if status == 413:
                raise SauceError("that image is too large for SauceNAO")
            if status == 429:
                body = await resp.text()
                window = "daily" if "daily" in body.lower() else "30-second"
                raise SauceQuotaExceeded(f"SauceNAO {window} search limit reached")
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise SauceError("couldn't reach SauceNAO") from exc
    except ValueError as exc:
        # Non-JSON body - SauceNAO serves HTML error and maintenance pages.
        raise SauceError("SauceNAO returned an unreadable response") from exc

    if not isinstance(payload, dict):
        raise SauceError("SauceNAO returned an unexpected response")

    header = payload.get("header") or {}
    _check_header(header, api_key)

    matches = []
    for result in payload.get("results") or []:
        match = normalize_match(result)
        if match and match["similarity"] >= min_similarity:
            matches.append(match)

    # The embed answers with matches[0], which is ours to guarantee.
    matches.sort(key=lambda m: m["similarity"], reverse=True)
    quota = (header.get("short_remaining"), header.get("long_remaining"))
    return matches, quota
