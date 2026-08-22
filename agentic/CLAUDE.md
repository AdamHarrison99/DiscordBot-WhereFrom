# WhereFrom

A Discord bot that finds the original source of an image: Google Lens via SerpApi, with
SauceNAO as a fallback when Lens finds nothing.

`README.md` covers what the bot does, how to set it up and how to run it — read it rather
than duplicating it here. This file is what the README can't tell you.

## Layout

- Bot code sits at the repo root; agent-facing docs and checks live in `agentic/`.
- `agentic/memory/` holds the working conventions — read `memory/MEMORY.md` first.
- A `CLAUDE.md` in a subfolder doesn't load automatically, so this one is imported by a
  stub above the repo root. Keep the stub if you have one.
- GitHub: <https://github.com/AdamHarrison99/DiscordBot-WhereFrom> (public)
- Branch is **`master`**, not `main`.
- Git identity is set **locally in this repo only** — don't assume `--global` works.
- **Never `git push` without asking first.** Local commits are fine unprompted.
- **Never quote a real message — not in a commit message, a comment, a doc or a log
  excerpt. Paraphrase.** The subject matter here is other people's conversations, so an
  example showing "what actually happened" is someone's chat, and a commit message is
  published as loudly as the code. Say what the behaviour was, not what was said: "a
  follow-up resolved against the previous turn", never the turn itself. Fixtures use
  placeholder speakers (`alpha`, `bravo`) and `CHANNEL = 1` — prose gets the same
  treatment. Everything in the tree is published — see `memory/wherefrom-project.md`.

## Code map

| File | Role |
| --- | --- |
| `bot.py` | discord.py client: logging setup, all four entry points, embed building, search orchestration |
| `lens_search.py` | SerpApi Google Lens HTTP layer. No discord imports; testable standalone |
| `sauce_search.py` | SauceNAO HTTP layer. Same deal |
| `web_search.py` | SerpApi plain-Google HTTP layer, for the agent's `search_web` tool |
| `page_reader.py` | Fetches a URL and extracts text. Stdlib HTML parsing, no new deps |
| `chat_agent.py` | OpenRouter free-router chat for @-mentions, plus the per-user throttle |
| `startup.bat` / `startup.sh` | Run the bot, creating the venv on first run |

Both *image* search modules return the same normalised match dict — `title`, `link`,
`source`, `thumbnail`, `similarity` — which is the only contract `build_embed` depends on.
Keep any new engine to that shape. `web_search.py` is deliberately outside it: its results
are text for the model, never an embed.

`AgentTools` in `bot.py` is the single tool_runner handed to `ask()`. It owns which tools
are offered (`definitions`) and the per-message rationing; `SourceFinder` is one tool
behind it, not the runner itself.

## Hard-won facts (don't re-derive)

- **SerpApi `google_lens` requires a `type` param.** Omitting it fails. We send
  `type=visual_matches`, the tab that returns the `visual_matches[]` array the embed reads.
  Verified live: `type=all` returns only `ai_overview` — no matches at all — so don't
  "simplify" by dropping it.
- **Image URLs go to SerpApi as-is; there is no re-hosting step.** An earlier version
  pushed non-Discord URLs through Catbox, which broke perfectly good public URLs; Catbox
  has uploads disabled anyway (HTTP 412). Re-adding a re-host costs a second SerpApi search
  just to detect the need — expensive at 100/month. Removing it was deliberate.
- **Google Lens returns nothing for `upload.wikimedia.org` URLs.** Not a bug here — Lens
  silently yields zero matches for them, even for the Mona Lisa. Never use Wikimedia images
  as test fixtures. `https://www.python.org/static/img/python-logo.png` is known-good.
- **SerpApi signals "no results" via the `error` field**, not an empty array, so it has to
  be string-matched apart from real failures. That's what `LensNoResults` is for, and
  `WebNoResults` on the plain-Google side.
- **Agent web search is SerpApi's `engine=google`, not OpenRouter's web plugin.** The
  plugin (`plugins: [{"id": "web"}]`, or a `:online` model suffix) works and needs no new
  key, but Exa bills $0.007 a request on top of the inflated prompt — 10-70x a normal
  reply — and it fires on every call whether the question needed it or not. Going through
  the existing tool loop means the model only searches when it decides to, at the cost of
  SerpApi quota instead of dollars. That was a deliberate trade, not an oversight.
- **The web tool is rationed twice**: `AgentTools` allows one search per message however
  many times the model asks, and `DailyBudget` caps searches per calendar day
  (`WEB_SEARCH_DAILY_LIMIT`, default 10). The budget lives in memory, so a restart hands
  the day's allowance back — it protects the month's 100, it doesn't account for it.
- **The last tool round sends `tool_choice: "none"`.** Without it a model that spends its
  final round asking for another tool returns empty content and `ChatEmptyReply` — the
  user sees "the model returned an empty reply". Reproduced on 2026-08-16; it got likelier
  the moment there were three tools to choose from.
- **`FakeSession` in `check_chat.py` deep-copies each request body.** `ask()` mutates one
  dict across rounds, so recording the reference makes every request look like the last —
  which silently passed a check asserting per-round state.
- **Tool calls log as a three-line story at INFO**: the call with the model's own
  arguments verbatim, what happened (searched / read / refused, with the reason), then the
  reply length and elapsed time. Every guard goes through `AgentTools._refuse` so a
  refused call can't look like one that never happened. `LOG_LEVEL=DEBUG` adds each search
  result's title and link. Arguments are `scrub`bed — they're model-written, so a newline
  in one would otherwise forge a log line.
- **Everything a tool returns is untrusted input.** Anyone can post a link to a page they
  wrote, so `describe_page` and `describe_web_results` label their contents as quoted
  material and say outright that instructions inside it don't count.
- **Tool results are truncated at 4000 chars** (`MAX_TOOL_RESULT_CHARS`), from the tail.
  `describe_web_results` therefore puts its "don't invent anything" instruction in the
  *first* line; a trailing one gets cut off exactly when the results are longest.
- **`search_web` gets the model a search, not a page.** SerpApi returns titles, snippets
  and links; that's what `read_page` is for.
- **`read_page` follows redirects by hand.** aiohttp would follow a 302 into `10.0.0.1`
  or `169.254.169.254` without ever showing it to the address check, and the model takes
  the URL from a Discord message — so every hop is re-resolved and rejected unless
  `ipaddress.is_global`. Don't "simplify" it back to `allow_redirects=True`.
- **That address check is not rebinding-proof, deliberately.** aiohttp resolves the name
  again when it connects, so a host answering publicly to `ensure_public` and privately a
  moment later gets through. Closing it means resolving once and connecting to the IP by
  hand, which breaks TLS verification and virtual hosting — not worth it for a single-user
  deployment or an empty container. Revisit if this ever runs somewhere with a metadata
  service or internal hosts worth reaching.
- **The reverse image search still takes no URL from the model.** `read_page` can hand it
  one — a page's `og:image`, or an image link the user posted — but that's the bot's find,
  not the model's choice. `AgentTools._find_source` is where the two meet. An attachment
  always wins over a discovered image.
- **HTML extraction is stdlib `HTMLParser`**, not BeautifulSoup: nothing else in
  `requirements.txt` is a parser and a 3000-character extract doesn't justify one. It
  drops `script`/`style`/`nav`/`footer` and keeps `og:` tags. Malformed pages are caught
  and whatever parsed is kept.
- **Extraction starts at `<main>`/`<article>`/`role="main"` when the page marks one.**
  Verified against real HTML on 2026-08-16: without it, Wikipedia spent the first ~200
  characters on "Jump to content / 4 languages / Edit links" and Sphinx pages on their
  whole breadcrumb, so the budget ran out before the article started. Below
  `MIN_MAIN_CHARS` the marker is ignored — some pages tag a sidebar as `<article>`.
- **Reddit threads are readable without a key** — append `.json` to any thread URL. Cloud
  hosts often get 403 from Reddit anyway; that surfaces as `PageBlocked`, not a bug.
- **YouTube is not usable through `read_page`.** Comments load from a separate
  continuation endpoint and aren't in the HTML at all; likes sit behind a renderer path
  that moves; datacenter IPs get consent walls. The real answer is the YouTube Data API
  (free, 10k units/day, 1 unit a call) — deliberately not built: it needs a Google
  Cloud key, and taking on another provider was declined.
- **SauceNAO reports failures inside a 200 response** — negative `status`, `user_id: 0` for
  a bad key, negative `short_remaining`/`long_remaining` for quota. See `_check_header`.
- **`bot.py` reads env at import time** (`os.environ[...]`). Importing it without
  `DISCORD_BOT_TOKEN` / `SERPAPI_KEY` set raises `KeyError`. Set dummies to import in tests.
- **Logging goes through a `QueueListener`** so disk writes never block the event loop, and
  the formatters redact `api_key=`/`token=` params. Don't switch to `basicConfig` — it
  double-formats every record through the QueueHandler.
- **Mention replies cost money, deliberately.** `openrouter/auto` routes to paid models;
  ~$0.0001–0.0008 per reply, logged per call. This was a considered switch away from
  free-only, not drift — see `plans/MENTION_AGENT_PLAN.md`. Don't "restore" the free-only guards.
- **`openrouter/free` has no reachable vision endpoint.** Text works at zero cost, but any
  image request 404s with "No endpoints available matching your guardrail restrictions and
  data policy" — that's `ChatNoEndpoints`. Tested against explicit free vision models
  (`gemma-4-31b-it:free`, `nemotron-nano-12b-v2-vl:free`) too; all refused. Vision is the
  entire reason auto routing is on.
- **`max_price` is a filter, not a cap that degrades gracefully.** Too low and OpenRouter
  404s rather than picking something cheaper. **The completion price binds, not the
  prompt price:** vision routes to models at $1.62–$5.00/M completion, so
  `OPENROUTER_MAX_PRICE=1` kills images while text still works. 5 covers everything seen
  so far; tested 1 (vision fails), 5 and 10 (both fine). Conversely, `max_price: 0` is what
  made the *free* router work on an account with a zero credit limit — without it, even
  free requests 404'd.
- **Reasoning tokens are billed out of `max_tokens`.** A thinking model will spend the
  whole budget and return empty `content` with `reasoning` populated. Every request sends
  `reasoning: {"enabled": false}`; that's what fixed the intermittent empty replies.
- **Token counts are estimates at 4 chars/token, and can't be anything better.** Auto
  routing spans models with different tokenizers, so `MAX_CONTEXT_TOKENS` trimming is
  approximate by design. Enforced inside `build_request`, not at the call site, so no
  future caller can route around it.
- **OpenRouter fetches image URLs server-side, and some hosts refuse it.** `python.org`
  returns 403 to their fetcher — it looks like a vision failure but isn't. Discord CDN URLs
  are fine; `raw.githubusercontent.com` is a good test host.
- **`message.mentions` includes the replied-to author**, so a plain reply to one of the
  bot's own messages arrives looking like a mention. `mentions_bot()` checks the literal
  `<@id>` in the content instead — without that, every "thanks!" reply bought an API call.
- **`when_mentioned` makes every handled mention an unknown prefix command.** discord.py's
  default handler logs that at ERROR with a traceback, so `on_message` returns before
  `process_commands` once a mention is handled.

## Constraints on testing

- **SerpApi free tier is 100 searches/month**, now shared between Lens and the agent's
  `search_web`. Every live call burns quota permanently. Prefer offline tests
  against fixture payloads; ask before spending live calls. ~6 were spent on 2026-08-03
  debugging the request contract.
- **SauceNAO free tier is 100/day, 6 per 30s** — cheaper to exercise, but still shared.
- **OpenRouter calls bill per request** (~$0.0001–0.0008). Cheap, but check before running
  a loop. Offline checks cover the request body, error mapping and throttle without a key.
- **Checks live in `agentic/checks/`** — see its README. No pytest; plain scripts that
  print `N passed, M failed`. Run them from the repo root with the venv python and no
  arguments. Add to them rather than writing throwaway scripts elsewhere.
- Local venv at `.venv` (gitignored), discord.py 2.7.1.

## Conventions

- READMEs end with this AI disclosure line:
  `*This project was built with AI code development tools ([Claude Code](https://www.anthropic.com/claude-code)).*`
- **Code comments are short.** One line, why not what, only where the code can't say it
  itself. No comment beats a redundant one. Docstrings are one line unless the function
  genuinely needs more. Don't write documentation in the code — it goes here or in the
  README. `node agentic/tools/check-comments.mjs` flags the lines that break this.
- `.env.example` and `agent_context.example.md` are committed templates for gitignored
  files. Add new vars to the example with a comment explaining what each one buys you.
  `agent_context.md` is deliberately *not* a copy of its example — don't sync them.
- **Memories go in `agentic/memory/`**, never in a per-session store outside the repo —
  one file per fact, linked from `memory/MEMORY.md`. They're published like everything
  else here, so write the rule, not who asked for it or when.
- Keep this document lean. Information has to earn its place: no change logs, no reasoning
  narratives, no restating the README or the code.

## Status

**Search path verified live** (2026-08-03): `perform_search` returns a correctly formatted
embed against the real SerpApi. Error classification (quota / auth / no-results / bad URL)
is unit-tested offline. The SauceNAO fallback landed after that and has **not** been
verified live.

**@-mention chat is built and works live** (`chat_agent.py`, `plans/MENTION_AGENT_PLAN.md`).
394 offline checks (127 `chat_agent`, 163 `bot.py` wiring against faked messages, 36
`web_search`, 68 `page_reader`), plus both paths verified end-to-end against the real API
— text and an image correctly described, $0.0001–0.002 a reply. `agent_context.md` is read
at startup only — there is deliberately no reload command, so a personality edit needs a
restart.

**`search_web` and `read_page` are offline-verified only** (2026-08-16): request contract,
error mapping, redirect and address guards, rationing and the dispatcher are all covered
against fakes. The *extractor* has been run against real HTML (Wikipedia, BBC News, the
Python docs) and reads them cleanly. Still unproven: SerpApi's `engine=google` response
shape — `answer_box` / `knowledge_graph` come from the docs, not an observed payload — and
`fetch_page` end to end through aiohttp, since the real-HTML run went through `urllib` and
`read_html` directly. One live SerpApi call would settle the first.

**Not yet run against real Discord.** The token has never connected, so these remain
unproven: gateway login, `tree.sync()` slash-command registration, the context-menu entry
appearing under Apps, the `?sauce` reply trigger, and every mention path. Exercising them on
a test server is the next thing to do.

## Backlog

Unbuilt proposals are listed in `IDEAS.md`; the ones worth building have a plan in
`plans/`, which is where the design and its rejected alternatives live.

- Further engines (Yandex, TinEye) behind the same normalised match shape.
- Per-server API key configuration.
- Caching recent lookups so a repeat image doesn't spend quota.
- NSFW handling. `sauce_search.py` sends `hide=0`, disabling SauceNAO's own filtering, so
  booru sources aren't silently dropped.
