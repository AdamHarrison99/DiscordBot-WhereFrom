"""WhereFrom - a Discord bot that finds the original source of an image
using Google Lens results via SerpApi."""

from __future__ import annotations

import logging
import os

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

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_requested_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
LOG_LEVEL = _requested_level if _requested_level in VALID_LOG_LEVELS else "INFO"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("wherefrom")

if _requested_level != LOG_LEVEL:
    log.warning(
        "Ignoring unrecognised LOG_LEVEL=%r; using INFO. Valid values: %s",
        _requested_level,
        ", ".join(VALID_LOG_LEVELS),
    )

# discord.py is extremely chatty at DEBUG (it logs every gateway payload), so
# only let it follow LOG_LEVEL down to DEBUG when that was asked for explicitly.
if LOG_LEVEL == "DEBUG":
    logging.getLogger("discord").setLevel(logging.DEBUG)
else:
    logging.getLogger("discord").setLevel(logging.INFO)

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
SERPAPI_KEY = os.environ["SERPAPI_KEY"]

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

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


def find_image_attachment(message: discord.Message) -> discord.Attachment | None:
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return attachment
        if attachment.filename.lower().endswith(IMAGE_EXTENSIONS):
            return attachment
    return None


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


async def perform_search(image_url: str) -> tuple[discord.Embed | None, str | None]:
    """Returns (embed, error_message) - exactly one of the two is set."""
    log.debug("Searching Google Lens for %s", image_url)
    try:
        payload = await search_google_lens(bot.session, image_url, SERPAPI_KEY)
    except LensBadImageUrl as exc:
        log.debug("Rejected image URL %r: %s", image_url, exc)
        return None, f"Couldn't search for that image \N{EM DASH} {exc}."
    except LensNoResults:
        log.debug("No visual matches for %s", image_url)
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

    match_count = len(payload.get("visual_matches") or [])
    log.debug("Google Lens returned %d visual matches for %s", match_count, image_url)

    embed = build_embed(payload)
    if embed is None:
        return None, NO_MATCH_MESSAGE
    return embed, None


@bot.tree.context_menu(name="Find Source")
async def find_source_context_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    attachment = find_image_attachment(message)
    if attachment is None:
        await interaction.response.send_message("That message doesn't have an image attachment.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    embed, error = await perform_search(attachment.url)
    await interaction.followup.send(content=error, embed=embed)


sauce_group = app_commands.Group(name="sauce", description="Find the source of an image")


@sauce_group.command(name="url", description="Find the source of an image by URL")
@app_commands.describe(url="Direct link to an image")
async def sauce_url(interaction: discord.Interaction, url: str) -> None:
    await interaction.response.defer(thinking=True)
    embed, error = await perform_search(url)
    await interaction.followup.send(content=error, embed=embed)


@sauce_group.command(name="file", description="Find the source of an uploaded image")
@app_commands.describe(file="Image file to search for")
async def sauce_file(interaction: discord.Interaction, file: discord.Attachment) -> None:
    if not (file.content_type or "").startswith("image/"):
        await interaction.response.send_message("That file doesn't look like an image.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    embed, error = await perform_search(file.url)
    await interaction.followup.send(content=error, embed=embed)


bot.tree.add_command(sauce_group)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.reference:
        return

    content = message.content.strip().lower()
    if content not in ("?sauce", "!sauce"):
        return

    try:
        replied = await message.channel.fetch_message(message.reference.message_id)
    except discord.NotFound:
        return

    attachment = find_image_attachment(replied)
    if attachment is None:
        await message.reply("That message doesn't have an image attachment.")
        return

    async with message.channel.typing():
        embed, error = await perform_search(attachment.url)
    await message.reply(content=error, embed=embed)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id if bot.user else "?")


if __name__ == "__main__":
    # log_handler=None stops discord.py installing a second handler on top of
    # the basicConfig one above, which would double every log line.
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
