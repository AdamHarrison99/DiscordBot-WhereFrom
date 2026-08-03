# Handoff: Discord Reverse Image Search Bot (Google Lens)

## 1. Purpose
A Discord bot that finds the original source of an image posted in a server, using Google Lens results (via SerpApi), and replies in-channel with the top match and a direct link to the source.

## 2. Core User Flow
1. A user posts an image in a channel, or right-clicks an existing message with an image attachment and selects the bot's "Find Source" context menu command.
2. The bot uploads/references the image, queries Google Lens via SerpApi.
3. The bot replies (as a threaded reply or embed) with:
   - The top match's title
   - A clickable link to the source page
   - A thumbnail preview of the matched image
   - Optionally, 2-3 additional match candidates below the top one

## 3. Tech Stack
- **Language:** Python 3.11+
- **Discord library:** `discord.py` (v2.x, supports slash commands + message context menu commands)
- **Reverse image search:** SerpApi's Google Lens engine (`engine=google_lens`)
- **Image hosting for the API call:** SerpApi's `google_lens` engine requires a public image URL. Since Discord attachment URLs are already public (CDN-hosted), pass the Discord attachment URL directly — no separate image host (e.g. Catbox) needed in the common case. Fallback: if the image comes from a non-Discord source with a private/expiring URL, re-upload to a throwaway host (Catbox.moe API, no auth required) before querying.
- **Hosting target:** Railway or Render free/hobby tier (no VPS, no port forwarding — bot connects outbound only via Discord's gateway WebSocket)
- **Secrets:** Discord bot token, SerpApi API key — both via environment variables, never hardcoded

## 4. Discord Bot Setup (prerequisite, done by whoever deploys it)
1. Create an application at https://discord.com/developers/applications
2. Add a Bot user, copy the token
3. Enable "Message Content Intent" under Privileged Gateway Intents (needed to read image attachments in regular messages)
4. Generate an invite URL with scopes `bot applications.commands` and permissions: Send Messages, Embed Links, Read Message History, Use Application Commands
5. Get a SerpApi key at https://serpapi.com (free tier: 100 searches/month)

## 5. Commands / Interactions
- **Message context menu command:** "Find Source" — right-click any message containing an image attachment
- **Slash command:** `/sauce url:<image_url>` — for pasting a direct image link
- **Slash command:** `/sauce file:<attachment>` — for uploading an image directly
- Optional: plain-text trigger — if a user replies to an image message with `?sauce` or `!sauce`, run the same lookup (nice-to-have, not required for v1)

## 6. Response Format
Discord embed containing:
- Title: top match's page title (truncate to ~100 chars)
- URL: the top match's source link (this is the clickable embed title link)
- Thumbnail: the matched image, if SerpApi returns one
- Footer: "Source: Google Lens via SerpApi"
- If no confident match found: a plain message like "No reliable source found for this image."

## 7. Error Handling Requirements
- SerpApi rate limit / quota exceeded → friendly message, don't crash
- Image URL unreachable or not an image → friendly message
- No results returned → friendly "no source found" message, not a silent failure
- Log errors to console/stdout (Railway/Render capture this automatically) — no need for a separate logging service in v1

## 8. Environment Variables
```
DISCORD_BOT_TOKEN=
SERPAPI_KEY=
```

## 9. Deployment
- Target: Railway or Render (free tier)
- `requirements.txt`: `discord.py`, `aiohttp`, `python-dotenv` (for local dev), plus whatever HTTP client is used to call SerpApi (`requests` or `aiohttp`)
- Entry point: `bot.py`, runs `bot.run(os.environ["DISCORD_BOT_TOKEN"])`
- No Dockerfile strictly required — Railway/Render can auto-detect a Python app from `requirements.txt` and a `Procfile` (`worker: python bot.py`)
- No open ports needed — this is a background worker process, not a web service

## 10. Out of Scope for v1 (nice-to-haves for later)
- Multiple search engines (Yandex, TinEye) as fallback if Google Lens has no result
- Per-server SerpApi key configuration (like SauceBot's `/config api_key`)
- Caching recent lookups to avoid repeat API calls on the same image
- NSFW-specific handling/filtering

## 11. Acceptance Criteria
- [ ] Right-clicking an image message and selecting "Find Source" returns a reply within ~5 seconds
- [ ] `/sauce url:` and `/sauce file:` both work
- [ ] Bot handles a non-image URL gracefully
- [ ] Bot handles SerpApi quota exhaustion gracefully
- [ ] Bot runs continuously on Railway/Render free tier without crashing on restart (i.e., no local file state that gets wiped)
