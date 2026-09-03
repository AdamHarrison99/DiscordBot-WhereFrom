"""Deciding whether to speak in a conversation nobody addressed to the bot.

No discord imports, so the whole decision path is testable against plain records.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import aiohttp

from chat_agent import FREE_ROUTER_MODEL, ask_once

DEFAULT_THRESHOLD = 70
DEFAULT_DEBOUNCE_SECONDS = 5
DEFAULT_MAX_WAIT_SECONDS = 30
DEFAULT_COOLDOWN_SECONDS = 120
DEFAULT_BUFFER_MESSAGES = 12
DEFAULT_STALE_MESSAGES = 3

# The gate is text-only, which is the one thing the free router does at zero cost.
DEFAULT_GATE_MODEL = FREE_ROUTER_MODEL

# Three short lines. Cheap models pad past this given room.
GATE_MAX_TOKENS = 40

# Long enough to judge intent, short enough that twelve of them stay cheap.
MAX_RECORD_CHARS = 200
MAX_AUTHOR_CHARS = 40

# Every image is prompt cost on a vision-priced model, and a picture six messages
# back is rarely the one being discussed.
MAX_REPLY_IMAGES = 2

# Audio is billed by duration, so the reply listens to one clip at most.
MAX_REPLY_AUDIO = 1

MODES = ("observe", "reply")

IMAGE_MARK = "[image attached]"
AUDIO_MARK = "[voice message or audio clip attached]"
SELF_MARK = "(this bot)"
OTHER_BOT_MARK = "(a bot)"
OTHER_FILE_MARK = "[attached a file I can't open]"

CONTROL_CHARS = re.compile(r"[\r\n\t\x00-\x1f]")

# The ambient reply is offered no tools, so a link is a page it cannot open.
LINK = re.compile(r"https?://\S+")


class MessageRecord(NamedTuple):
    author: str
    author_id: int
    is_bot: bool
    text: str
    image_urls: tuple[str, ...]
    message_id: int
    at: float
    # Video and documents: marked, never fetched or described.
    other_files: bool = False
    # (url, format) per clip, fetched only if the bot decides to answer.
    audio: tuple[tuple[str, str], ...] = ()
    # The message this one is a discord reply to.
    reply_to: int | None = None


class Verdict(NamedTuple):
    score: int
    target: int | None
    reason: str


class Judgement(NamedTuple):
    verdict: Verdict
    cost: float
    model: str | None


def flatten(text: str, limit: int = MAX_RECORD_CHARS) -> str:
    """Transcript lines are numbered, so an embedded newline would forge one."""
    return CONTROL_CHARS.sub(" ", text).strip()[:limit]


class ChannelBuffer:
    """Rolling per-channel record of what was said. In memory only, like
    `Conversation` - a restart forgets, which is the intended lifetime."""

    def __init__(self, max_messages: int, ttl_seconds: float) -> None:
        self.max_messages = max(max_messages, 0)
        self.ttl = ttl_seconds
        self._log: dict[int, deque[MessageRecord]] = {}

    def add(self, key: int, record: MessageRecord, now: float | None = None) -> None:
        if not self.max_messages:
            return
        now = time.monotonic() if now is None else now
        self._expire(key, now)
        self._log.setdefault(key, deque(maxlen=self.max_messages)).append(record)

    def recent(self, key: int, limit: int | None = None, now: float | None = None) -> list[MessageRecord]:
        now = time.monotonic() if now is None else now
        self._expire(key, now)
        records = list(self._log.get(key, ()))
        return records if limit is None else records[-limit:]

    def newest_id(self, key: int) -> int | None:
        entries = self._log.get(key)
        return entries[-1].message_id if entries else None

    def size(self, key: int) -> int:
        return len(self._log.get(key, ()))

    def forget(self, key: int) -> bool:
        return self._log.pop(key, None) is not None

    def _expire(self, key: int, now: float) -> None:
        """A channel resumed hours later is a new conversation, not a continuation."""
        entries = self._log.get(key)
        if entries and now - entries[-1].at >= self.ttl:
            del self._log[key]


class AmbientLimits:
    """Every free reason not to speak, in one place, each one nameable so the
    observe log says which guard fired."""

    def __init__(
        self,
        channels: Sequence[int] = (),
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.channels = frozenset(channels)
        self.cooldown = cooldown_seconds
        self._last_reply: dict[int, float] = {}

    def allow(
        self, key: int, records: Sequence[MessageRecord], now: float | None = None
    ) -> str | None:
        """None to proceed, else a refusal the log prints verbatim."""
        now = time.monotonic() if now is None else now
        if key not in self.channels:
            return "channel not enabled"
        if not records:
            return "nothing buffered"

        last = self._last_reply.get(key)
        if last is not None:
            if now - last < self.cooldown:
                return "cooldown"
            # Without this, a quiet channel gets a monologue every cooldown.
            if not any(r.at > last and not r.is_bot for r in records):
                return "no human has spoken since my last reply"
        return None

    def record_reply(self, key: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._last_reply[key] = now


# Both prompts are files, never constants - see agentic/CLAUDE.md.
class JudgePrompts(NamedTuple):
    system: str
    template: str


class ReplyPrompts(NamedTuple):
    """What the ambient reply is told, from reply_template.md."""

    context: str
    instruction: str


def parse_sections(text: str, names: Sequence[str]) -> dict[str, str]:
    """Splits a file into the "# Heading" sections asked for, and only those.

    Raises ValueError if one is missing."""
    pattern = re.compile(r"^#\s*(" + "|".join(names) + r")\s*$", re.IGNORECASE | re.MULTILINE)
    # A byte order mark would sit in front of the first heading and hide it.
    parts = pattern.split(text.lstrip("﻿"))
    found = {parts[i].lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    missing = [name for name in names if not found.get(name)]
    if missing:
        raise ValueError("no " + " or ".join("# " + name for name in missing) + " section")
    return found


def parse_judge_file(text: str) -> JudgePrompts:
    """Splits a "# System" / "# Template" file. Raises ValueError on anything else."""
    found = parse_sections(text, ("system", "template"))
    if "{transcript}" not in found["template"]:
        raise ValueError("the template has no {transcript} placeholder")
    return JudgePrompts(found["system"], found["template"])


def parse_reply_file(text: str) -> ReplyPrompts:
    """Splits a "# Context" / "# Instruction" file."""
    found = parse_sections(text, ("context", "instruction"))
    return ReplyPrompts(found["context"], found["instruction"])


def read_prompt_file(path: Path, parse):
    """Notepad writes a BOM, which the parser strips again."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError("can't read " + path.name + ": " + str(exc)) from exc
    return parse(text)


def load_judge_prompts(path: Path) -> JudgePrompts:
    """Reads and validates the gate's prompt file."""
    return read_prompt_file(path, parse_judge_file)


def load_reply_prompts(path: Path) -> ReplyPrompts:
    """Reads and validates the ambient reply's prompt file."""
    return read_prompt_file(path, parse_reply_file)


def said(record: MessageRecord) -> str:
    """What one message contributes: its text, plus a note for anything attached."""
    parts = [flatten(record.text)]
    if record.image_urls:
        parts.append(IMAGE_MARK)
    if record.audio:
        parts.append(AUDIO_MARK)
    if record.other_files:
        parts.append(OTHER_FILE_MARK)
    return " ".join(part for part in parts if part)


def speaker(record: MessageRecord, bot_id: int | None) -> str:
    """Marked, so a criterion about what the bot itself said is checkable at all."""
    who = flatten(record.author, MAX_AUTHOR_CHARS)
    if bot_id is not None and record.author_id == bot_id:
        return f"{who} {SELF_MARK}"
    return f"{who} {OTHER_BOT_MARK}" if record.is_bot else who


def reply_note(record: MessageRecord, positions: dict[int, int]) -> str:
    """Which line a message answers, when that line is in the window."""
    if record.reply_to is None:
        return ""
    number = positions.get(record.reply_to)
    return f"(replying to {number})" if number else "(replying to an earlier message)"


def build_transcript(records: Sequence[MessageRecord], bot_id: int | None = None) -> str:
    positions = {record.message_id: number for number, record in enumerate(records, 1)}
    lines = []
    for number, record in enumerate(records, 1):
        note = reply_note(record, positions)
        head = f"{number}. {speaker(record, bot_id)}"
        if note:
            head = f"{head} {note}"
        lines.append(f"{head}: {said(record)}".rstrip())
    return "\n".join(lines)


def build_judge_prompt(
    records: Sequence[MessageRecord],
    *,
    template: str,
    bot_id: int | None = None,
) -> str:
    """No persona: the gate decides whether, not what. Substituted, not formatted."""
    return template.replace("{transcript}", build_transcript(records, bot_id))


SCORE_RE = re.compile(r"^\s*SCORE\s*[:=]\s*(-?\d+)", re.IGNORECASE | re.MULTILINE)
TARGET_RE = re.compile(r"^\s*TARGET\s*[:=]\s*(\d+)", re.IGNORECASE | re.MULTILINE)
REASON_RE = re.compile(r"^\s*REASON\s*[:=]\s*(\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_verdict(text: str, transcript_len: int = 0) -> Verdict:
    """Fails closed: anything unparseable scores 0, which is silence."""
    score_match = SCORE_RE.search(text or "")
    if not score_match:
        return Verdict(0, None, "unparseable verdict")

    score = max(0, min(100, int(score_match.group(1))))

    target = None
    target_match = TARGET_RE.search(text)
    if target_match:
        candidate = int(target_match.group(1))
        if 1 <= candidate <= transcript_len:
            target = candidate

    reason_match = REASON_RE.search(text)
    reason = flatten(reason_match.group(1)) if reason_match else ""
    return Verdict(score, target, reason)


def answered_ids(records: Sequence[MessageRecord], bot_id: int | None) -> set[int]:
    """Messages the bot has already replied to. Its own reply is the record of
    that, so a second pass at the same message is visible without extra state."""
    if bot_id is None:
        return set()
    return {
        record.reply_to
        for record in records
        if record.author_id == bot_id and record.reply_to is not None
    }


def usable_target(
    records: Sequence[MessageRecord], target: int | None, bot_id: int | None
) -> int | None:
    """Drops a target the bot has answered, or one of its own. The score stands."""
    if target is None or not 1 <= target <= len(records):
        return None
    chosen = records[target - 1]
    if bot_id is not None and chosen.author_id == bot_id:
        return None
    return None if chosen.message_id in answered_ids(records, bot_id) else target


async def judge(
    session: aiohttp.ClientSession,
    records: Sequence[MessageRecord],
    *,
    api_key: str,
    prompts: JudgePrompts,
    model: str | Sequence[str] = DEFAULT_GATE_MODEL,
    bot_id: int | None = None,
    max_price: float | None = None,
) -> Judgement:
    reply = await ask_once(
        session,
        prompts.system,
        build_judge_prompt(records, template=prompts.template, bot_id=bot_id),
        api_key=api_key,
        model=model,
        max_tokens=GATE_MAX_TOKENS,
        max_price=max_price,
    )
    return Judgement(parse_verdict(reply.text, len(records)), reply.cost, reply.model)


def build_reply_history(records: Sequence[MessageRecord], bot_id: int) -> list[dict]:
    """Turns in the mention path's `name: text` shape, replies marked.

    Only other people's turns are annotated."""
    parents = {record.message_id: record for record in records}
    history = []
    for index, record in enumerate(records):
        content = said(record)
        if not content:
            continue
        if record.author_id == bot_id:
            history.append({"role": "assistant", "content": flatten(record.text)})
            continue
        name = flatten(record.author, MAX_AUTHOR_CHARS)
        parent = parents.get(record.reply_to) if record.reply_to is not None else None
        adjacent = index > 0 and records[index - 1].message_id == record.reply_to
        note = ""
        if parent is not None and not adjacent:
            who = "you" if parent.author_id == bot_id else flatten(parent.author, MAX_AUTHOR_CHARS)
            note = f" (replying to {who})"
        history.append({"role": "user", "content": f"{name}{note}: {content}"})
    return history


def images_for_reply(
    records: Sequence[MessageRecord], target: int | None = None
) -> list[str]:
    """The targeted message's pictures, else the most recent ones on offer."""
    if target is not None and 1 <= target <= len(records):
        chosen = records[target - 1]
        if chosen.image_urls:
            return list(chosen.image_urls[:MAX_REPLY_IMAGES])
    for record in reversed(records):
        if record.image_urls:
            return list(record.image_urls[:MAX_REPLY_IMAGES])
    return []


def prompting_record(
    records: Sequence[MessageRecord], target: int | None, bot_id: int
) -> MessageRecord | None:
    """The message a reply is for: the judge's target, else the last human to
    speak. Neither the bot nor another bot is someone to answer."""
    if target is not None and 1 <= target <= len(records):
        chosen = records[target - 1]
        if chosen.author_id != bot_id and not chosen.is_bot:
            return chosen
    for record in reversed(records):
        if record.author_id != bot_id and not record.is_bot:
            return record
    return None


def addressee(records: Sequence[MessageRecord], target: int | None, bot_id: int) -> int | None:
    """Who the reply is for. An id, not a mention - formatting one is discord's."""
    chosen = prompting_record(records, target, bot_id)
    return chosen.author_id if chosen is not None else None


def audio_for_reply(
    records: Sequence[MessageRecord], target: int | None = None
) -> list[tuple[str, str]]:
    """The targeted message's clip, else the most recent one on offer."""
    if target is not None and 1 <= target <= len(records):
        chosen = records[target - 1]
        if chosen.audio:
            return list(chosen.audio[:MAX_REPLY_AUDIO])
    for record in reversed(records):
        if record.audio:
            return list(record.audio[:MAX_REPLY_AUDIO])
    return []


def count_newer(records: Sequence[MessageRecord], message_id: int | None) -> int:
    """How far the conversation has moved since a snapshot, counted by id."""
    if message_id is None:
        return 0
    return sum(1 for record in records if record.message_id > message_id)


def carries_unreadable(record: MessageRecord) -> bool:
    """A file it can't read or a link it can't follow. Images don't count."""
    return record.other_files or bool(LINK.search(flatten(record.text)))


def is_readable(record: MessageRecord) -> bool:
    """A lone video, document or link is context, not a prompt: no gate call."""
    if record.image_urls or record.audio:
        return True
    # Punctuation left behind by a stripped link isn't something said either.
    return any(ch.isalnum() for ch in LINK.sub(" ", flatten(record.text)))


def resolve_mode(raw: str) -> str:
    """Anything unrecognised observes, which is the safe direction."""
    mode = (raw or "").strip().lower()
    return mode if mode in MODES else "observe"
