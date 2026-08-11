"""WhereFrom - a Discord bot that finds the original source of an image
using Google Lens results via SerpApi."""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import queue
import re
import sys
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from lens_search import (
    LensAuthError,
    LensBadImageUrl,
    LensNoResults,
    LensQuotaExceeded,
    LensSearchError,
    normalize_matches,
    search_google_lens,
)
from sauce_search import (
    DEFAULT_MIN_SIMILARITY,
    SauceAuthError,
    SauceError,
    SauceQuotaExceeded,
    search_saucenao,
)
from chat_agent import (
    AUTO_ROUTER_MODEL,
    DEFAULT_MAX_CONTEXT_TOKENS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MEMORY_MINUTES,
    DEFAULT_MEMORY_TURNS,
    ChatAuthError,
    ChatEmptyReply,
    ChatError,
    ChatNoEndpoints,
    ChatRateLimited,
    ChatRefused,
    ChatUnavailable,
    Conversation,
    MentionThrottle,
    ask,
    estimate_tokens,
    load_agent_context,
)

NO_MATCH_MESSAGE = "No reliable source found for this image."

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILE_DISABLED = ("none", "off")

URL_PATTERN = re.compile(r"https?://\S+")

# Discord signs its CDN URLs with these. They're noise in a terminal; any other
# query param (X's ?format=jpg, say) is part of the address and is kept.
SIGNED_QUERY_PARAMS = frozenset({"ex", "is", "hm"})

CONTROL_CHARS = re.compile(r"[\r\n\t\x00-\x1f]")

# Both upstreams take their key as a query param. Nothing logs a full request
# URL today, but one `raise_for_status()` would start - aiohttp puts the URL in
# ClientResponseError - so scrub at the formatter and stop worrying about it.
SECRET_PARAM = re.compile(r"((?:api_key|apikey|token|key)=)[^&\s\"']+", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    return SECRET_PARAM.sub(r"\1<redacted>", text)


def env_str(name: str, default: str = "") -> str:
    """A blank `KEY=` line counts as unset, so it still gets the default."""
    return os.environ.get(name, "").strip() or default


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    raw = env_str(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def scrub(text: str, limit: int = 200) -> str:
    """Strip control characters from user-supplied text before logging it, so
    nobody can forge log lines by embedding newlines."""
    cleaned = CONTROL_CHARS.sub(" ", text)
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."


def shorten_url(url: str, limit: int = 100) -> str:
    """Drop signed CDN params for terminal display; the file keeps the URL whole."""
    base, _, query = url.partition("?")
    if query:
        kept = []
        for part in query.split("&"):
            # Discord's URLs end in a bare "&=", so drop empty-named params too.
            key = part.split("=", 1)[0]
            if key and key not in SIGNED_QUERY_PARAMS:
                kept.append(part)
        if kept:
            base = f"{base}?{'&'.join(kept)}"
    return base if len(base) <= limit else base[:limit] + "..."


class RedactingFormatter(logging.Formatter):
    """Strips API keys from anything written to a log."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


class ConsoleFormatter(RedactingFormatter):
    """As above, but with URLs shortened for readability."""

    def format(self, record: logging.LogRecord) -> str:
        return URL_PATTERN.sub(lambda m: shorten_url(m.group(0)), super().format(record))


class DropDiscordChatter(logging.Filter):
    """Let discord.py's own records into the file only at WARNING or above."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "discord" or record.name.startswith("discord."):
            return record.levelno >= logging.WARNING
        return True


def configure_streams() -> None:
    """Windows consoles default to cp1252, which can't encode many Discord
    display names. Force UTF-8 so logging can never crash the bot."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def resolve_log_level() -> tuple[str, str | None]:
    """Returns (level, warning). Unset is not the same as invalid."""
    raw = env_str("LOG_LEVEL")
    if not raw:
        return "INFO", None
    level = raw.upper()
    if level in VALID_LOG_LEVELS:
        return level, None
    return "INFO", (
        f"Ignoring unrecognised LOG_LEVEL={raw!r}; using INFO. "
        f"Valid values: {', '.join(VALID_LOG_LEVELS)}"
    )


def build_output_handlers() -> tuple[list[logging.Handler], Path | None, str | None]:
    """Console always; rotating file too unless LOG_FILE is 'none' or 'off'."""
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ConsoleFormatter(LOG_FORMAT))
    handlers: list[logging.Handler] = [console]

    raw_path = env_str("LOG_FILE", "wherefrom.log")
    if raw_path.lower() in LOG_FILE_DISABLED:
        return handlers, None, None

    path = Path(raw_path)
    if not path.is_absolute():
        path = BASE_DIR / path

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            # minimum=1: maxBytes=0 disables rotation entirely, which would
            # defeat the point of capping the file.
            maxBytes=env_int("LOG_MAX_BYTES", 1_000_000),
            backupCount=env_int("LOG_BACKUP_COUNT", 3, minimum=0),
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return handlers, None, f"Could not open log file {path}: {exc}"

    file_handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    file_handler.addFilter(DropDiscordChatter())
    handlers.append(file_handler)
    return handlers, path, None


def setup_logging() -> tuple[str, Path | None, logging.handlers.QueueListener, list[str]]:
    """Route records through a queue so disk writes never block the event loop."""
    configure_streams()
    level, level_warning = resolve_log_level()
    handlers, path, file_error = build_output_handlers()

    record_queue: queue.SimpleQueue = queue.SimpleQueue()
    listener = logging.handlers.QueueListener(
        record_queue, *handlers, respect_handler_level=True
    )
    listener.start()
    atexit.register(listener.stop)

    # Configure the root logger directly rather than via basicConfig: that
    # would attach its own formatter to the QueueHandler, which then formats
    # the record before queueing it and the real handlers format it a second
    # time, doubling every prefix.
    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(logging.handlers.QueueHandler(record_queue))
    root.setLevel(level)
    # discord.py logs every gateway payload at DEBUG, so only opt in deliberately.
    logging.getLogger("discord").setLevel(logging.DEBUG if level == "DEBUG" else logging.INFO)

    warnings = [w for w in (level_warning, file_error) if w]
    return level, path, listener, warnings


# atexit runs LIFO, so this stop() drains the queue before logging.shutdown()
# (registered when logging was imported) closes the handlers underneath it.
LOG_LEVEL, LOG_PATH, LOG_LISTENER, _startup_warnings = setup_logging()
log = logging.getLogger("wherefrom")

for _warning in _startup_warnings:
    log.warning("%s", _warning)
if LOG_PATH:
    log.info("Logging to %s", LOG_PATH)
else:
    log.info("File logging disabled; console only.")

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
SERPAPI_KEY = os.environ["SERPAPI_KEY"]
# Optional. Without it the bot simply doesn't fall back when Lens finds nothing.
SAUCENAO_API_KEY = env_str("SAUCENAO_API_KEY")
SAUCENAO_MIN_SIMILARITY = env_float("SAUCENAO_MIN_SIMILARITY", DEFAULT_MIN_SIMILARITY)

# Optional. Without it the bot ignores @-mentions exactly as it did before.
OPENROUTER_API_KEY = env_str("OPENROUTER_API_KEY")
OPENROUTER_MODEL = env_str("OPENROUTER_MODEL", AUTO_ROUTER_MODEL)
# Per-kind overrides. Unset falls back to OPENROUTER_MODEL, i.e. auto routing.
OPENROUTER_TEXT_MODEL = env_str("OPENROUTER_TEXT_MODEL", OPENROUTER_MODEL)
OPENROUTER_IMAGE_MODEL = env_str("OPENROUTER_IMAGE_MODEL", OPENROUTER_MODEL)
OPENROUTER_MAX_TOKENS = env_int("OPENROUTER_MAX_TOKENS", DEFAULT_MAX_TOKENS)
# Dollars per million tokens. Unset means no ceiling; too low leaves no endpoints.
# Blank and 0 are different: 0 is a real ceiling meaning free-only.
OPENROUTER_MAX_PRICE = (
    env_float("OPENROUTER_MAX_PRICE", 0.0, maximum=1000.0)
    if env_str("OPENROUTER_MAX_PRICE")
    else None
)
MENTION_RATE_LIMIT = env_int("MENTION_RATE_LIMIT_PER_MINUTE", 4)
AGENT_CONTEXT_FILE = env_str("AGENT_CONTEXT_FILE", "agent_context.md")
MAX_CONTEXT_TOKENS = env_int("MAX_CONTEXT_TOKENS", DEFAULT_MAX_CONTEXT_TOKENS)
# 0 turns makes every message standalone again.
CONVERSATION_MEMORY_TURNS = env_int("CONVERSATION_MEMORY_TURNS", DEFAULT_MEMORY_TURNS, minimum=0)
CONVERSATION_MEMORY_MINUTES = env_int("CONVERSATION_MEMORY_MINUTES", DEFAULT_MEMORY_MINUTES)

MAX_SHOWN_MATCHES = 4

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
SAUCE_TRIGGERS = ("?sauce", "!sauce")

DISCORD_MESSAGE_LIMIT = 2000
NO_QUESTION_REPLY = (
    "I look up where images come from. Reply to an image with `?sauce`, or use "
    "`/sauce url:` or `/sauce file:`. Ask me a question and I'll answer that too."
)
RATE_LIMITED_EMOJI = "\N{HOURGLASS}"
DESCRIBE_IMAGE_QUESTION = "What's in this image?"


def resolve_agent_context() -> str | None:
    """Returns the system prompt, or None if the chat feature stays off."""
    if not OPENROUTER_API_KEY:
        return None

    path = Path(AGENT_CONTEXT_FILE)
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        context = load_agent_context(path)
    except ChatError as exc:
        log.error("%s; @-mention replies disabled", exc)
        return None

    tokens = estimate_tokens(context)
    if tokens >= MAX_CONTEXT_TOKENS:
        # Never trimmed, so an oversized one is a bill, not a broken prompt.
        log.warning(
            "%s is ~%d tokens, larger than MAX_CONTEXT_TOKENS=%d. It is always sent in "
            "full, so every reply pays for it and little history will fit alongside",
            path.name,
            tokens,
            MAX_CONTEXT_TOKENS,
        )

    log.info(
        "@-mention replies enabled via %s, context from %s (~%d tokens, plus a %d budget "
        "for history and the question)",
        OPENROUTER_MODEL,
        path,
        tokens,
        MAX_CONTEXT_TOKENS,
    )
    return context


AGENT_CONTEXT = resolve_agent_context()
mention_throttle = MentionThrottle(MENTION_RATE_LIMIT)
conversations = Conversation(
    CONVERSATION_MEMORY_TURNS, CONVERSATION_MEMORY_MINUTES * 60
)

intents = discord.Intents.default()
intents.message_content = True


class WhereFromBot(commands.Bot):
    session: aiohttp.ClientSession

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()
        await self.tree.sync()

    async def close(self) -> None:
        await self.session.close()
        await super().close()


# Match titles come from arbitrary web pages, so an "@everyone" in one would
# otherwise ping the server. replied_user still notifies whoever asked.
bot = WhereFromBot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    allowed_mentions=discord.AllowedMentions(
        everyone=False, users=False, roles=False, replied_user=True
    ),
)


def is_image(attachment: discord.Attachment) -> bool:
    """Discord doesn't always set content_type, so fall back to the extension."""
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def find_image_attachment(message: discord.Message) -> discord.Attachment | None:
    return next((a for a in message.attachments if is_image(a)), None)


def build_embed(matches: list[dict], engine: str) -> discord.Embed | None:
    """Build the reply from normalised matches (see lens_search.normalize_matches)."""
    if not matches:
        return None

    top = matches[0]
    embed = discord.Embed(
        title=top["title"][:100],
        url=top["link"],
        color=discord.Color.blurple(),
    )
    if top.get("thumbnail"):
        embed.set_thumbnail(url=top["thumbnail"])
    if top.get("source"):
        embed.add_field(name="Source site", value=top["source"], inline=True)
    if top.get("similarity") is not None:
        embed.add_field(name="Similarity", value=f"{top['similarity']:.1f}%", inline=True)

    extra = matches[1:MAX_SHOWN_MATCHES]
    if extra:
        lines = [f"[{m['title'][:80]}]({m['link']})" for m in extra]
        embed.add_field(name="Other matches", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Source: {engine}")
    return embed


def describe_invocation(
    user: discord.abc.User,
    channel: discord.abc.MessageableChannel | None,
    guild: discord.Guild | None,
) -> str:
    """Human-readable 'who, where' string for activity logs."""
    if guild is None:
        return f"{user} in DM"
    return f"{user} in {guild}/#{channel}"


def log_matches(engine: str, image_url: str, matches: list[dict]) -> None:
    log.info("%s found %d match(es) for %s", engine, len(matches), image_url)
    for i, match in enumerate(matches[:MAX_SHOWN_MATCHES], start=1):
        log.info(
            "  %d. [%s] %s - %s",
            i,
            match.get("source") or "?",
            match["title"][:80],
            match["link"],
        )
    # The rest never reach the embed, but are worth having when working out
    # why the chosen link isn't the real source.
    for i, match in enumerate(matches[MAX_SHOWN_MATCHES:], start=MAX_SHOWN_MATCHES + 1):
        log.debug("  %d. [%s] %s", i, match.get("source") or "?", match["link"])


async def search_lens(image_url: str) -> tuple[list[dict], str | None]:
    """Returns (matches, fatal_error). No matches with no error means 'try next'."""
    log.debug("Searching Google Lens for %s", image_url)
    try:
        payload = await search_google_lens(bot.session, image_url, SERPAPI_KEY)
    except LensBadImageUrl as exc:
        log.info("Rejected image URL %s: %s", scrub(image_url), exc)
        return [], f"Couldn't search for that image \N{EM DASH} {exc}."
    except LensNoResults:
        return [], None
    except LensQuotaExceeded:
        log.warning("SerpApi quota exhausted")
        return [], "This bot has hit its SerpApi search quota for now. Please try again later."
    except LensAuthError:
        log.error("SerpApi rejected the API key")
        return [], "This bot's search API key isn't working. Please tell the server admin."
    except LensSearchError as exc:
        log.warning("Lens search failed: %s", exc)
        return [], f"Couldn't search for that image \N{EM DASH} {exc}."
    except Exception:
        log.exception("Unexpected error during Google Lens search")
        return [], "Something went wrong while searching for that image."

    matches = normalize_matches(payload)
    if not matches:
        # Distinguish "Lens knows nothing" from "Lens knew things but none were
        # linkable" - they look identical downstream and need different digging.
        returned = len(payload.get("visual_matches") or [])
        if returned:
            log.info("Google Lens returned %d match(es), none with a usable link", returned)
        else:
            log.info("Google Lens found nothing for %s", image_url)
    return matches, None


async def search_sauce(image_url: str) -> list[dict]:
    """Fallback lookup. Never raises - a failure here just means no fallback,
    since the user is already being told Lens found nothing."""
    if not SAUCENAO_API_KEY:
        log.debug("No SAUCENAO_API_KEY set; skipping fallback")
        return []

    log.debug("Falling back to SauceNAO for %s", image_url)
    try:
        matches, (short_left, long_left) = await search_saucenao(
            bot.session,
            image_url,
            SAUCENAO_API_KEY,
            min_similarity=SAUCENAO_MIN_SIMILARITY,
        )
    except SauceQuotaExceeded as exc:
        log.warning("SauceNAO fallback unavailable: %s", exc)
        return []
    except SauceAuthError:
        log.error("SauceNAO rejected the API key; fallback disabled until fixed")
        return []
    except SauceError as exc:
        log.warning("SauceNAO fallback failed: %s", exc)
        return []
    except Exception:
        log.exception("Unexpected error during SauceNAO search")
        return []

    log.debug("SauceNAO quota remaining: %s short, %s long", short_left, long_left)
    return matches


async def lookup_source(image_url: str) -> tuple[list[dict], str, str | None]:
    """Returns (matches, engine, error). Google Lens first; SauceNAO only when
    Lens finds nothing, since Lens regularly misses illustration and anime
    sources."""
    matches, error = await search_lens(image_url)
    engine = "Google Lens via SerpApi"

    if error:
        return [], engine, error

    if not matches:
        matches = await search_sauce(image_url)
        engine = "SauceNAO"

    if matches:
        log_matches(engine, image_url, matches)
    else:
        log.info("No matches found for %s", image_url)
    return matches, engine, None


async def perform_search(image_url: str) -> tuple[discord.Embed | None, str | None]:
    """Returns (embed, error_message) - exactly one of the two is set."""
    matches, engine, error = await lookup_source(image_url)
    if error:
        return None, error
    if not matches:
        return None, NO_MATCH_MESSAGE
    return build_embed(matches, engine), None


@bot.tree.context_menu(name="Find Source")
async def find_source_context_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    who = describe_invocation(interaction.user, interaction.channel, interaction.guild)
    attachment = find_image_attachment(message)
    if attachment is None:
        log.info("'Find Source' by %s - message had no image attachment", who)
        await interaction.response.send_message("That message doesn't have an image attachment.", ephemeral=True)
        return

    log.info("'Find Source' by %s on %s", who, attachment.url)
    await interaction.response.defer(thinking=True)
    embed, error = await perform_search(attachment.url)
    await interaction.followup.send(content=error, embed=embed)


sauce_group = app_commands.Group(name="sauce", description="Find the source of an image")


@sauce_group.command(name="url", description="Find the source of an image by URL")
@app_commands.describe(url="Direct link to an image")
async def sauce_url(interaction: discord.Interaction, url: str) -> None:
    who = describe_invocation(interaction.user, interaction.channel, interaction.guild)
    log.info("'/sauce url' by %s on %s", who, scrub(url))
    await interaction.response.defer(thinking=True)
    embed, error = await perform_search(url)
    await interaction.followup.send(content=error, embed=embed)


@sauce_group.command(name="file", description="Find the source of an uploaded image")
@app_commands.describe(file="Image file to search for")
async def sauce_file(interaction: discord.Interaction, file: discord.Attachment) -> None:
    who = describe_invocation(interaction.user, interaction.channel, interaction.guild)
    if not is_image(file):
        log.info("'/sauce file' by %s - rejected non-image %r", who, scrub(file.filename))
        await interaction.response.send_message("That file doesn't look like an image.", ephemeral=True)
        return

    log.info("'/sauce file' by %s on %r", who, scrub(file.filename))
    await interaction.response.defer(thinking=True)
    embed, error = await perform_search(file.url)
    await interaction.followup.send(content=error, embed=embed)


bot.tree.add_command(sauce_group)


@bot.tree.command(name="forget", description="Make the bot forget this channel's conversation")
async def forget(interaction: discord.Interaction) -> None:
    who = describe_invocation(interaction.user, interaction.channel, interaction.guild)
    log.info("'/forget' by %s", who)

    if interaction.channel is None:
        await interaction.response.send_message("Nothing to forget here.", ephemeral=True)
        return

    cleared = conversations.forget(interaction.channel.id)
    await interaction.response.send_message(
        "Forgotten \N{EM DASH} starting fresh." if cleared else "Nothing to forget yet."
    )


async def handle_sauce_reply(message: discord.Message) -> bool:
    """Run a lookup when someone replies to an image with ?sauce / !sauce.
    Returns True if this message was a trigger, handled or not."""
    if message.author.bot or not message.reference:
        return False

    content = message.content.strip().lower()
    if content not in SAUCE_TRIGGERS:
        return False

    who = describe_invocation(message.author, message.channel, message.guild)

    # Forwarded messages also populate `reference`, but without a message_id.
    reference_id = message.reference.message_id
    if reference_id is None:
        log.info("'%s' by %s - reference has no message id (a forward?)", content, who)
        return True

    try:
        replied = await message.channel.fetch_message(reference_id)
    except discord.NotFound:
        log.info("'%s' by %s - replied-to message no longer exists", content, who)
        return True
    except discord.Forbidden:
        log.warning("'%s' by %s - missing permission to read that message", content, who)
        await message.reply("I don't have permission to read that message.")
        return True
    except discord.HTTPException as exc:
        log.warning("'%s' by %s - couldn't fetch that message: %s", content, who, exc)
        await message.reply("Couldn't read that message just now. Try again in a moment.")
        return True

    attachment = find_image_attachment(replied)
    if attachment is None:
        log.info("'%s' by %s - replied-to message had no image attachment", content, who)
        await message.reply("That message doesn't have an image attachment.")
        return True

    log.info("'%s' by %s on %s", content, who, attachment.url)
    async with message.channel.typing():
        embed, error = await perform_search(attachment.url)
    await message.reply(content=error, embed=embed)
    return True


def bot_mention_forms() -> tuple[str, ...]:
    return () if bot.user is None else (f"<@{bot.user.id}>", f"<@!{bot.user.id}>")


def mentions_bot(message: discord.Message) -> bool:
    """Checked against the text, not `mentions` - Discord lists the replied-to
    author there, which would make every reply look like a mention."""
    return any(form in message.content for form in bot_mention_forms())


async def resolve_reference(message: discord.Message) -> discord.Message | None:
    """The message being replied to. Discord usually inlines it; fetch covers the
    rest, including messages older than this process."""
    reference = message.reference
    if reference is None:
        return None

    replied = reference.resolved
    if isinstance(replied, discord.Message):
        return replied
    if reference.message_id is None:
        return None
    try:
        return await message.channel.fetch_message(reference.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def image_urls_in(message: discord.Message) -> list[str]:
    return [a.url for a in message.attachments if is_image(a)]


def strip_bot_mention(message: discord.Message) -> str:
    content = message.content
    for form in bot_mention_forms():
        content = content.replace(form, " ")
    return content.strip()


async def safe_reply(message: discord.Message, content: str) -> None:
    """The message can be deleted, or the channel locked down, between trigger
    and answer - neither is worth a traceback."""
    try:
        await message.reply(content)
    except discord.HTTPException as exc:
        log.warning("Couldn't reply in %s: %s", message.channel, exc)


FIND_SOURCE_TOOL = {
    "type": "function",
    "function": {
        "name": "find_image_source",
        "description": (
            "Reverse image search the image in this message to find where it came "
            "from. Call this when someone asks where an image is from, who made it, "
            "what anime or artist it is, or for its source or sauce. Returns the "
            "pages the image was found on."
        ),
        # No parameters on purpose: the bot searches the image it already has,
        # so the model can't point the search at a URL of its own choosing.
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def describe_matches(matches: list[dict], engine: str) -> str:
    lines = [f"Reverse image search results from {engine}:"]
    for match in matches[:MAX_SHOWN_MATCHES]:
        parts = [match["title"][:120]]
        if match.get("source"):
            parts.append(f"on {match['source']}")
        parts.append(match["link"])
        if match.get("similarity") is not None:
            parts.append(f"{match['similarity']:.0f}% similar")
        lines.append("- " + " | ".join(parts))
    lines.append(
        "Quote the first link in full in your reply. Don't invent anything not listed."
    )
    return "\n".join(lines)


class SourceFinder:
    """Runs the reverse search for the agent, once per message: SerpApi's free
    tier is 100 a month and the model decides when to spend one."""

    def __init__(self, image_url: str, who: str) -> None:
        self.image_url = image_url
        self.who = who
        self.top_link: str | None = None
        self._spent = False

    async def __call__(self, call: dict) -> str:
        name = (call.get("function") or {}).get("name")
        if name != "find_image_source":
            return f"No such tool: {name}."
        if self._spent:
            return "Already searched this image; use the results above."

        self._spent = True
        log.info("Agent called find_image_source for %s (asked by %s)", self.image_url, self.who)
        try:
            matches, engine, error = await lookup_source(self.image_url)
        except Exception:
            log.exception("find_image_source failed")
            return "The search failed. Tell the user it didn't work."

        if error:
            return f"The search couldn't run: {error}"
        if not matches:
            return "No source found for this image. Tell the user that plainly."

        self.top_link = matches[0]["link"]
        return describe_matches(matches, engine)


async def answer_mention(
    question: str,
    image_urls: list[str],
    history: list[dict] | None = None,
    who: str = "someone",
) -> tuple[str, bool]:
    """Returns (reply text, worth remembering). Errors are returned, not raised,
    but never enter the conversation history."""
    model = OPENROUTER_IMAGE_MODEL if image_urls else OPENROUTER_TEXT_MODEL
    # Only offered when there's actually an image to search.
    tools = [FIND_SOURCE_TOOL] if image_urls else []
    finder = SourceFinder(image_urls[0], who) if image_urls else None

    try:
        reply = await ask(
            bot.session,
            AGENT_CONTEXT,
            question,
            api_key=OPENROUTER_API_KEY,
            model=model,
            max_tokens=OPENROUTER_MAX_TOKENS,
            image_urls=image_urls,
            max_price=OPENROUTER_MAX_PRICE,
            max_context_tokens=MAX_CONTEXT_TOKENS,
            history=history or [],
            tools=tools,
            tool_runner=finder,
        )
    except ChatRateLimited as exc:
        log.warning("OpenRouter free-tier limit reached: %s", exc)
        return "I've used up my free questions for now. Try again later.", False
    except ChatAuthError:
        log.error("OpenRouter rejected the API key")
        return "My chat API key isn't working. Please tell the server admin.", False
    except ChatNoEndpoints as exc:
        log.error(
            "Nothing matched the routing constraints (%s). OPENROUTER_MAX_PRICE=%s may "
            "be too low - vision models cost well over $1/M - or the account's data "
            "policy at https://openrouter.ai/settings/privacy is too restrictive",
            exc,
            OPENROUTER_MAX_PRICE,
        )
        return "My chat backend isn't configured right. Please tell the server admin.", False
    except ChatUnavailable as exc:
        log.info("No free model available: %s", exc)
        return "No free model has capacity right now. Try again in a minute.", False
    except ChatRefused as exc:
        log.info("Model declined to answer: %s", exc)
        return "I'd rather not answer that one.", False
    except ChatEmptyReply as exc:
        log.warning("The model returned an empty reply (%s)", exc.detail)
        return "The model returned an empty reply \N{EM DASH} ask me again?", False
    except ChatError as exc:
        log.warning("Chat reply failed: %s", exc)
        return f"Couldn't answer that \N{EM DASH} {exc}.", False
    except Exception:
        log.exception("Unexpected error answering a mention")
        return "Something went wrong answering that.", False

    log.info(
        "Answered via %s, cost $%.6f (%d chars)%s",
        reply.model or "?",
        reply.cost,
        len(reply.text),
        f", tools: {', '.join(reply.tools_used)}" if reply.tools_used else "",
    )

    text = reply.text
    # The whole point of a source lookup is the link. Models paraphrase it away
    # ("it's from Wikipedia"), so put it back rather than hope the prompt holds.
    if finder and finder.top_link and finder.top_link not in text:
        text = f"{text}\n{finder.top_link}"
    return text[:DISCORD_MESSAGE_LIMIT], True


async def handle_mention(message: discord.Message) -> bool:
    """Returns True if this was ours to answer - an @mention, or a reply to one
    of our own messages."""
    if AGENT_CONTEXT is None or message.author.bot or message.mention_everyone:
        return False

    replied = await resolve_reference(message)
    replying_to_bot = (
        replied is not None and bot.user is not None and replied.author.id == bot.user.id
    )
    if not (mentions_bot(message) or replying_to_bot):
        return False

    who = describe_invocation(message.author, message.channel, message.guild)
    if not mention_throttle.allow(message.author.id):
        log.info("Throttled mention from %s", who)
        try:
            await message.add_reaction(RATE_LIMITED_EMOJI)
        except discord.HTTPException:
            pass
        return True

    question = strip_bot_mention(message)
    # "@bot what is this" as a reply means the image one message up, not this one.
    image_urls = image_urls_in(message)
    if replied is not None:
        image_urls += [u for u in image_urls_in(replied) if u not in image_urls]
    if not question and not image_urls:
        await safe_reply(message, NO_QUESTION_REPLY)
        return True
    if not question:
        question = DESCRIBE_IMAGE_QUESTION

    # Channels are shared, so the history has to say who said what.
    speaker = message.author.display_name
    remembered = f"{speaker}: {question}"
    history = conversations.history(message.channel.id)

    log.info(
        "Mention from %s (%d image(s), %d remembered): %s",
        who,
        len(image_urls),
        len(history),
        scrub(question),
    )
    async with message.channel.typing():
        answer, keep = await answer_mention(remembered, image_urls, history, who)

    if keep:
        conversations.remember(message.channel.id, "user", remembered)
        conversations.remember(message.channel.id, "assistant", answer)
    await safe_reply(message, answer)
    return True


@bot.event
async def on_message(message: discord.Message) -> None:
    if await handle_sauce_reply(message) or await handle_mention(message):
        return
    # Overriding on_message replaces the default, which is what normally
    # dispatches prefix commands - so do it here. Skipped above because
    # `when_mentioned` makes every handled mention an unknown command, which
    # discord.py logs as an ERROR with a traceback.
    await bot.process_commands(message)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id if bot.user else "?")


if __name__ == "__main__":
    # log_handler=None stops discord.py installing a second handler on top of
    # the basicConfig one above, which would double every log line.
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
