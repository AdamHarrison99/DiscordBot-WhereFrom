"""Short chat replies for @-mentions, via OpenRouter's auto router."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import aiohttp

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

AUTO_ROUTER_MODEL = "openrouter/auto"
# Free vision endpoints aren't reachable on this account, so free-only loses images.
FREE_ROUTER_MODEL = "openrouter/free"

# Routed providers are slower and more variable than SerpApi's 15s.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

MAX_QUESTION_CHARS = 1000
MAX_IMAGES = 4

# Three sentences is ~70 tokens; the rest is headroom for emoji and CJK, which
# cost several tokens each. The length rule itself lives in the agent context.
DEFAULT_MAX_TOKENS = 150

DEFAULT_MAX_CONTEXT_TOKENS = 2000

# The router spans many tokenizers, so an exact count isn't possible. Four chars
# per token is the usual approximation and errs towards over-counting English.
CHARS_PER_TOKEN = 4

# The question keeps at least this much even if the system prompt is oversized.
MIN_QUESTION_CHARS = 200

APP_NAME = "WhereFrom"
APP_URL = "https://github.com/AdamHarrison99/DiscordBot-WhereFrom"

# X-Title names the app in OpenRouter's activity log; HTTP-Referer is what it
# attributes the request to. Both go on every call.
ATTRIBUTION_HEADERS = {"HTTP-Referer": APP_URL, "X-Title": APP_NAME}


def build_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", **ATTRIBUTION_HEADERS}


class ChatError(Exception):
    """A chat reply could not be produced."""


class ChatAuthError(ChatError):
    """The OpenRouter API key is missing or rejected."""


class ChatRateLimited(ChatError):
    """Hit OpenRouter's per-minute or daily free-tier limit."""


class ChatUnavailable(ChatError):
    """No free provider had capacity."""


class ChatNoEndpoints(ChatUnavailable):
    """Nothing matched the routing constraints - OPENROUTER_MAX_PRICE too low, or
    the account's data policy. Both are config, not code."""


class ChatReply(NamedTuple):
    text: str
    model: str | None
    cost: float


class Prompt(NamedTuple):
    context: str
    question: str
    dropped_chars: int


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def fit_to_budget(context: str, question: str, max_tokens: int) -> Prompt:
    """Trim the question first, then the system prompt - the prompt carries the
    bot's rules, so it's the last thing worth losing. Images aren't counted:
    their cost is decided by the model, not by anything measurable here."""
    if max_tokens <= 0:
        return Prompt(context, question, 0)

    budget = max_tokens * CHARS_PER_TOKEN
    original = len(context) + len(question)
    if original <= budget:
        return Prompt(context, question, 0)

    question = question[: max(budget - len(context), MIN_QUESTION_CHARS)]
    context = context[: max(budget - len(question), 0)]
    return Prompt(context, question, original - len(context) - len(question))


def load_agent_context(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChatError(f"could not read agent context file {path}: {exc}") from exc
    if not text.strip():
        raise ChatError(f"agent context file {path} is empty")
    return text


class MentionThrottle:
    """Per-user sliding window. The daily cap is account-wide, so one user can
    otherwise drain the whole server's budget."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[int, deque[float]] = {}

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._evict(now)
        hits = self._hits.setdefault(user_id, deque())
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def _evict(self, now: float) -> None:
        for user_id, hits in list(self._hits.items()):
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if not hits:
                del self._hits[user_id]


def _user_content(question: str, image_urls: Sequence[str]) -> str | list[dict]:
    text = question[:MAX_QUESTION_CHARS]
    if not image_urls:
        return text
    return [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": url}} for url in image_urls[:MAX_IMAGES]
    ]


def build_request(
    context: str,
    question: str,
    model: str,
    max_tokens: int,
    image_urls: Sequence[str] = (),
    max_price: float | None = None,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
) -> dict:
    prompt = fit_to_budget(context, question, max_context_tokens)
    body = {
        "model": model,
        "max_tokens": max_tokens,
        # Reasoning tokens come out of max_tokens, so a thinking model can spend
        # the whole budget and return empty content. Chat replies don't need it.
        "reasoning": {"enabled": False},
        "messages": [
            {"role": "system", "content": prompt.context},
            {"role": "user", "content": _user_content(prompt.question, image_urls)},
        ],
    }
    # Omitted by default: too low a ceiling leaves no endpoints and 404s.
    if max_price is not None:
        body["provider"] = {"max_price": {"prompt": max_price, "completion": max_price}}
    return body


def _error_detail(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")
    return str(error) if error else ""


def _raise_for_status(status: int, payload: dict) -> None:
    detail = _error_detail(payload)
    # "No endpoints available matching your guardrail restrictions and data policy"
    # and "No endpoints found that satisfy the max price" are the same class of problem.
    if "no endpoints" in detail.lower():
        raise ChatNoEndpoints(detail)
    if status == 401:
        raise ChatAuthError(detail or "OpenRouter rejected the API key")
    if status == 429:
        raise ChatRateLimited(detail or "OpenRouter free-tier limit reached")
    if status == 402:
        # Unreachable unless a zero-cost guard has a hole.
        raise ChatError(f"OpenRouter asked for payment on a free request: {detail}")
    if status in (502, 503):
        raise ChatUnavailable(detail or "no free model had capacity")
    if status != 200 or detail:
        raise ChatError(detail or f"OpenRouter returned an error ({status})")


SENTENCE_ENDS = ".!?…"

# Below this, dropping the fragment would cost more than the ragged edge does.
KEEP_FRACTION = 0.6


def _drop_dangling_sentence(text: str) -> str:
    """Hitting max_tokens cuts mid-sentence. Fall back to the last complete one,
    unless that throws away most of the answer."""
    cut = max(text.rfind(end) for end in SENTENCE_ENDS)
    if cut < 0 or cut + 1 < len(text) * KEEP_FRACTION:
        return text
    return text[: cut + 1]


def _extract_reply(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise ChatError("the model returned no reply")
    choice = choices[0]
    # Reasoning models can fill `reasoning` and leave content empty.
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        raise ChatError("the model returned an empty reply")
    if choice.get("finish_reason") == "length":
        content = _drop_dangling_sentence(content)
    return content


def _reported_cost(payload: dict) -> float:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    try:
        return float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def ask(
    session: aiohttp.ClientSession,
    context: str,
    question: str,
    *,
    api_key: str,
    model: str = AUTO_ROUTER_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    image_urls: Sequence[str] = (),
    max_price: float | None = None,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
) -> ChatReply:
    headers = build_headers(api_key)
    body = build_request(
        context, question, model, max_tokens, image_urls, max_price, max_context_tokens
    )

    try:
        async with session.post(
            OPENROUTER_URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT
        ) as resp:
            status = resp.status
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise ChatError("couldn't reach OpenRouter") from exc
    except ValueError as exc:
        raise ChatError("OpenRouter returned an unreadable response") from exc

    if not isinstance(payload, dict):
        raise ChatError("OpenRouter returned an unexpected response")

    _raise_for_status(status, payload)
    return ChatReply(_extract_reply(payload), payload.get("model"), _reported_cost(payload))
