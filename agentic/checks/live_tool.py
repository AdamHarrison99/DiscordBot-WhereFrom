"""Live: does the agent call find_image_source itself and name the source?

Stubs lookup_source: OpenRouter tokens, no SerpApi quota. --real spends one.
"""
import asyncio, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
repo = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo))

import aiohttp
import bot

REAL = "--real" in sys.argv
IMG = "https://raw.githubusercontent.com/github/explore/main/topics/python/python.png"

if not REAL:
    async def stub(url):
        print(f"    [stubbed search for {url[:60]}...]")
        return ([{"title": "Python logo on Wikipedia", "link": "https://en.wikipedia.org/wiki/Python",
                  "source": "en.wikipedia.org", "thumbnail": None, "similarity": 97.0}],
                "Google Lens via SerpApi", None)
    bot.lookup_source = stub

CHANNEL = 77
TURNS = [
    ("alpha", "what's in this picture?", [IMG]),   # describe: should NOT search
    ("alpha", "where did it come from?", [IMG]),   # origin: SHOULD search
    ("bravo", "what link did you just give alpha?", []),
]


async def main():
    bot.bot.session = aiohttp.ClientSession()
    try:
        for speaker, text, images in TURNS:
            remembered = f"{speaker}: {text}"
            history = bot.conversations.history(CHANNEL)
            answer, keep = await bot.answer_mention(remembered, images, history, speaker)
            if keep:
                bot.conversations.remember(CHANNEL, "user", remembered)
                bot.conversations.remember(CHANNEL, "assistant", answer)
            print(f"\n{speaker}: {text}" + (" (+image)" if images else ""))
            print(f"  -> {answer}")
    finally:
        await bot.bot.session.close()

asyncio.run(main())
