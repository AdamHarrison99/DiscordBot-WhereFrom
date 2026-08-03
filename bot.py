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
    search_google_lens,
    top_matches,
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


class ConsoleFormatter(logging.Formatter):
    """File format, but with URLs shortened for readability."""

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

    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
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

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
SAUCE_TRIGGERS = ("?sauce", "!sauce")

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


bot = WhereFromBot(command_prefix=commands.when_mentioned, intents=intents)


def is_image(attachment: discord.Attachment) -> bool:
    """Discord doesn't always set content_type, so fall back to the extension."""
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def find_image_attachment(message: discord.Message) -> discord.Attachment | None:
    return next((a for a in message.attachments if is_image(a)), None)


def build_embed(payload: dict) -> discord.Embed | None:
    matches = top_matches(payload)
    if not matches:
        return None

    top = matches[0]
    embed = discord.Embed(
        title=(top.get("title") or "Untitled match")[:100],
        url=top.get("link"),
        color=discord.Color.blurple(),
    )
    thumbnail = top.get("thumbnail") or top.get("image")
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if top.get("source"):
        embed.add_field(name="Source site", value=top["source"], inline=True)

    extra = matches[1:4]
    if extra:
        lines = []
        for match in extra:
            title = (match.get("title") or "Untitled")[:80]
            link = match.get("link")
            lines.append(f"[{title}]({link})" if link else title)
        embed.add_field(name="Other matches", value="\n".join(lines), inline=False)

    embed.set_footer(text="Source: Google Lens via SerpApi")
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


async def perform_search(image_url: str) -> tuple[discord.Embed | None, str | None]:
    """Returns (embed, error_message) - exactly one of the two is set."""
    log.debug("Searching Google Lens for %s", image_url)
    try:
        payload = await search_google_lens(bot.session, image_url, SERPAPI_KEY)
    except LensBadImageUrl as exc:
        log.info("Rejected image URL %s: %s", scrub(image_url), exc)
        return None, f"Couldn't search for that image \N{EM DASH} {exc}."
    except LensNoResults:
        log.info("No matches found for %s", image_url)
        return None, NO_MATCH_MESSAGE
    except LensQuotaExceeded:
        log.warning("SerpApi quota exhausted")
        return None, "This bot has hit its SerpApi search quota for now. Please try again later."
    except LensAuthError:
        log.error("SerpApi rejected the API key")
        return None, "This bot's search API key isn't working. Please tell the server admin."
    except LensSearchError as exc:
        log.warning("Lens search failed: %s", exc)
        return None, f"Couldn't search for that image \N{EM DASH} {exc}."
    except Exception:
        log.exception("Unexpected error during Google Lens search")
        return None, "Something went wrong while searching for that image."

    matches = payload.get("visual_matches") or []
    embed = build_embed(payload)
    if embed is None:
        log.info("No matches found for %s", image_url)
        return None, NO_MATCH_MESSAGE

    shown = top_matches(payload)
    log.info("Found %d match(es) for %s", len(matches), image_url)
    for i, match in enumerate(shown, start=1):
        log.info(
            "  %d. [%s] %s - %s",
            i,
            match.get("source") or "?",
            (match.get("title") or "Untitled")[:80],
            match.get("link") or "(no link)",
        )
    # The rest never reach the embed, but are worth having when working out
    # why the chosen link isn't the real source.
    for i, match in enumerate(matches[len(shown):], start=len(shown) + 1):
        log.debug("  %d. [%s] %s", i, match.get("source") or "?", match.get("link") or "(no link)")

    return embed, None


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


async def handle_sauce_reply(message: discord.Message) -> None:
    """Run a lookup when someone replies to an image with ?sauce / !sauce."""
    if message.author.bot or not message.reference:
        return

    content = message.content.strip().lower()
    if content not in SAUCE_TRIGGERS:
        return

    who = describe_invocation(message.author, message.channel, message.guild)

    # Forwarded messages also populate `reference`, but without a message_id.
    reference_id = message.reference.message_id
    if reference_id is None:
        log.info("'%s' by %s - reference has no message id (a forward?)", content, who)
        return

    try:
        replied = await message.channel.fetch_message(reference_id)
    except discord.NotFound:
        log.info("'%s' by %s - replied-to message no longer exists", content, who)
        return
    except discord.Forbidden:
        log.warning("'%s' by %s - missing permission to read that message", content, who)
        await message.reply("I don't have permission to read that message.")
        return
    except discord.HTTPException as exc:
        log.warning("'%s' by %s - couldn't fetch that message: %s", content, who, exc)
        await message.reply("Couldn't read that message just now. Try again in a moment.")
        return

    attachment = find_image_attachment(replied)
    if attachment is None:
        log.info("'%s' by %s - replied-to message had no image attachment", content, who)
        await message.reply("That message doesn't have an image attachment.")
        return

    log.info("'%s' by %s on %s", content, who, attachment.url)
    async with message.channel.typing():
        embed, error = await perform_search(attachment.url)
    await message.reply(content=error, embed=embed)


@bot.event
async def on_message(message: discord.Message) -> None:
    await handle_sauce_reply(message)
    # Overriding on_message replaces the default, which is what normally
    # dispatches prefix commands - so do it here.
    await bot.process_commands(message)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id if bot.user else "?")


if __name__ == "__main__":
    # log_handler=None stops discord.py installing a second handler on top of
    # the basicConfig one above, which would double every log line.
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
