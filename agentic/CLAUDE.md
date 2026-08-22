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
| `chat_agent.py` | OpenRouter chat for @-mentions, the per-user throttle, and `ask_once` |
| `ambient.py` | Deciding whether to speak unprompted: buffer, local gate, judge. No discord imports |
| `startup.bat` / `startup.sh` | Run the bot, creating the venv on first run |

Both *image* search modules return the same normalised match dict — `title`, `link`,
`source`, `thumbnail`, `similarity` — which is the only contract `build_embed` depends on.
Keep any new engine to that shape. `web_search.py` is deliberately outside it: its results
are text for the model, never an embed.

`ambient.py` holds the decision, `bot.py` holds the discord: `to_record` is the only
boundary between them, and the reply goes through `ask()` with no tools rather than through
`answer_mention`. `describe_chat_failure` is shared by both reply paths — the mention posts
what it returns, ambient only logs it.

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
- **Tool calls log as a three-line story at INFO**: the call by name with its argument
  *length*, what happened (searched / read / refused, with the reason), then the reply
  length and elapsed time. Every guard goes through `AgentTools._refuse` so a refused call
  can't look like one that never happened. `LOG_LEVEL=DEBUG` adds each search result's
  title and link — upstream text, not anyone's. **No search query reaches the log**, in
  `AgentTools` or in `lookup_web`, on the success path or the empty one: the model writes
  a query out of what someone asked, so logging it puts that person's question on disk by
  another route. Only its length is recorded. URLs are the deliberate exception — a link
  names a public page rather than quoting anybody, and which hosts refuse us is most of
  what `read_page` debugging is.
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
- **Nothing anyone says reaches disk.** The ambient buffer and the conversation memory are
  in memory only, and no log line carries message text — mentions log a character count,
  ambient logs the score, the guard that fired and the bot's own words. The judge prompt
  tells the gate to describe rather than quote, because its reason *is* logged. Don't add
  a DEBUG transcript dump: the log would become a file of other people's conversation.
- **Ambient reads text and images, nothing else.** `to_record` collects image URLs and sets
  `other_files` for everything else; the transcript marks those so the model can say it
  can't open them, and a message with no text and no image never buys a gate call
  (`is_readable`). Video, audio and documents are never fetched.
- **The reply prompt says outright that nobody asked.** Without `AMBIENT_CONTEXT_NOTE` the
  model reads the transcript as a question put to it and answers like a summoned assistant
  — greeting the channel, summarising, offering more help. The persona alone doesn't fix it.
- **Every ambient refusal logs at INFO except one.** A quiet channel with no log line
  reads as a broken feature, so the eligibility refusals, the in-flight skip and the
  `AmbientLimits` reasons are all INFO. Only "not an enabled channel" stays DEBUG — it
  fires on every message in every channel of every guild. The startup line names the
  enabled channel ids for the same reason: a count can't be checked against a config.
- **The judge scores against `AMBIENT_SELF_SUMMARY`, not `agent_context.md`.** The gate
  deliberately never sees the persona, so that one sentence is the whole self-description
  it reasons from — and a narrow one costs replies. A summary naming only image lookups had
  the gate scoring a message about the bot at 0, giving "no valid image question" as its
  reason. It is the highest-leverage calibration knob there is, ahead of the threshold.
- **The verdict format puts `SCORE` first.** `GATE_MAX_TOKENS` is 40, so a model that
  overruns on `REASON` loses whatever comes after it. Score first means a truncated reply
  costs the explanation; reason first means it costs the decision, and `parse_verdict`
  fails closed to 0 — silence that looks exactly like a considered 0.
- **The gate is text-only and the reply is not.** `openrouter/free` works for the judge at
  zero cost precisely because no image is sent; the no-free-vision finding above still
  binds the reply, which is why that side uses auto routing.
- **`silent=True` and `mention_author=False` on every ambient post.** The client sets
  `replied_user=True`, so a reference without the override pings someone who never asked.
  A reply to the newest message is visual noise, so that case degrades to a plain `send` —
  which then carries an `<@id>` prefix from `addressee`, since without a reply header
  nothing else says who the bot is talking to. `silent=True` keeps that from buzzing anyone.
- **Ambient failures are silent, deliberately.** A user who @-mentions the bot deserves "my
  API key isn't working"; a channel that never asked deserves nothing. Both classify
  through `describe_chat_failure`; only the mention path posts the string.
- **A message arriving mid-evaluation doesn't cancel it.** `ambient_running` holds the
  channels with a judge or a reply in flight; `consider_ambient` schedules nothing for
  those. Cancelling instead would abort a running HTTP request on every new message and
  leave `AMBIENT_STALE_MESSAGES` deciding nothing. A mention interrupts by timestamp
  (`ambient_interrupted`), read just before posting, for the same reason.
- **Observe mode costs the same as running live.** It pays for the judge *and* the reply,
  and consumes the hourly slot, so the logged cadence matches what a live run would do.
  That's the point — you're reading what it would have said.
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
- **Every model setting is a fallback chain.** `env_models` parses a comma-separated
  list and appends `FREE_ROUTER_MODEL` last, so the final attempt costs nothing;
  `model_field` then sends `model` for one id and `models` for several. Verified live
  2026-08-22: a `models` array whose first entry is rate-limited is served by the second.
  Three limits — **the array takes at most 3 ids** (`MAX_FALLBACK_MODELS`; a fourth is a
  hard 400, and the appended free router counts toward it, so `env_models` truncates and
  says which ids it dropped), **an invalid model id is a hard 400, not a skipped entry**,
  and `route: "fallback"` is neither needed nor helpful (it 429'd where the bare array
  succeeded).
  **`OPENROUTER_IMAGE_MODEL` is the exception** (`free_last=False`): a router with no vision
  endpoint can only turn a busy paid model into `ChatNoEndpoints`, which reads to the user
  as "the admin misconfigured me". It is dropped from that chain even when inherited from
  `OPENROUTER_MODEL` — unless it is all that chain has, since nothing can serve an empty one.
- **A leading `~` is OpenRouter's "latest" alias and is part of the id.**
  `~deepseek/deepseek-v4-flash-latest` resolves to a dated build; stripping the tilde
  gives "is not a valid model ID". Don't "clean" it out of a config value.
- **An account privacy policy shrinks the free pool to almost nothing.** With
  non-zero-retention providers disallowed, `openrouter/free` had exactly one endpoint
  permitted out of 18, and it was rate-limited on 5 of 5 attempts. `max_price: 0` then
  fails outright with `ChatNoEndpoints` naming the data policy — so the older
  "`max_price: 0` rescues the free router" finding only holds without such a policy.
  `/api/v1/models/user` returns the account-filtered catalogue and is the way to check.
- **The image model needs tool calling as well as vision.** `google/gemini-2.5-flash-image`
  has vision and no `tools`, so it would take the mention path and silently lose
  `find_image_source`. Check `supported_parameters` before pinning one.
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
- **Every check script ends in `sys.exit()`, so anything appended after it never runs.**
  It exits 0 while the new checks sit there dead, and the total moves by exactly nothing.
  Add checks above the summary print.
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
- **Auditing before a commit includes a PII strip.** Read the tracked tree — not only the
  diff — for anything that identifies a person or a machine: names, emails, handles, server
  or channel ids, absolute paths from a developer's disk, API keys, and quoted messages.
  Commit messages count; so do comments, docstrings, fixtures and example files. Do it by
  reading and reasoning, not with a word list — a blacklist passes the thing it hasn't seen.
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
563 offline checks (132 `chat_agent`, 173 `bot.py` wiring against faked messages, 36
`web_search`, 68 `page_reader`, 154 `ambient`), plus both paths verified end-to-end
against the real API — text and an image correctly described, $0.0001–0.002 a reply. `agent_context.md` is read
at startup only — there is deliberately no reload command, so a personality edit needs a
restart.

**Ambient replies are built and offline-verified only** (2026-08-22): `ambient.py` plus its
`bot.py` wiring, 154 checks covering the buffer, every named refusal, the debounce and its
deadline, verdict parsing, reply shaping and both mid-flight aborts. Never run against real
Discord or a real gate model, so the judge's calibration is guesswork until observe mode
has run for a few days — `AMBIENT_THRESHOLD=70` is a starting point, not a measured one.

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
