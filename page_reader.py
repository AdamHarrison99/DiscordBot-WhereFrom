"""Fetch a URL and turn it into text for the chat agent.

The model supplies the URL, so the guards are about where a fetch can point: a
public-address check per hop, a byte cap, and text types only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)

# Enough for a long article once the markup is gone; the cap that matters is
# max_chars, applied after extraction.
MAX_BYTES = 500_000

DEFAULT_MAX_CHARS = 3000

# Followed by hand so every hop can be re-checked - see fetch_page.
MAX_REDIRECTS = 3

REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# An honest agent gets through more bot walls here than a fake Chrome.
USER_AGENT = "WhereFromBot/1.0 (+https://github.com/AdamHarrison99/DiscordBot-WhereFrom)"

TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")
IMAGE_TYPES = ("image/",)


class PageError(Exception):
    """A page could not be read."""


class PageBadUrl(PageError):
    """Not an http(s) URL, or one pointing somewhere it shouldn't."""


class PageIsImage(PageError):
    """The URL is an image, not a page. `url` is the (redirect-resolved) address,
    so the caller can hand it to the reverse image search instead."""

    def __init__(self, url: str) -> None:
        super().__init__("that link is an image, not a page")
        self.url = url


class PageNotText(PageError):
    """A PDF, a video, a download - nothing readable as text."""


class PageBlocked(PageError):
    """The site refused us: bot wall, login wall, or rate limit."""


class PageNotFound(PageError):
    """404 or 410."""


def validate_url(url: str) -> str:
    cleaned = (url or "").strip().strip("<>")
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PageBadUrl("that doesn't look like a web address")
    try:
        parts.port
    except ValueError as exc:
        # urlsplit only parses the port when asked, and throws if it's nonsense.
        raise PageBadUrl("that link has an impossible port") from exc
    return cleaned


async def ensure_public(url: str) -> None:
    """Reject anything resolving to a private, loopback or reserved address.

    Not rebinding-proof - see agentic/CLAUDE.md."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await _getaddrinfo(host, port)
    except socket.gaierror as exc:
        raise PageBadUrl(f"couldn't resolve {host}") from exc

    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise PageBadUrl(f"{host} resolved to something unusable")
        if not address.is_global:
            raise PageBadUrl(f"{host} isn't a public address")


async def _getaddrinfo(host: str, port: int):
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _classify_status(status: int) -> PageError:
    if status in (404, 410):
        return PageNotFound(f"there's nothing at that link ({status})")
    if status in (401, 403, 429, 451):
        return PageBlocked(f"the site refused to serve that page ({status})")
    return PageError(f"the site returned an error ({status})")


async def fetch_page(
    session: aiohttp.ClientSession, url: str, max_chars: int = DEFAULT_MAX_CHARS
) -> dict:
    """Returns the page as {url, title, description, image, text, truncated}.

    Redirects are followed by hand so every hop reaches ensure_public."""
    current = as_reddit_json(validate_url(url))
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"}

    for _ in range(MAX_REDIRECTS + 1):
        await ensure_public(current)
        try:
            async with session.get(
                current, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False
            ) as resp:
                if resp.status in REDIRECT_STATUSES and resp.headers.get("Location"):
                    current = validate_url(urljoin(current, resp.headers["Location"]))
                    continue
                if resp.status != 200:
                    raise _classify_status(resp.status)

                content_type = (resp.content_type or "").lower()
                if content_type.startswith(IMAGE_TYPES):
                    raise PageIsImage(current)
                if not content_type.startswith(TEXT_TYPES):
                    raise PageNotText(f"that link is a {content_type or 'file'}, not a page")

                raw = await resp.content.read(MAX_BYTES)
                encoding = _encoding_of(resp)
                final_url = str(resp.url)
        except aiohttp.ClientError as exc:
            raise PageError("couldn't reach that page") from exc
        except TimeoutError as exc:
            raise PageError("that page took too long to load") from exc

        text = raw.decode(encoding, errors="replace")
        if content_type == "application/json":
            return read_json(final_url, text, max_chars)
        return read_html(final_url, text, max_chars)

    raise PageError("that link redirects in circles")


def _encoding_of(resp: aiohttp.ClientResponse) -> str:
    try:
        return resp.get_encoding()
    except (RuntimeError, LookupError):
        return "utf-8"


SKIP_TAGS = frozenset({
    "script", "style", "noscript", "svg", "template", "iframe", "canvas",
    "nav", "footer", "aside", "form", "button", "select", "option",
})

# Tags whose content deserves a line of its own once the markup is gone.
BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "section", "article", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
})

META_KEYS = ("og:title", "og:description", "description", "og:image", "twitter:image")

# Below this, whatever was marked up as the main content clearly wasn't it.
MIN_MAIN_CHARS = 200


class _Extractor(HTMLParser):
    """Just enough HTML to get a title, the og: tags and the visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        # Where the real content starts, if the page says. Wikipedia spends its
        # first 200 characters on its own menus.
        self.main_start: int | None = None
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self.main_start is None and (
            tag in ("main", "article") or dict(attrs).get("role") == "main"
        ):
            self.main_start = len(self.parts)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            values = dict(attrs)
            key = (values.get("property") or values.get("name") or "").lower()
            if key in META_KEYS and values.get("content"):
                self.meta.setdefault(key, values["content"].strip())
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        elif data.strip():
            self.parts.append(data)


WHITESPACE = re.compile(r"[ \t\x0b\f\r]+")
BLANK_LINES = re.compile(r"\n\s*\n\s*\n+")


def collapse(text: str) -> str:
    return BLANK_LINES.sub("\n\n", WHITESPACE.sub(" ", text)).strip()


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Cuts at a word boundary so the model isn't handed half a word."""
    if len(text) <= max_chars:
        return text, False
    cut = text.rfind(" ", 0, max_chars)
    return text[: cut if cut > max_chars // 2 else max_chars].rstrip(), True


def read_html(url: str, html: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    parser = _Extractor()
    try:
        parser.feed(html)
    except Exception:
        # HTMLParser chokes on some malformed pages; whatever it got is still worth having.
        pass

    parts = parser.parts
    if parser.main_start is not None:
        content = collapse("".join(parts[parser.main_start:]))
        # Only trust it if it actually held the article; some pages mark up a
        # sidebar as <article> and leave the text outside it.
        if len(content) >= MIN_MAIN_CHARS:
            parts = parts[parser.main_start:]

    body, truncated = truncate(collapse("".join(parts)), max_chars)
    image = parser.meta.get("og:image") or parser.meta.get("twitter:image") or ""
    return {
        "url": url,
        "title": unescape(collapse(parser.title)) or parser.meta.get("og:title", ""),
        "description": parser.meta.get("og:description") or parser.meta.get("description", ""),
        "image": urljoin(url, image) if image else "",
        "text": body,
        "truncated": truncated,
    }


REDDIT_HOSTS = frozenset({"reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com"})

MAX_REDDIT_COMMENTS = 8


def as_reddit_json(url: str) -> str:
    """Reddit hands out any thread as JSON if you ask for `.json`, no key needed.
    Cloud hosts often get a 403 anyway - that's PageBlocked, not a bug here."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in REDDIT_HOSTS or "/comments/" not in parts.path:
        return url
    path = parts.path.rstrip("/")
    if path.endswith(".json"):
        return url
    return f"{parts.scheme}://{parts.netloc}{path}.json"


def read_json(url: str, body: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    try:
        payload = json.loads(body)
    except ValueError:
        text, truncated = truncate(collapse(body), max_chars)
        return {"url": url, "title": "", "description": "", "image": "",
                "text": text, "truncated": truncated}

    if _looks_like_reddit(payload):
        return read_reddit(url, payload, max_chars)

    text, truncated = truncate(json.dumps(payload, indent=1)[: max_chars * 2], max_chars)
    return {"url": url, "title": "", "description": "", "image": "",
            "text": text, "truncated": truncated}


def _looks_like_reddit(payload) -> bool:
    return (
        isinstance(payload, list)
        and len(payload) >= 2
        and all(isinstance(part, dict) and part.get("kind") == "Listing" for part in payload[:2])
    )


def _children(listing) -> list[dict]:
    data = listing.get("data") if isinstance(listing, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    return [c.get("data", {}) for c in children if isinstance(c, dict)] if children else []


def read_reddit(url: str, payload: list, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    posts = _children(payload[0])
    post = posts[0] if posts else {}

    lines = []
    if post.get("selftext"):
        lines.append(post["selftext"])
    comments = [c for c in _children(payload[1]) if c.get("body")][:MAX_REDDIT_COMMENTS]
    if comments:
        lines.append("\nTop comments:")
        lines += [f"- {c.get('author', '?')} ({c.get('score', 0)}): {c['body']}" for c in comments]

    text, truncated = truncate(collapse("\n".join(lines)), max_chars)
    title = post.get("title", "")
    score = post.get("score")
    if score is not None:
        title = f"{title} ({score} points, {post.get('num_comments', 0)} comments)"
    return {
        "url": url,
        "title": title,
        "description": "",
        # Reddit puts the thumbnail in url_overridden_by_dest for image posts.
        "image": post.get("url_overridden_by_dest", "") if post.get("post_hint") == "image" else "",
        "text": text,
        "truncated": truncated,
    }
