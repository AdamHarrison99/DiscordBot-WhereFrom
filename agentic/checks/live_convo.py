"""Live multi-speaker conversation through bot.answer_mention + the real memory."""
import asyncio, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
repo = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo))

import aiohttp
import bot

CHANNEL = 1
IMG = "https://raw.githubusercontent.com/github/explore/main/topics/python/python.png"

TURNS = [
    ("alpha", "hey, what do you do?", []),
    ("alpha", "what's this?", [IMG]),
    ("bravo", "what did alpha just show you?", []),
    ("bravo", "and who asked you the very first question?", []),
]


async def main():
    bot.bot.session = aiohttp.ClientSession()
    try:
        for speaker, text, images in TURNS:
            remembered = f"{speaker}: {text}"
            history = bot.conversations.history(CHANNEL)
            answer, keep = await bot.answer_mention(remembered, images, history)
            if keep:
                bot.conversations.remember(CHANNEL, "user", remembered)
                bot.conversations.remember(CHANNEL, "assistant", answer)
            print(f"\n[{len(history)} in history] {speaker}: {text}"
                  + (" (+image)" if images else ""))
            print(f"  -> {answer}")
    finally:
        await bot.bot.session.close()

asyncio.run(main())
