"""Short chat replies for @-mentions, via OpenRouter's auto router."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import NamedTuple

ToolRunner = Callable[[dict], Awaitable[str]]

import aiohttp

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

AUTO_ROUTER_MODEL = "openrouter/auto"
# Free vision endpoints aren't reachable on this account, so free-only loses images.
FREE_ROUTER_MODEL = "openrouter/free"

# Routed providers are slower and more variable than SerpApi's 15s.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

MAX_QUESTION_CHARS = 1000
MAX_IMAGES = 4

# One round is enough for "search, then answer", and each round is a paid call.
MAX_TOOL_ROUNDS = 1

# Tool output is appended after the budget has been applied, so cap it here.
# Fits a page read at ~1000 tokens on the follow-up call.
MAX_TOOL_RESULT_CHARS = 4000

# Three sentences is ~70 tokens; the rest is headroom for emoji and CJK, which
# cost several tokens each. The length rule itself lives in the agent context.
DEFAULT_MAX_TOKENS = 150

DEFAULT_MAX_CONTEXT_TOKENS = 2000

# Kept per channel, so people can follow up without repeating themselves.
DEFAULT_MEMORY_TURNS = 10
DEFAULT_MEMORY_MINUTES = 30

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


class ChatRefused(ChatError):
    """The model declined, or a provider filter blanked the reply."""


class ChatEmptyReply(ChatError):
    """Nothing to post. `detail` carries the why for the log; str() stays clean
    because this one is shown in the channel."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("the model returned an empty reply")
        self.detail = detail or "no finish_reason given"


class ChatUnavailable(ChatError):
    """No free provider had capacity."""


class ChatNoEndpoints(ChatUnavailable):
    """Nothing matched the routing constraints - OPENROUTER_MAX_PRICE too low, or
    the account's data policy. Both are config, not code."""


class ChatReply(NamedTuple):
    text: str
    model: str | None
    cost: float
    # Tuple, not list: a NamedTuple default is shared by every instance.
    tools_used: tuple[str, ...] = ()


class Prompt(NamedTuple):
    context: str
    history: list[dict]
    question: str
    dropped_chars: int


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def _history_chars(history: Sequence[dict]) -> int:
    return sum(len(m.get("content") or "") for m in history)


def fit_to_budget(
    context: str, history: Sequence[dict], question: str, max_tokens: int
) -> Prompt:
    """Budget covers the history and the question; oldest history goes first.

    The system prompt is never trimmed - it is the bot's identity and rules, and
    a half-truncated rulebook is worse than a shorter memory. An oversized one is
    a config problem, flagged at startup. Images aren't counted; only the model
    knows what they cost.
    """
    kept = list(history)
    if max_tokens <= 0:
        return Prompt(context, kept, question, 0)

    budget = max_tokens * CHARS_PER_TOKEN
    original = len(question) + _history_chars(kept)
    if original <= budget:
        return Prompt(context, kept, question, 0)

    running = _history_chars(kept)
    while kept and len(question) + running > budget:
        running -= len(kept.pop(0).get("content") or "")

    question = question[: max(budget - running, MIN_QUESTION_CHARS)]
    return Prompt(context, kept, question, original - len(question) - running)


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


class Conversation:
    """Rolling per-channel history, in memory only. A restart forgets everything,
    which is fine - the free tiers wipe disk anyway."""

    def __init__(self, max_turns: int, ttl_seconds: float) -> None:
        # One turn is a user message and its reply.
        self.max_messages = max(max_turns, 0) * 2
        self.ttl = ttl_seconds
        self._log: dict[int, deque[tuple[float, dict]]] = {}

    def history(self, key: int, now: float | None = None) -> list[dict]:
        now = time.monotonic() if now is None else now
        self._expire(key, now)
        return [message for _, message in self._log.get(key, ())]

    def remember(self, key: int, role: str, content: str, now: float | None = None) -> None:
        if not self.max_messages or not content:
            return
        now = time.monotonic() if now is None else now
        self._expire(key, now)
        entries = self._log.setdefault(key, deque(maxlen=self.max_messages))
        entries.append((now, {"role": role, "content": content}))

    def forget(self, key: int) -> bool:
        return self._log.pop(key, None) is not None

    def _expire(self, key: int, now: float) -> None:
        """A conversation resumed hours later is a new one, not a continuation."""
        entries = self._log.get(key)
        if entries and now - entries[-1][0] >= self.ttl:
            del self._log[key]


# Without this the model reads an attachment as an image, whatever it actually is.
AUDIO_NOTE = (
    "\n\n(An audio clip is attached to this message. It is audio, not an image. "
    "Listen to it and answer about what you hear.)"
)

# One clip a message: audio is billed by duration and inflates the body by a third.
MAX_AUDIO_CLIPS = 1


def _user_content(
    question: str, image_urls: Sequence[str], audio: Sequence[tuple[str, str]] = ()
) -> str | list[dict]:
    text = question[:MAX_QUESTION_CHARS]
    if not image_urls and not audio:
        return text
    parts: list[dict] = [{"type": "text", "text": text + (AUDIO_NOTE if audio else "")}]
    parts += [{"type": "image_url", "image_url": {"url": url}} for url in image_urls[:MAX_IMAGES]]
    # OpenRouter takes no URL for audio, so the bytes travel in the body.
    parts += [
        {"type": "input_audio", "input_audio": {"data": data, "format": fmt}}
        for data, fmt in audio[:MAX_AUDIO_CLIPS]
    ]
    return parts


# OpenRouter rejects a longer array outright, so the tail is dropped, not sent.
MAX_FALLBACK_MODELS = 3


def model_field(model: str | Sequence[str]) -> dict:
    """One id sends `model`; several send `models`, which OpenRouter tries in
    order, moving on when one errors or is rate-limited."""
    ids = [model] if isinstance(model, str) else [m for m in model if m]
    # An empty list would send `models: []`, which is a request nothing can serve.
    ids = (ids or [FREE_ROUTER_MODEL])[:MAX_FALLBACK_MODELS]
    return {"model": ids[0]} if len(ids) == 1 else {"models": ids}


def build_request(
    context: str,
    question: str,
    model: str | Sequence[str],
    max_tokens: int,
    image_urls: Sequence[str] = (),
    max_price: float | None = None,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    history: Sequence[dict] = (),
    audio: Sequence[tuple[str, str]] = (),
) -> dict:
    prompt = fit_to_budget(context, history, question, max_context_tokens)
    body = {
        **model_field(model),
        "max_tokens": max_tokens,
        # Reasoning tokens come out of max_tokens, so a thinking model can spend
        # the whole budget and return empty content. Chat replies don't need it.
        "reasoning": {"enabled": False},
        "messages": [
            {"role": "system", "content": prompt.context},
            *prompt.history,
            {"role": "user", "content": _user_content(prompt.question, image_urls, audio)},
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
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    # Reasoning models can fill `reasoning` and leave content empty.
    content = (message.get("content") or "").strip()

    if not content:
        refusal = (message.get("refusal") or "").strip()
        if refusal or finish in ("content_filter", "error"):
            raise ChatRefused(refusal or f"declined ({finish})")
        detail = f"finish_reason={finish}, native={choice.get('native_finish_reason')}"
        # Some models reason regardless of reasoning.enabled=false and spend the
        # whole budget doing it. Worth naming - the fix is more max_tokens.
        if message.get("reasoning"):
            detail += f", spent the budget reasoning ({_reasoning_tokens(payload)} tokens)"
        raise ChatEmptyReply(detail)

    if finish == "length":
        content = _drop_dangling_sentence(content)
    return content


def _reasoning_tokens(payload: dict) -> int:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        return 0
    try:
        return int(details.get("reasoning_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _reported_cost(payload: dict) -> float:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    try:
        return float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _post(session: aiohttp.ClientSession, body: dict, headers: dict) -> dict:
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
    return payload


def _tool_calls(payload: dict) -> list[dict]:
    choices = payload.get("choices") or []
    if not choices:
        return []
    calls = (choices[0].get("message") or {}).get("tool_calls")
    return calls if isinstance(calls, list) else []


async def ask_once(
    session: aiohttp.ClientSession,
    system: str,
    user: str,
    *,
    api_key: str,
    model: str | Sequence[str],
    max_tokens: int,
    max_price: float | None = None,
) -> ChatReply:
    """One completion, no tools, no history, no images. For classification, where
    the persona and the budget machinery would only be cost."""
    body = {
        **model_field(model),
        "max_tokens": max_tokens,
        "reasoning": {"enabled": False},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if max_price is not None:
        body["provider"] = {"max_price": {"prompt": max_price, "completion": max_price}}

    payload = await _post(session, body, build_headers(api_key))
    return ChatReply(_extract_reply(payload), payload.get("model"), _reported_cost(payload))


async def ask(
    session: aiohttp.ClientSession,
    context: str,
    question: str,
    *,
    api_key: str,
    model: str | Sequence[str] = AUTO_ROUTER_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    image_urls: Sequence[str] = (),
    max_price: float | None = None,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    history: Sequence[dict] = (),
    tools: Sequence[dict] = (),
    tool_runner: ToolRunner | None = None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    audio: Sequence[tuple[str, str]] = (),
) -> ChatReply:
    headers = build_headers(api_key)
    body = build_request(
        context, question, model, max_tokens, image_urls, max_price,
        max_context_tokens, history, audio,
    )
    if tools and tool_runner:
        body["tools"] = list(tools)

    cost = 0.0
    used_tools: list[str] = []
    for remaining in range(max_tool_rounds, -1, -1):
        payload = await _post(session, body, headers)
        cost += _reported_cost(payload)

        calls = _tool_calls(payload) if tool_runner else []
        if not calls or not remaining:
            break

        # The assistant turn asking for the call has to go back verbatim.
        body["messages"].append((payload["choices"][0].get("message") or {}))
        for call in calls:
            name = ((call.get("function") or {}).get("name")) or "?"
            used_tools.append(name)
            body["messages"].append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": (await tool_runner(call))[:MAX_TOOL_RESULT_CHARS],
            })

        # The next round is the last, so forbid another tool call: a model that
        # spends it asking for one returns empty content and nothing to post.
        if remaining == 1:
            body["tool_choice"] = "none"

    return ChatReply(_extract_reply(payload), payload.get("model"), cost, tuple(used_tools))
