# WhereFrom

A Discord bot that finds where an image came from, using Google Lens via
[SerpApi](https://serpapi.com) with [SauceNAO](https://saucenao.com) as a fallback.
It can also answer questions when you @-mention it.

## Usage

| | |
| --- | --- |
| Right-click a message → **Apps** → **Find Source** | search an image someone posted |
| `/sauce url:<image_url>` | search a link |
| `/sauce file:<attachment>` | search an upload |
| Reply `?sauce` or `!sauce` to an image | same, without the menus |
| `@WhereFrom <question>` | ask it anything; attach an image or reply to one |
| `/forget` | clear this channel's conversation memory |

Mentions are off unless `OPENROUTER_API_KEY` is set. Once on, you can also just reply to
the bot to keep talking — no @ needed — and ask it where an attached image came from
instead of using the commands above.

## Setup

1. Create an application at <https://discord.com/developers/applications>.
2. On the **Bot** tab, **Reset Token** for your `DISCORD_BOT_TOKEN`. This is *not* the
   Application ID or Public Key — the token is ~70 characters in three dot-separated
   parts, and is shown only once.
3. Still on **Bot**, enable **Message Content Intent** under Privileged Gateway Intents
   and **Save Changes** — the toggle silently reverts if you navigate away. Without it the
   `?sauce` reply trigger and @-mentions don't work.
4. Invite with scopes `bot applications.commands` and permissions: Send Messages, Embed
   Links, Read Message History, Use Application Commands.
5. Get a SerpApi key (free tier: 100 searches/month).
6. Optional: a free SauceNAO key (register, then **account → api**), and an
   [OpenRouter](https://openrouter.ai/keys) key if you want @-mention replies.
7. Copy `.env.example` to `.env` — copy, don't rename — and fill in the keys.

Every setting is documented in `.env.example`, which ships with working defaults.

## How a lookup works

Google Lens answers first. On a miss the bot retries through SauceNAO, which indexes
Pixiv, Danbooru, yande.re, Twitter and DeviantArt — the illustration and anime sources
Lens routinely misses. The reply names whichever engine found it, and SauceNAO results
carry a similarity score; anything below `SAUCENAO_MIN_SIMILARITY` (default 60%) is
discarded as noise.

The fallback only runs on a miss, so a hit costs nothing extra. Without
`SAUCENAO_API_KEY` the bot simply reports no match.

## Asking it questions

@-mention the bot and it replies in-channel. It sees images you attach or reply to, keeps
the last few exchanges per channel so follow-ups make sense, and can run a reverse image
search itself when you ask where something came from.

Pin `OPENROUTER_TEXT_MODEL` and `OPENROUTER_IMAGE_MODEL` to stop auto-routing picking a
different model every time. The image model needs vision *and* tool-calling support for
the built-in search to work.

The bot's personality lives in `agent_context.md`, which is gitignored — copy
`agent_context.example.md` to it and edit. It's read at startup, so restart to apply
changes.

## Running it

Double-click **`startup.bat`** (Windows) or run **`./startup.sh`** (macOS/Linux/WSL).
Either creates the virtualenv and installs dependencies on first run. Add `-v` for debug
logging. Manually:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

## Deployment

Runs as a background worker with no open ports. `Procfile` declares
`worker: python bot.py`. Set your keys as environment variables in the platform
dashboard rather than shipping a `.env`.

---

*This project was built with AI code development tools ([Claude Code](https://www.anthropic.com/claude-code)).*
