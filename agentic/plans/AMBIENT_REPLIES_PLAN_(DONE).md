# Plan: ambient replies — speak without being @-mentioned

**Status: BUILT** (2026-08-22), offline-verified only — never run against real Discord or a
real gate model. `ambient.py`, its `bot.py` wiring and `agentic/checks/check_ambient.py`.
Where the build departed from this document, the last section says so; everything else
landed as written.

## Goal

The bot joins a conversation on its own when it has something to add — it's being talked
about, it can supply context nobody has, a joke lands. It must not always respond, must
not respond emptily, and must feel like a participant rather than a tripwire.

Two properties the design has to hold, in this order:

1. **Silence is the default and the failure mode.** Every unparseable response, every
   error, every ambiguity resolves to "say nothing". A bot that misses a chance to be
   funny costs nothing; a bot that interjects wrongly gets muted.
2. **Cost is bounded structurally, not by intent.** Ceilings that no model score can
   override, the way `AgentTools` rations `search_web` today.

## Decisions taken

Settled before the build, recorded so they aren't relitigated:

- **No tools.** Ambient replies get no `search_web`, no `read_page`, no
  `find_image_source`. Nobody asked the bot to spend SerpApi quota, and an unprompted
  search is the kind of cost that accumulates invisibly.
- **Vision stays.** If the burst carries an image, the reply routes to
  `OPENROUTER_IMAGE_MODEL` so the bot can actually see what people are talking about.
  Text-only would make it blind in exactly the conversations it exists for.
- **One private server.** The opt-in is a channel ID list in `.env`; no per-guild store,
  no admin command. Defaults stay conservative anyway, since the repo is public.
- **Shared quotas, not parallel ones.** No second budget system. See Budgets below —
  this decision has less bite than it looks once tools are off, and the gap is flagged.

## Why this isn't just "call the model on every message"

Buffering is free; calling is what costs. They're separate decisions and the whole design
lives between them.

`on_message` already receives **every message in every visible channel** —
`intents.message_content = True` is set and the handler fires unconditionally, it just
returns early. A rolling per-channel buffer costs a `deque`. So the question is never
"do we send history every time"; it's **what decides the buffer is worth a model call**.

## Why a name match isn't the trigger

Matching the bot's name is good precision and terrible recall. It catches "wherefrom is
being weird today" and misses every moment that actually makes a bot feel present:

> "wait what was that image from" · "does anyone know if that's real" · "ask it" ·
> "it said the opposite yesterday"

The false-positive direction is worse than it looks: "where's that from?" said to another
human, answered by a bot, is the annoying-bot failure in one line.

So the name match is kept but **demoted**. A hit doesn't mean "reply" — it means "definitely
ask the judge", bypassing the debounce wait since being addressed deserves a faster answer
than being overheard. Everything else reaches the judge through the ordinary path.

## The ladder

Five layers, each cheaper than the next, each discarding most of the traffic.

| Layer | Cost | Job |
| --- | --- | --- |
| 0. Buffer | free | Every message into a per-channel deque, bot's own replies included |
| 1. Local gate | free | Opted-in channel? Cooldown elapsed? Under the ceilings? Has a human spoken since my last ambient reply? |
| 2. Debounce | free | Wait for a quiet gap, then judge the *burst* as one unit |
| 3. Judge | ~$0.00003 | Read the last ~12 messages, return a 0–100 score, a target and a reason |
| 4. Reply | $0.0001–0.0008 | Tool-free `ask()`, image model when the burst has a picture |

Layers 0–2 are pure Python with no network, which is most of the code and all of the
behaviour that makes this feel natural. They're testable offline the way `check_bot.py`
already fakes discord objects.

## Group reply ordering

The part that separates "a bot that talks" from "a bot that converses". Four distinct
problems, none of which the mention path has to solve today because a mention is a single
message with an unambiguous author and an unambiguous moment.

### Burst grouping — debounce with a deadline

People type in bursts. Judging message-by-message interrupts mid-thought; judging on the
pause reads the room. Per channel, keep one `asyncio.Task` that sleeps
`AMBIENT_DEBOUNCE_SECONDS`; each new message cancels and reschedules it.

Naive debounce starves: a channel that never goes quiet never fires. So the scheduler also
carries a deadline — the first message of a burst stamps `burst_started`, and the task
fires regardless once `AMBIENT_MAX_WAIT_SECONDS` has elapsed. Trailing debounce with a
max-wait ceiling, not plain trailing debounce.

One in-flight evaluation per channel, enforced by holding a single task handle per channel
id. Two overlapping bursts double-posting is the obvious failure and it would happen on
the first busy evening.

### Which message the reply attaches to

discord.py 2.7.1's `send()` takes `reference`, `mention_author` and — the useful one here
— `silent`. Three shapes, and the choice is the difference between joining and barging in:

| Shape | Reads as | Use when |
| --- | --- | --- |
| `channel.send(text, silent=True)` | joining the conversation | default for ambient |
| `msg.reply(text, mention_author=False, silent=True)` | answering a specific person, no ping | the judge names a target that isn't the newest message |
| `msg.reply(text)` | answering and pinging | never, for ambient |

**`silent=True` on every ambient message.** It suppresses the push notification while still
posting normally. An unprompted bot message should never buzz someone's phone; a mention
reply still may, because that person asked.

**`mention_author=False` is required, not cosmetic.** The client is constructed with
`allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False,
replied_user=True)`. Everyone/users/roles being off means the reply *text* can't ping no
matter what the model writes — but `replied_user=True` means the reply *reference* pings the
person being replied to unless the call overrides it. Omitting the flag on an ambient reply
therefore pings someone who never asked for one, which is the single most annoying thing
this feature could do. `Message.reply` is `channel.send(content, reference=self, **kwargs)`,
so both flags pass straight through.

The judge returns `TARGET`, so the choice is data-driven: no target, or the target *is* the
newest message ⇒ plain `send` (a reply to the last message is visual noise, it's already at
the bottom). Target is an older message ⇒ `reply` with the reference, so "this is about the
thing three messages up" is legible.

### The conversation moves while the model thinks

Judge plus reply is 1–5 seconds. Messages arrive in that window and a reply to a moment
that has passed is exactly what makes a bot feel robotic.

Track staleness against **our own buffer**, not `channel.last_message_id` — that attribute
is populated from cache and can be stale or `None`, and the buffer is the thing we already
know is correct. Record the buffer's length and newest message id at dispatch, compare
immediately before posting, and abort if:

- **the bot was @-mentioned meanwhile** — `handle_mention` owns it now, and posting both is
  a visible double-reply.
- **more than `AMBIENT_STALE_MESSAGES` new messages arrived** (default 3) — the burst the
  judge read is no longer the conversation.
- **the targeted message was deleted** — `reply` to a deleted message raises
  `discord.HTTPException`; fall back to plain `send` rather than dropping the reply.

Aborting after paying for the judge and the reply is *fine*. The spend is a rounding error
next to posting something that lands wrong.

### Ordering against the existing handlers

`on_message` chains `handle_sauce_reply` → `handle_mention`. Ambient goes last and only when
both return False, and must not swallow `process_commands`.

Two subtleties, both of which are easy to get backwards:

- A message that triggers a mention still has to **enter the buffer** — the ambient context
  is incomplete without it — but must **not schedule a gate**. Buffering and gating are
  separate calls made at different points in the handler.
- **The bot's own messages must be buffered too**, or it can't see that it already spoke,
  and the "has a human spoken since" rule has nothing to measure against. So the self-check
  cannot be a blanket early return at the top of the handler.

The author checks belong *inside* `consider_ambient`, not as new early returns in
`on_message`. An early `return` for bot authors would also skip `process_commands`, and
while discord.py's `process_commands` ignores bot authors anyway, changing the shape of a
working handler to add a feature that can be expressed without touching it is how
regressions get introduced. `handle_sauce_reply` and `handle_mention` already do their own
`message.author.bot` checks.

```text
on_message
  ├─ ambient_buffer.add(to_record(message))   ← always, first, before any early return
  ├─ handle_sauce_reply  → return
  ├─ handle_mention      → return             (cancels any pending ambient task for the channel)
  ├─ consider_ambient(message)                ← ignores self and other bots internally
  └─ process_commands
```

Other bots' messages are part of the conversation the judge reads, but they never schedule
a gate and never satisfy the "a human has spoken" precondition. That rule is what stops
an echo loop: two bots running this feature in one channel would otherwise answer each
other until someone pulled the plug.

### Typing indicator

Show `channel.typing()` for the **reply** generation only, never the judge. Typing during
judging leaks the deliberation and — worse — shows even when the answer is "say nothing",
which is the creepiest possible behaviour. The typing indicator should mean "I've decided
to speak and I'm writing", which is what it means for a human.

## Vision without tools

Tools are off, but sight isn't. `answer_mention` already picks
`OPENROUTER_IMAGE_MODEL if image_urls else OPENROUTER_TEXT_MODEL`, which is exactly the
required behaviour. Ambient doesn't call that function (see the wiring section) but applies
the same one-line rule, so the two paths can't drift into choosing models differently.

What ambient has to decide is *which* images, since a 12-message buffer may hold several:

- the targeted message's images, if the judge named a target and it has any;
- otherwise the newest image-bearing message in the burst;
- capped at 2, well under `chat_agent.MAX_IMAGES` (4). Every image is prompt cost on a
  vision-priced model, and a picture six messages back is rarely what's being discussed.

Two constraints worth stating so they aren't rediscovered:

- **The judge never sees the image.** It's a text-only call on a cheap model, and the
  transcript marks attachments as `[image]`. So the judge decides "there's a picture here
  and someone is asking about it" from context alone. Accepted: making the gate multimodal
  would cost more than the reply it's gating.
- **Discord attachment URLs are signed and expire.** The buffer TTL is minutes and the
  signed-param lifetime is far longer, so this can't bite in practice — but it's the reason
  the buffer stores URLs rather than the plan relying on refetching old messages.

## New module: `ambient.py`

Repo root, alongside `chat_agent.py`. **No discord imports** — same rule as the search
layers, so `check_ambient.py` can drive it with plain dicts. asyncio is stdlib and fine.

### `MessageRecord(NamedTuple)`

`author`, `author_id`, `is_bot`, `text`, `image_urls`, `message_id`, `at` (monotonic).

Flattened out of `discord.Message` at the boundary in `bot.py`, so nothing below this line
knows what a discord object is. `image_urls` rather than a `has_image` flag, because the
reply path needs the URLs and refetching the message to get them would be a second failure
mode for no gain.

### `class ChannelBuffer`

Same shape as `Conversation` in `chat_agent.py` — a per-key deque with a TTL — but holding
`MessageRecord`s rather than API message dicts, because the judge needs authorship, images
and timing that the chat history format throws away.

- `add(channel_id, record)`
- `recent(channel_id, limit) -> list[MessageRecord]`
- `newest_id(channel_id) -> int | None` and `size(channel_id) -> int`, for the staleness check
- `forget(channel_id)` — wire into the existing `/forget` so one command clears both

Worth considering a shared base with `Conversation` rather than a second TTL-deque; they
differ only in payload. Left as an implementation call.

### `class AmbientLimits`

All the free refusals in one place, so a reason string can be logged for each:

- channel not in `AMBIENT_CHANNELS`
- cooldown not elapsed since the last ambient reply here
- hourly ceiling hit (`AMBIENT_MAX_PER_HOUR`, sliding window like `MentionThrottle`)
- no human message since the bot's last ambient reply

`allow(channel_id) -> str | None` — `None` to proceed, else the reason, which goes straight
to the log. Every refusal being nameable is what makes the observe-mode log readable; it's
the same reasoning that put every tool guard through `AgentTools._refuse`.

### `build_judge_prompt(records, self_summary) -> str`

A numbered transcript, oldest first, `1. name: text`, with `[image]` marking attachments.
Numbering exists so `TARGET` can refer to a line.

The bot's own messages are labelled with its name like anyone else's — the judge needs to
see that it already spoke.

**The persona does not go in here.** `agent_context.md` is ~6KB; sending it to a cheap model
on every gate is most of the gate's cost, and the gate decides *whether*, not *what*. Only
the reply needs the voice. `AMBIENT_SELF_SUMMARY` is one line ("a Discord bot that finds
where images come from and answers questions"), defaulting to a constant.

### `parse_verdict(text) -> Verdict`

`Verdict(score: int, target: int | None, reason: str)`.

Expected output, three lines:

```text
SCORE: 72
TARGET: 4
REASON: unanswered question about an image nobody sourced
```

Line-oriented `KEY: value`, not JSON — small models break JSON often enough to matter and
this needs no nesting. Parsed with a per-line regex, case-insensitive.

**Fails closed.** Missing or unparseable `SCORE` ⇒ score 0. Out of range ⇒ clamped. `TARGET`
outside the transcript ⇒ `None`, not an error. Reason missing ⇒ empty string. A judge that
returns garbage produces silence, never an exception and never a post.

### `async def judge(session, records, *, api_key, model, self_summary) -> Verdict`

The one network call. It does **not** get its own HTTP layer — that would be a second
OpenRouter client in a repo whose whole convention is one HTTP layer per provider.

**Add `ask_once()` to `chat_agent.py`**: a single tool-free completion taking a system
string and a user string, returning text and cost. It reuses `build_headers`, `_post`,
`_raise_for_status`, `_reported_cost` and the `Chat*` exception hierarchy, and sends
`reasoning: {"enabled": false}` like everything else. `ask()` keeps the persona, history,
images, tools and budget machinery; `judge()` needs none of it.

`max_tokens` around 40 — three short lines. Cheap models pad past that if allowed.

`ask_once()` earns its place twice over: the ambient reply is also tool-free, so it can go
through the same function rather than through `answer_mention`'s tool machinery.

## Wiring into `bot.py`

- `ambient_buffer`, `ambient_limits` as module-level singletons beside `conversations` and
  `mention_throttle`.
- `to_record(message)` — the discord→`MessageRecord` boundary.
- `async def consider_ambient(message)` — schedules/reschedules the per-channel debounce.
- `async def run_ambient(channel)` — the fired task: local gate → judge → threshold →
  staleness re-check → post. Wrapped so a raised exception logs and never kills the task.
- `handle_mention` cancels any pending ambient task for that channel on the way through.

### The reply call

Ambient does **not** reuse `answer_mention`: that function always constructs `AgentTools`
and passes `tools=` and `tool_runner=`, and its top-link reinstatement is meaningless with
no tools. Threading a "no tools" flag through it would leave a function whose body is half
dead code on one of its two paths.

Instead `run_ambient` calls `ask()` directly with `tools=()` and no `tool_runner`, choosing
`OPENROUTER_IMAGE_MODEL` / `OPENROUTER_TEXT_MODEL` by whether images were collected. What
it must not lose by skipping `answer_mention` is the error mapping, which is the valuable
part of that function — so **factor the `except` ladder out of `answer_mention` into
`describe_chat_failure(exc) -> str`**, and have both paths use it. One behaviour change
falls out of that and is wanted: ambient failures are logged and **not posted**. A user who
@-mentions the bot deserves "my API key isn't working"; a channel that never asked deserves
silence.

## Configuration

Added to `.env.example` with a prose block in the existing style.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AMBIENT_ENABLED` | `0` | Off. The privacy-widening switch — see Safety |
| `AMBIENT_CHANNELS` | *(empty)* | Comma-separated channel IDs. Empty means nowhere, even when enabled |
| `AMBIENT_MODE` | `observe` | `observe` logs what it would have said; `reply` posts |
| `AMBIENT_GATE_MODEL` | *(empty)* | Blank ⇒ `openrouter/free`. A cheap pinned ID overrides |
| `AMBIENT_THRESHOLD` | `70` | Score at or above which it speaks |
| `AMBIENT_DEBOUNCE_SECONDS` | `5` | Quiet gap before a burst is judged |
| `AMBIENT_MAX_WAIT_SECONDS` | `30` | Fire anyway, so a busy channel isn't starved |
| `AMBIENT_COOLDOWN_SECONDS` | `120` | Minimum gap between ambient replies in one channel |
| `AMBIENT_MAX_PER_HOUR` | `4` | Hard ceiling per channel; no score overrides it |
| `AMBIENT_BUFFER_MESSAGES` | `12` | Transcript length handed to the judge |
| `AMBIENT_STALE_MESSAGES` | `3` | New messages during generation that abort the post |
| `AMBIENT_SELF_SUMMARY` | *(constant)* | One line telling the judge what the bot is |

All read through the existing `env_str` / `env_int` / `env_flag` helpers, which treat a
blank `KEY=` as unset.

### Budgets

Shared, not parallel: **no new daily-budget object.** With tools off there is no SerpApi
spend to share, so what "shared" means concretely is narrower than it first appears, and
worth being honest about:

- `WEB_SEARCH_DAILY_LIMIT` and `DailyBudget` are untouched, because ambient makes no
  searches. If tools are ever enabled here, they go through the existing `AgentTools`
  rationing rather than a second one.
- `MENTION_RATE_LIMIT_PER_MINUTE` is keyed by user id, and an ambient reply has no invoking
  user. Ambient is bounded by `AMBIENT_COOLDOWN_SECONDS` and `AMBIENT_MAX_PER_HOUR`
  instead — those are ceilings on *how often the bot speaks*, which is the thing that
  actually needs limiting, and they are per channel rather than per user.
- **OpenRouter dollars are the one genuinely shared pool, and nothing caps them today** —
  not for mentions either. Ambient adds a second uncapped spender to it. The per-hour
  ceiling bounds the reply spend; the judge spend is bounded only by traffic. On one
  private server at cheap-model prices that is cents a month, which is why this is a note
  rather than a blocker. A global daily dollar cap would be the fix if it ever matters, and
  it should cover mentions too rather than being an ambient-only budget.

### Model choice and the free tier

Checked against the OpenRouter models API, 2026-08-19. Gate prompt ≈ 800 tokens in, 15 out:

| Gate model | per gate | 200/day | 1000/day |
| --- | --- | --- | --- |
| `openrouter/free` | $0 | $0 | $0 |
| `inclusionai/ling-2.6-flash` | $0.000008 | $0.05/mo | $0.25/mo |
| `qwen/qwen3.7-flash` | $0.000026 | $0.16/mo | $0.78/mo |
| `openai/gpt-oss-20b` | $0.000026 | $0.16/mo | $0.78/mo |

Twenty actual replies a day is $0.06–$0.48/month. **The judging is cheaper than the talking
at every plausible volume** — so tune for restraint, not for cost.

The free router works because the gate is text-only; the no-free-vision-endpoint finding
doesn't apply — and the *reply* uses paid auto routing, which is where vision lives anyway.
The catch is the free tier's **20 requests/minute and 50/day**, rising to **1000/day** only
on an account that has bought $10 of credits lifetime. One private server plausibly fits
inside 50 gates a day; a busy one won't. If the gate starts returning `ChatRateLimited`,
that's the signal to pin a $0.03/M model rather than a bug.

Pin a model either way — `auto` would route a trivial classification to something expensive.

## The judge prompt

Built from negative examples, because the failure mode is over-triggering. A model asked
"should you respond?" is being asked whether to be helpful, and it is trained to say yes.

The prompt must:

- state the bar as *high* and say that most conversations need no bot
- give explicit "do not reply" cases: they're talking to each other; someone already
  answered; it's a joke that needs no third party; the bot spoke recently; it would only be
  agreeing
- give explicit "do reply" cases: an unanswered factual question in the bot's competence;
  someone is asking where an image is from; the bot is being discussed or asked about
- demand the reason *before* the score, so the score is justified rather than vibed
- forbid prose outside the three lines

Calibration is empirical, not designed. That's what observe mode is for.

## Safety and privacy

- **Default off, per-channel opt-in.** Today only messages @-mentioning the bot reach
  OpenRouter; ambient mode sends unaddressed conversation between third parties to whatever
  provider the router picks. That is a real widening and the README has to say so plainly.
  This runs on one private server, but the repo is public and AGPL, so the shipped default
  has to be the conservative one.
- **DMs excluded**, and not reachable via `AMBIENT_CHANNELS` either — a DM is a
  conversation of two where an uninvited third voice is worse than in a guild.
- Transcripts are untrusted input, exactly like tool output. The judge prompt says so, and
  the judge's output is parsed as three regex-matched fields — nothing but a number, an
  integer and a short string survives parsing, so it can't smuggle instructions through.
- The buffer is memory-only and TTL'd, like `Conversation`. A restart forgets.
- `/forget` clears the ambient buffer for the channel too.

### Nothing anyone says reaches disk

The plan originally had observe mode log the transcript at `DEBUG`, on the grounds that
calibration needs to see what the judge saw. That was rejected during the build and the
rule is now absolute: **no message text is written to the log, ever.**

What survives is enough to calibrate on — the score, the threshold it was measured
against, which local guard refused, the cost, and the bot's own reply. What it would have
*said* is a bot response and is logged in full; what prompted it is not. The mention path
was brought into line at the same time and now logs a character count where it used to log
the question.

Two consequences worth keeping:

- **The judge's `REASON` is logged**, and the judge is looking at people's messages when it
  writes it. The prompt therefore tells it to describe rather than quote. That is a prompt
  instruction, not a guarantee, which is the one soft edge in this rule.
- **Never paste log excerpts into the repo** — not into `CLAUDE.md` as a hard-won fact, not
  into a commit message, not into a plan. When the live run teaches something, write the
  finding, not the material: "the judge over-scored agreement-only replies" carries the
  lesson without carrying anyone's chat into a public repo.

`AMBIENT_CHANNELS` gets the same treatment: real channel ids go in `.env`, which is
gitignored. `.env.example` ships it empty and must stay that way — an id identifies a
specific server and channel.

## Testing

`agentic/checks/check_ambient.py`, offline, no key, faking discord objects the way
`check_bot.py` does. Target coverage:

**Buffer** — eviction at limit, TTL expiry, per-channel isolation, the bot's own messages
stored, image URLs preserved, `/forget` clearing.

**Local gate** — each refusal reason fires independently and is nameable; opted-out channel
never proceeds; ceiling holds across a burst; "human spoke since" blocks a second
consecutive ambient reply; another bot's message doesn't satisfy it.

**Debounce** — a new message reschedules; the deadline fires a never-quiet channel; only one
task per channel survives; a mention cancels the pending task.

**Verdict parsing** — well-formed; missing SCORE; non-numeric; out of range; absent TARGET;
target out of bounds; prose wrapped around the three lines; empty string. All resolve to a
`Verdict`, never an exception, and anything malformed scores 0.

**Ordering and races** — mention arriving mid-flight aborts the post; `AMBIENT_STALE_MESSAGES`
exceeded aborts; deleted target degrades to plain `send`; target newest ⇒ plain `send`;
target older ⇒ `reply` with reference; `silent=True` and `mention_author=False` on every
ambient post.

**Vision selection** — targeted message's images win; otherwise newest image-bearing message;
capped at 2; image model chosen only when images were collected.

**Failures are silent** — every `Chat*` exception on the ambient path logs and posts nothing,
while the mention path still posts its friendly message.

**Observe mode** — the full path runs, the log line is produced, nothing is posted.

**Request shape** — gate model pinned, no tools, small `max_tokens`, reasoning disabled,
persona absent from the gate prompt.

`FakeSession` must deep-copy request bodies, per the existing note in `check_chat.py`.

## Build order

1. **Confirm the Message Content intent is actually enabled in the Developer Portal.**
   Blocks everything else. The token has never connected, so this is unproven. Ambient
   reading requires the privileged intent; mentions work without it. Without it discord.py
   fails at login with `PrivilegedIntentsRequired` — loud, not silent.
2. Buffer, limits, debounce, ordering. No network at all; fully covered by `check_ambient.py`.
3. `parse_verdict` and `build_judge_prompt` against fixture transcripts.
4. `ask_once()` in `chat_agent.py` and `describe_chat_failure()` in `bot.py` (it maps to
   user-facing strings, so it belongs beside them), with the existing mention checks still
   passing unchanged.
5. `judge()` on top of `ask_once()`.
6. Observe mode, live, several days in one real channel.
7. Read the log, set `AMBIENT_THRESHOLD` from it, switch to `reply`.

Steps 2–4 are most of the code and cost nothing to verify, which is how the rest of this
repo was built.

## Rejected alternatives

Kept so they aren't re-derived:

- **Single-stage — let the full model decide and emit a `PASS` sentinel.** Fewer round
  trips, one code path. Rejected: it pays full model price on every candidate window,
  loses the tunable threshold, and loses the observe-mode logging that makes calibration
  possible. A model that "decides" to reply leaves no number to argue with.
- **Local embedding similarity** against topics the bot knows. Zero marginal cost and real
  semantic relevance without keywords. Rejected on dependencies: `requirements.txt` is
  three lines, and this wants sentence-transformers plus torch — ~2GB to replace a $0.03/M
  API call. The same reasoning kept BeautifulSoup out of `page_reader.py`.
- **Fixed-cadence polling** of every active channel every N seconds. Bounded, predictable
  cost. Strictly worse than debounce: it fires on silence, misses bursts, and debounce is
  bounded anyway once the cooldown and hourly ceiling exist.
- **Keyword triggering alone** — see "Why a name match isn't the trigger" above.

## Known limitations

Recorded so they aren't mistaken for bugs later:

- **The judge is blind.** It scores on the transcript, with `[image]` as the only signal
  that a picture exists. A conversation whose entire content is an image will be judged on
  its captions.
- **Edits and deletions are ignored.** The buffer records what was said when it was said;
  `on_message_edit` and `on_message_delete` aren't wired. A message edited after the fact
  is judged in its original form.
- **Observe mode pays for the reply it doesn't post.** That's the point — you need to read
  what it would have said — but it means observe mode costs the same as running it live.
- **Nothing detects that another bot already answered.** The cooldown and the "human spoke
  since" rule cover most of it.

## Docs to update when this lands

- `README.md` — what ambient replies are, that they're off by default, and the privacy note.
- `.env.example` — the block above.
- `agentic/CLAUDE.md` — code map row for `ambient.py`, hard-won facts from the live run,
  status.
- `agentic/checks/README.md` — `check_ambient.py`.
- `agentic/IDEAS.md` — already trimmed to a pointer at this plan; delete the entry outright
  once this is built and the plan is marked implemented.
- `agent_context.example.md` — whether the persona needs to know it may speak unprompted.

## What changed during the build

Recorded because the rest of this document describes the design, not the diff.

- **No transcript in the log, at any level.** See above. The plan's observe-mode logging
  was cut back to the score, the guard, the cost and the bot's own words.
- **Text and images only.** `MessageRecord` gained `other_files`; video, audio and
  documents are marked in the transcript so the model can say it can't open them, and a
  message carrying nothing else never buys a gate call (`is_readable`). The judge prompt
  and the persona both got a line saying so.
- **The reply prompt states that nobody asked.** `AMBIENT_CONTEXT_NOTE` is prepended to
  the persona for ambient replies only. Without it the model reads the transcript as a
  question put to it and answers like a summoned assistant.
- **`judge()` returns a `Judgement`, not a `Verdict`** — the verdict plus the cost and the
  routed model, so `bot.py` can log the spend where the logger lives.
- **Staleness is counted by message id** (`count_newer`), not by buffer length: the deque
  stops growing once full, so length would silently stop detecting drift.
- **Observe mode consumes the hourly slot.** Otherwise the cadence in the log is not the
  cadence a live run would have had, which defeats the purpose of calibrating on it.
- **The reply transcript goes in `history`, not in the question.** `MAX_QUESTION_CHARS` is
  1000, which a twelve-message transcript would overrun.
- **A message arriving after the debounce doesn't cancel the evaluation.** The plan had
  every message cancel and reschedule; taken literally that cancels a *running* judge
  mid-request, so one message would abort every reply and make `AMBIENT_STALE_MESSAGES`
  dead code. `ambient_running` now separates the two: while an evaluation is in flight
  nothing new is scheduled, and how far the conversation moved is the staleness count's
  decision. A mention still interrupts, by timestamp rather than by cancellation, so the
  in-flight HTTP call finishes tidily.
- **Only opted-in channels are buffered.** The plan buffered every message so a
  mention-handled one still reached the transcript, which is right *within* a channel the
  bot may speak in - but it also held conversation from channels it never can. Nothing is
  now kept for a channel not in `AMBIENT_CHANNELS`.
- **The NSFW-channel exclusion was dropped.** The plan refused age-restricted channels
  alongside DMs; on a private server that is the owner's call to make in Discord, not the
  bot's to override. `AMBIENT_CHANNELS` is the opt-in either way.
