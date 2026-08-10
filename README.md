# WhereFrom

A Discord bot that finds the original source of an image using Google Lens
results (via [SerpApi](https://serpapi.com)).

## Usage

- Right-click a message with an image attachment -> **Apps** -> **Find Source**
- `/sauce url:<image_url>`
- `/sauce file:<attachment>`
- Reply to an image message with `?sauce` or `!sauce`
- `@WhereFrom <question>` — ask it something, optionally with an image attached
  (off unless `OPENROUTER_API_KEY` is set; see below)

## Setup

1. Create a Discord application at <https://discord.com/developers/applications>.
2. Open the **Bot** tab in the left sidebar and click **Reset Token** to get your
   `DISCORD_BOT_TOKEN`. This is *not* the Application ID or the Public Key from the
   General Information tab — the bot token is ~70 characters in three dot-separated
   parts. It is shown only once; reset it again if you lose it.
3. Still on the **Bot** tab, scroll down to **Privileged Gateway Intents** and enable
   **Message Content Intent**, then click **Save Changes** — the toggle reverts if you
   navigate away without saving. The bot fails to connect without this intent, since
   `bot.py` declares `intents.message_content = True`. (It is only needed for the
   `?sauce` / `!sauce` reply trigger; slash and right-click commands work without it.)
4. Invite the bot with scopes `bot applications.commands` and permissions:
   Send Messages, Embed Links, Read Message History, Use Application Commands.
5. Get a SerpApi key at <https://serpapi.com> (free tier: 100 searches/month).
6. Optional but recommended: get a free SauceNAO key at <https://saucenao.com>
   (register, then **account → api**) for the fallback described below.
7. Copy `.env.example` to `.env` (copy, don't rename — the example is the committed
   template) and fill in `DISCORD_BOT_TOKEN` and `SERPAPI_KEY`.

## How a lookup works

Google Lens (via SerpApi) answers first. When it finds nothing, the bot retries
through **SauceNAO**, which indexes Pixiv, Danbooru, yande.re, Twitter and
DeviantArt — the illustration and anime sources Lens routinely misses. The reply
embed names whichever engine found the match, and SauceNAO results also show a
similarity percentage.

The fallback only runs on a miss, so a Lens hit costs nothing extra. SauceNAO's
free tier allows 100 searches/day and 6 per 30 seconds, well clear of SerpApi's
100/month. Without `SAUCENAO_API_KEY` the bot just reports no match, as before.

Matches below `SAUCENAO_MIN_SIMILARITY` (default 60%) are discarded as noise.

## Asking it questions

@-mention the bot and it answers in-channel — short replies, and it can look at any image
attached to your message. It can't run a reverse image search from a mention; use the
commands above for that.

This is off until you set `OPENROUTER_API_KEY` (get one at
<https://openrouter.ai/keys>). Everything else has a working default.

**This costs money.** Requests go through `openrouter/auto`, which picks a model to suit
the prompt — paid ones included. Replies land around **$0.0001–$0.0008 each**, so a busy
day of a few hundred mentions is still well under a dollar. Each user is throttled to
`MENTION_RATE_LIMIT_PER_MINUTE` (default 4); over that the bot reacts with an hourglass
instead of replying. `OPENROUTER_MAX_PRICE` sets a price ceiling if you want a hard cap.

**To spend nothing**, set `OPENROUTER_MODEL=openrouter/free`. Text answers work fine, but
images don't — no free vision endpoint is reachable, so the bot reports a configuration
error on any mention with a picture attached. That's an OpenRouter availability limit, not
something this bot can route around.

Replies are capped at `OPENROUTER_MAX_TOKENS` (default 150), which comfortably fits the
three sentences the agent context asks for, with headroom for emoji and non-Latin scripts.
It's a backstop, not the rule — a token cap can't count sentences, so the length
instruction lives in `agent_context.md` where the model can follow it. If a reply does get
cut off mid-sentence, the unfinished fragment is dropped rather than posted.

Reasoning is switched off on every request. Reasoning tokens are billed out of that same
budget, and a thinking model will happily spend all of it and return nothing at all.

`MAX_CONTEXT_TOKENS` (default 2000) caps what gets *sent* — the agent context file plus the
question. Over that, the question is trimmed first and the context second, so the bot keeps
its instructions as long as possible. It's measured at roughly four characters per token,
because auto-routing spans models with different tokenizers and an exact count isn't
available. Attached images aren't counted. The startup log prints the context file's
estimated size, and warns if it alone fills the budget.

The bot's personality and rules live in `agent_context.md`, which is gitignored — copy
`agent_context.example.md` to it and edit. Admins can reload it without a restart with
`/reloadcontext`.

## Running it

Double-click **`startup.bat`** (Windows) or run **`./startup.sh`** (macOS/Linux/WSL).
Either one creates the virtualenv and installs dependencies on first run, then starts
the bot. They stop with a clear message if `.env` is missing.

### Log file

The bot writes to `wherefrom.log` next to `bot.py`, in addition to the terminal.
The file is capped: once it reaches `LOG_MAX_BYTES` (default ~1 MB) it is rotated
to `wherefrom.log.1`, the previous `.1` becomes `.2`, and so on up to
`LOG_BACKUP_COUNT` (default 3). The oldest is discarded, so disk use is bounded
at roughly 4 MB total.

Terminal and file deliberately differ:

| What | Terminal | Log file |
| --- | --- | --- |
| URLs | shortened for readability | **full**, including query strings |
| discord.py internals | shown | only WARNING and above |
| Bot activity | shown | shown |

Set `LOG_FILE=none` to turn the file off — worth doing on Railway/Render, where
the disk is wiped on restart and stdout is captured anyway. Set
`LOG_BACKUP_COUNT=0` to empty the file on rotation rather than keeping copies.
`LOG_MAX_BYTES` must be at least 1; `0` would disable rotation altogether and is
ignored in favour of the default.

Leaving any of these blank (or absent) uses the default — a bare `LOG_FILE=`
line does **not** disable file logging.

Log files are gitignored, but note the full Discord CDN URLs they contain include
signed access parameters for those attachments.

### Verbose logging

Pass `-v` (or `--verbose`) to either script to turn on debug logging and unmute
pip's output:

```bat
startup.bat -v
```

Equivalently, set `LOG_LEVEL` in `.env` or the environment — one of `DEBUG`,
`INFO` (default), `WARNING`, `ERROR`, `CRITICAL`. `DEBUG` logs every image URL
searched and how many matches came back, and also enables discord.py's own
gateway logging, which is very noisy. An unrecognised value falls back to `INFO`
with a warning.

Manually, if you prefer:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

## Deployment

Deploys as a background worker (no open ports needed) on Railway or Render
free tier. `Procfile` declares `worker: python bot.py`. Set
`DISCORD_BOT_TOKEN` and `SERPAPI_KEY` as environment variables in the
platform's dashboard.

---

*This project was built with AI code development tools ([Claude Code](https://www.anthropic.com/claude-code)).*
