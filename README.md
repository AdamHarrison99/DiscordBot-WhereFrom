# WhereFrom

A Discord bot that finds the original source of an image using Google Lens
results (via [SerpApi](https://serpapi.com)).

## Usage

- Right-click a message with an image attachment -> **Apps** -> **Find Source**
- `/sauce url:<image_url>`
- `/sauce file:<attachment>`
- Reply to an image message with `?sauce` or `!sauce`

## Setup

1. Create a Discord application at https://discord.com/developers/applications,
   add a bot user, and copy the token.
2. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
3. Invite the bot with scopes `bot applications.commands` and permissions:
   Send Messages, Embed Links, Read Message History, Use Application Commands.
4. Get a SerpApi key at https://serpapi.com (free tier: 100 searches/month).
5. Copy `.env.example` to `.env` and fill in `DISCORD_BOT_TOKEN` and `SERPAPI_KEY`.

```
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
