# Ideas

Proposals that have been thought through but not built. Each entry records the design and,
more importantly, the options rejected on the way to it — so a later session doesn't spend
the research again. Nothing here is committed to. An idea that gets built graduates to its
own plan doc and comes out of this file.

---

## Ambient replies — speak without being @-mentioned

**Status: researched 2026-08-19, nothing built.**

A config option letting the bot join a conversation on its own: when it's being talked
about, when it can supply context nobody has, when a joke lands. It must not always
respond, and must not respond emptily.

### The question this turns on

Buffering every message is free; *calling the model* is what costs. They are separate
decisions, and the whole design lives in the gap between them.

`on_message` already receives **every message in every visible channel** —
`intents.message_content = True` is set and the handler fires unconditionally, it just
returns early. So a rolling per-channel buffer of raw chatter costs a `deque` and nothing
else. `Conversation` in `chat_agent.py` is nearly the right shape; it is only ever fed
mention turns today.

So the real question is never "do we send history every time" — it's **what decides the
buffer is worth a model call**.

### Why a name match isn't the trigger

Good precision, terrible recall. It catches "wherefrom is being weird today" and misses
every moment that actually makes a bot feel present:

> "wait what was that image from" · "does anyone know if that's real" · "ask it" ·
> "it said the opposite yesterday"

The false-positive direction is worse than it looks: "where's that from?" said to another
human, answered by a bot, is the annoying-bot failure in one line.

Keep the name match, **demote it**. A hit means "definitely ask the judge", not "definitely
reply".

### The ladder

Four layers, each cheaper than the next, each discarding most of the traffic.

| Layer | Cost | Job |
| --- | --- | --- |
| 0. Buffer | free | Every message into a per-channel deque, with speaker names and the bot's own replies |
| 1. Local gate | free | Channel opted in? Cooldown elapsed? Under the hourly ceiling? Has a human spoken since my last ambient reply? |
| 2. Debounce | free | Wait for a ~5s quiet gap, then judge the *burst* as one unit |
| 3. LLM judge | ~$0.00003 | Read the last ~12 messages, return a 0–100 "should I speak" score and a one-line reason |
| 4. Reply | $0.0001–0.0008 | Above threshold ⇒ the existing `answer_mention` path, tools and all |

**Layer 2 buys the most naturalness for zero money.** People type in bursts; judging
message-by-message interrupts mid-thought, judging on the pause reads the room. It also
collapses a six-message flurry into one gate call.

**Layer 3 returns a score, not a boolean.** One tunable knob, and — the real payoff — the
near-misses are loggable. `scored 62, threshold 70, reason: they're asking each other, not
me` is how the threshold gets calibrated instead of guessed.

### Cost is not the constraint

Gate prompt ≈ 800 tokens in, 15 out. Prices checked against the OpenRouter models API on
2026-08-19:

| Gate model | per gate | 200/day | 1000/day |
| --- | --- | --- | --- |
| `openrouter/free`, or a pinned `:free` model | $0 | $0 | $0 |
| `inclusionai/ling-2.6-flash` | $0.000008 | $0.05/mo | $0.25/mo |
| `qwen/qwen3.7-flash` | $0.000026 | $0.16/mo | $0.78/mo |
| `openai/gpt-oss-20b` | $0.000026 | $0.16/mo | $0.78/mo |
| `google/gemma-3-4b-it` | $0.000042 | $0.25/mo | $1.25/mo |

Twenty actual replies a day is $0.06–$0.48/month. **The judging is cheaper than the talking
at every plausible volume**, so design for not being annoying rather than for cheapness.

The free router works here — the gate is text-only, so the no-free-vision-endpoint finding
doesn't apply. One caveat: OpenRouter's free tier is **20 requests/minute and 50/day**,
rising to **1000/day** only on an account that has purchased $10 of credits lifetime. Under
that threshold, 50/day is too tight for a busy channel and a $0.03/M paid model is the
safer floor. Pin a specific cheap model rather than `openrouter/auto` either way — auto
would route a trivial classification to something expensive.

### What will actually bite

- **Models say yes far too often.** The central risk, not cost. A model asked "should you
  respond?" is being asked whether to be helpful, and it is trained to say yes. In order of
  effectiveness: a hard hourly ceiling no score can override; a high threshold lowered only
  on logged evidence; a judge prompt built from explicit *negative* examples ("they're
  talking to each other", "someone already answered", "it's a joke that needs no third
  party") rather than positive ones.
- **The Message Content intent is unverified.** The token has never connected. Ambient
  reading *requires* the privileged intent actually enabled in the Developer Portal;
  mentions work without it, arbitrary channel reading does not. Without it discord.py fails
  at login with `PrivilegedIntentsRequired` — loud, not silent. Confirm before building.
- **It materially widens what leaves the server.** Today only messages @-mentioning the bot
  reach OpenRouter. Ambient mode sends unaddressed conversation between third parties to
  whatever provider the router picks. Defensible, but it wants: default off, per-channel
  opt-in (never server-wide), and a line in the README. The AGPL means someone else will
  run this, so the default has to be the conservative one.
- **Echo loops.** The bot's own messages go into the buffer (so it doesn't repeat itself)
  but must never trigger a gate, and "a human has spoken since my last ambient reply" is a
  hard precondition. Two bots with this feature in one channel loop forever otherwise.
- **Ordering.** `on_message` chains `handle_sauce_reply` → `handle_mention`; ambient goes
  last, only when both return False, and must not swallow `process_commands`.

### Rejected

- **Single-stage — let the full model decide and emit a `PASS` sentinel.** Fewer round
  trips, one code path. Rejected: full model price on every candidate window, no tunable
  threshold, and no observe-mode logging. A model that "decides" to reply leaves no number
  to argue with.
- **Local embedding similarity** against topics the bot knows. Zero marginal cost, real
  semantic relevance without keywords. Rejected on dependencies: `requirements.txt` is
  three lines, and this wants sentence-transformers plus torch — ~2GB to replace a $0.03/M
  API call. Same reasoning that kept BeautifulSoup out of `page_reader.py`.
- **Fixed-cadence polling** of every active channel every N seconds. Bounded, predictable
  cost. Strictly worse than debounce: fires on silence, misses bursts, and debounce is
  bounded anyway once the cooldown exists.

### Config surface

| Variable | Default | Meaning |
| --- | --- | --- |
| `AMBIENT_ENABLED` | `0` | Off by default — this is the privacy-widening switch |
| `AMBIENT_CHANNELS` | *(empty)* | Comma-separated channel IDs; empty means nowhere |
| `AMBIENT_MODE` | `observe` | `observe` or `reply` |
| `AMBIENT_GATE_MODEL` | *(empty)* | Blank ⇒ `openrouter/free`; a cheap paid ID overrides |
| `AMBIENT_THRESHOLD` | `70` | 0–100, from the judge |
| `AMBIENT_DEBOUNCE_SECONDS` | `5` | Quiet gap before a burst is judged |
| `AMBIENT_COOLDOWN_SECONDS` | `120` | Minimum gap between ambient replies in one channel |
| `AMBIENT_MAX_PER_HOUR` | `4` | Hard ceiling; no score overrides it |
| `AMBIENT_BUFFER_MESSAGES` | `12` | How much transcript the judge sees |

**`AMBIENT_MODE=observe` is the load-bearing one.** The bot has never been on a real
server, so there is no distribution to pick a threshold against. Observe mode runs the full
gate and logs `score / reason / the reply it would have posted`, and posts nothing. Run it
live for a few days, read the log, then pick the number and switch to `reply`. It costs the
gate price and turns a guess into a measurement.

### Build order

1. Confirm Message Content is enabled in the Developer Portal — blocks everything else.
2. Buffer, local gate, debounce. No API calls at all; testable offline against faked
   messages the way `check_bot.py` already does.
3. The judge as a module with no discord imports, alongside `web_search.py`, testable
   against fixture transcripts.
4. Observe mode, live, several days.
5. Threshold from the logs, then `reply`.

Steps 2–3 are most of the code and cost nothing to verify, which is how the rest of this
repo was built.
