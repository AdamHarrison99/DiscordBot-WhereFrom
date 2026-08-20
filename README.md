# WhereFrom

A Discord bot that finds where an image came from — Google Lens via
[SerpApi](https://serpapi.com), falling back to [SauceNAO](https://saucenao.com) for the
art and anime sources Lens misses. Add an [OpenRouter](https://openrouter.ai) key and it
also answers questions, reads links people post, and searches the web when it needs to.

## What it does

| | |
| --- | --- |
| Right-click a message → **Apps** → **Find Source** | search an image someone posted |
| `/sauce url:<image_url>` | search a link |
| `/sauce file:<attachment>` | search an upload |
| Reply `?sauce` or `!sauce` to an image | same, without the menus |
| `@WhereFrom <question>` | ask it anything — it can look at images, open links and search |
| `/forget` | clear this channel's conversation memory |

Everything after the fourth row needs `OPENROUTER_API_KEY`; without it, mentions are
ignored and the rest still works.

### When you @-mention it

You just ask — it works out which of these it needs, and does them itself:

| | |
| --- | --- |
| **Looks at images** | ones you attach, or the image in a message you replied to |
| **Finds a source** | runs the same Lens/SauceNAO search on its own, so "where's this from?" needs no command |
| **Searches the web** | news, prices, dates, scores — anything that's changed since the model was trained |
| **Reads links** | opens a page you post and answers from what's on it: articles, wikis, docs, Reddit threads |
| **Remembers** | the last few exchanges in the channel, so follow-ups make sense |

Reply to one of its messages and it keeps talking — no @ needed. Post an image *link* and
it goes to the reverse image search, as does the main picture on any page it reads, so
"where's the photo on this page from?" works.

## Quick start

You need Python 3.11+, a Discord bot token, and a SerpApi key. Both other keys are
optional. All three services have free tiers.

1. Create an app at <https://discord.com/developers/applications>, then on the **Bot**
   tab hit **Reset Token** — that's `DISCORD_BOT_TOKEN`, shown only once. It is *not* the
   Application ID or Public Key.
2. Same tab, enable **Message Content Intent** and **Save Changes**. The toggle silently
   reverts if you navigate away, and without it `?sauce` and @-mentions do nothing.
3. Invite the bot with scopes `bot applications.commands` and permissions: Send Messages,
   Embed Links, Read Message History, Use Application Commands.
4. Get a [SerpApi key](https://serpapi.com) (100 searches/month free). Optionally a
   [SauceNAO key](https://saucenao.com/user.php) for the fallback and an
   [OpenRouter key](https://openrouter.ai/keys) for chat.
5. Copy `.env.example` to `.env` — copy, don't rename — and fill in your keys.
6. Run **`startup.bat`** (Windows) or **`./startup.sh`** (macOS/Linux/WSL). Either builds
   the virtualenv on first run. Add `-v` for debug logging.

Starting it by hand instead:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

## Configuring it

Every setting lives in `.env.example`, already set to its defaults and commented — the
model, spending limits, how much of the SerpApi quota chat may spend, memory length,
logging. Delete any line to get its default back.

The bot's personality is `agent_context.md`: copy `agent_context.example.md` over it and
edit. It's read at startup, so restart to apply changes.

## Deploying it

A background worker with no open ports — `Procfile` declares `worker: python bot.py`.
Set the keys as environment variables in your platform's dashboard instead of shipping
a `.env`, and set `LOG_FILE=none` where the disk is wiped on restart.

`agentic/` is developer documentation: why the code is shaped the way it is, the API
quirks behind it, and offline check scripts that run without any keys.

---

*This project was built with AI code development tools ([Claude Code](https://www.anthropic.com/claude-code)).*
