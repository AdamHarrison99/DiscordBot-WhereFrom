# WhereFrom

A Discord bot that finds the original source of an image using Google Lens
results (via [SerpApi](https://serpapi.com)).

## Usage

- Right-click a message with an image attachment -> **Apps** -> **Find Source**
- `/sauce url:<image_url>`
- `/sauce file:<attachment>`
- Reply to an image message with `?sauce` or `!sauce`

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
6. Copy `.env.example` to `.env` (copy, don't rename — the example is the committed
   template) and fill in `DISCORD_BOT_TOKEN` and `SERPAPI_KEY`.

## Running it

Double-click **`startup.bat`** (Windows) or run **`./startup.sh`** (macOS/Linux/WSL).
Either one creates the virtualenv and installs dependencies on first run, then starts
the bot. They stop with a clear message if `.env` is missing.

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
