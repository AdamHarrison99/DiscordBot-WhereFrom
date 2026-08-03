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
    LensQuotaExceeded,
    LensSearchError,
    ensure_public_url,
    search_google_lens,
    top_matches,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wherefrom")

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
    try:
        public_url = await ensure_public_url(bot.session, image_url)
        payload = await search_google_lens(bot.session, public_url, SERPAPI_KEY)
    except LensQuotaExceeded:
        return None, "This bot has hit its SerpApi search quota for now. Please try again later."
    except LensSearchError as exc:
        return None, f"Couldn't search for that image: {exc}"
    except Exception:
        log.exception("Unexpected error during Google Lens search")
        return None, "Something went wrong while searching for that image."

    embed = build_embed(payload)
    if embed is None:
        return None, "No reliable source found for this image."
    return embed, None


async def reply_with_result(send, image_url: str) -> None:
    embed, error = await perform_search(image_url)
    await send(content=error, embed=embed)


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
    bot.run(DISCORD_BOT_TOKEN)
