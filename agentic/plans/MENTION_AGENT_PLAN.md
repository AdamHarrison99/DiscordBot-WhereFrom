# Plan: answer @-mentions with a free OpenRouter model

**Status: implemented.** `chat_agent.py` plus the `bot.py` wiring, verified offline (47
checks). Kept as the design record — the rationale below still explains why the code looks
the way it does.

Two things landed beyond the original plan: **vision** (images attached to the mentioning
message are sent to the model, since `openrouter/free` accepts image input) and
**`/forget`** (clears a channel's memory).

**Now on paid auto-routing.** Free-only was dropped because no free vision endpoint is
reachable — see below. Verified live end-to-end: text $0.000080, image $0.000828.

## Goal

`@WhereFrom what does ?sauce do` → the bot replies in-channel with a short answer, generated
by a free model on OpenRouter. It answers basic questions and explains its own commands. It
does **not** gain the ability to run searches from a mention.

Cost must be structurally zero, not merely intended to be zero.

## Routing: why this ended up on `openrouter/auto`

The original design was free-only, with three guards making cost structurally zero. That
shipped, then lost an argument with reality.

**`openrouter/free` cannot do vision on this account.** Every image request 404s with "No
endpoints available matching your guardrail restrictions and data policy" — including
explicit free vision models (`gemma-4-31b-it:free`, `nemotron-nano-12b-v2-vl:free`), and
with the account's privacy settings opened up. Text-only works fine at zero cost.

Since seeing attached images is a requirement, and it is only reachable on paid routing,
the free-only guards were removed by decision (2026-08-10). `openrouter/auto` now handles
everything, at roughly $0.0001–0.0008 per reply. `OPENROUTER_MODEL=openrouter/free` still
works for anyone who wants zero cost without images.

Two findings worth keeping:

- **`max_price` filters rather than degrades.** Too low a ceiling yields a 404, not a
  cheaper model. Counter-intuitively, `max_price: 0` was what made the *free* router work
  on an account with a zero credit limit — without it, even free requests 404'd. It is now
  omitted by default and exposed as `OPENROUTER_MAX_PRICE`.
- **Reasoning tokens are billed out of `max_tokens`,** so a thinking model spends the whole
  budget and returns empty content. Every request sends `reasoning: {"enabled": false}`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | *(unset)* | Unset ⇒ feature entirely off; mentions behave as today |
| `AGENT_CONTEXT_FILE` | `agent_context.md` | System prompt file, relative to `bot.py` unless absolute |
| `OPENROUTER_MODEL` | `openrouter/free` | Must be free; validated at startup |
| `OPENROUTER_MAX_TOKENS` | `300` | Reply cap |
| `MENTION_RATE_LIMIT_PER_MINUTE` | `4` | Per-user throttle |

Already added to `.env.example`. Read them with the existing `env_str` / `env_int` helpers,
which treat a blank `KEY=` as unset.

`agent_context.md` is gitignored so the personality can be tuned without a commit;
`agent_context.example.md` is the committed template, mirroring the `.env` / `.env.example`
pair already in the repo.

**The two are deliberately not identical and must not be auto-synced.** The example carries
the full safe default including the Boundaries section; the local `agent_context.md` is a
trimmed working copy. Propagate *structural* changes (a new section every deployment
needs, a renamed command) to the example, but never overwrite one file with the other.

## New module: `chat_agent.py`

Same shape as `lens_search.py` and `sauce_search.py`: pure HTTP, **no discord imports**,
testable standalone. That separation is the repo's main structural convention.

```python
class ChatError(Exception): ...          # generic failure
class ChatAuthError(ChatError): ...      # 401, bad key
class ChatRateLimited(ChatError): ...    # 429, includes retry hint
class ChatUnavailable(ChatError): ...    # 502/503, no free provider had capacity
```

### `load_agent_context(path) -> str`

Reads the file as UTF-8 and returns it. Raises on missing/empty — a chat agent with no
system prompt is a misconfiguration, not a default.

The file's first few lines are editor-facing notes above a `---` separator. Decide one of
two and document it in the file itself:

- **Send the whole file.** Simplest, and the stray notes cost ~60 tokens. The current file
  already tells the model "the model sees this verbatim."
- **Strip everything before the first `---`.** Cleaner prompt, one more rule to remember.

Recommend the first — fewer moving parts, and the file is honest about it today.

### `ask(session, context, question, *, model, max_tokens, api_key) -> str`

`POST https://openrouter.ai/api/v1/chat/completions`

```jsonc
{
  "model": "openrouter/free",
  "provider": { "max_price": { "prompt": 0, "completion": 0, "request": 0 } },
  "max_tokens": 300,
  "messages": [
    { "role": "system", "content": "<agent_context.md>" },
    { "role": "user",   "content": "<the user's message, mention stripped>" }
  ]
}
```

Headers: `Authorization: Bearer <key>`, `Content-Type: application/json`, plus
`HTTP-Referer: https://github.com/AdamHarrison99/DiscordBot-WhereFrom` and
`X-Title: WhereFrom` — optional, but they're how OpenRouter attributes usage.

Reuse `bot.session`; the bot already owns one `aiohttp.ClientSession`. Timeout 30s total —
free providers are slower and more variable than SerpApi, so don't reuse the 15s constant.

Status handling: 401 → `ChatAuthError`; 429 → `ChatRateLimited`; 402 → `ChatError` (should
be unreachable given the guards — if it ever fires, one of them has a hole, so log loudly);
502/503 → `ChatUnavailable`; other non-200 → `ChatError`.

On success, assert the response's `usage.cost` is zero (or absent) and log the `model` field
— that's which free model actually answered, and it's the only way to notice the guards
failing. Cheap insurance.

## Wiring into `bot.py`

`on_message` already exists and already calls `handle_sauce_reply`. Add a sibling
`handle_mention` before `bot.process_commands(message)`.

Order matters — check `?sauce` first, since a reply-with-mention shouldn't be swallowed by
the chat path.

Gate, in order, cheapest checks first:

1. Feature enabled (`OPENROUTER_API_KEY` set and context loaded at startup).
2. `message.author.bot` is False. **Non-negotiable** — two of these bots in one channel
   would otherwise talk to each other until the daily quota is gone.
3. `bot.user` is in `message.mentions`, and it's a real mention rather than an
   `@everyone`/`@here` sweep — check `message.mention_everyone` is False.
4. Not a reply whose content is a `?sauce` trigger.
5. Per-user throttle passes.
6. Stripped question is non-empty. A bare `@WhereFrom` with no text gets a canned one-liner
   pointing at `/sauce`, spending no tokens.

Then `async with message.channel.typing():` around the call and `message.reply(...)` with
the answer. Consistent with how `handle_sauce_reply` already behaves.

Note `command_prefix=commands.when_mentioned` is already set, so a mention is also a command
prefix. Verify a plain `@WhereFrom hello` doesn't get eaten as an unknown command before the
chat handler sees it — `process_commands` runs after, so it should be fine, but confirm the
`CommandNotFound` log noise isn't introduced.

### Throttling

An in-memory `dict[user_id, deque[timestamp]]`, sliding one-minute window. Prune on access
so it can't grow unboundedly. Local state is fine here — it's per-process and Railway wiping
it on restart is harmless, unlike a cache that would need persistence.

Over the limit: react to the message rather than replying. A reply costs a message and
invites another mention; a reaction is silent and unmistakable.

## Safety

The system prompt tells the model to ignore embedded instructions, but **the prompt is not
the defence** — a public Discord channel is hostile input and the model will eventually be
talked out of any rule stated in text. Enforce at the code layer:

- Cap the incoming question (~1,000 chars) before sending. Long inputs are the cheap
  injection vector and also the expensive one.
- ~~`allowed_mentions`~~ **already done.** Set on the client constructor in `bot.py` as
  `everyone=False, users=False, roles=False, replied_user=True`, so it defaults for every
  send and needs nothing per call site. A model talked into emitting `@everyone` can't ping
  anyone, but whoever asked still gets their reply notification. Don't "tidy" this to
  `AllowedMentions.none()` — that would silently drop the reply ping.
- Strip control characters with the existing `scrub()` before logging the question, so
  nobody can forge log lines.
- Truncate the reply to Discord's 2,000-char limit; `max_tokens=300` makes that unlikely
  but not impossible.

`agent_context.example.md` carries the injection-resistance and refusal rules in its
Boundaries section; the local `agent_context.md` deliberately omits them. That's a supported
configuration precisely because the code-layer items above hold regardless of what the
prompt says — which is the point of putting them there.

## Testing

Offline, against fixture payloads, same as the existing modules — no key needed for the
bulk of it:

- Each status code maps to the right exception.
- A malformed/empty `choices` array doesn't raise `KeyError`.
- The throttle admits N and rejects N+1 within the window, then admits again after it.
- `load_agent_context` raises on a missing and on an empty file.
- The request body always carries the `max_price` block and a free model id.

Live: one or two calls only. The daily cap is 50 across the whole account, and burning it
on tests means the feature looks broken in Discord for the rest of the day.

## Docs to update when this lands

- `README.md` — new feature, new env vars, and note that mentions are off by default.
- `agentic/CLAUDE.md` — move this out of Backlog into the code map and Status.
- `agent_context.example.md` — carry over structural changes only; see above.

## Open questions

1. **Conversation memory?** Still stateless: one mention, one reply, no history. Threads
   would be nicer but multiply token use against a 50/day cap. Staying stateless.
2. ~~**`/reloadcontext`**~~ — built, then removed (2026-08-10). The context file is
   read at startup only; a personality edit needs a restart. `/forget` clears memory.
3. ~~**Should the bot see attached images?**~~ — yes, built. Up to `MAX_IMAGES` (4) image
   attachments on the mentioning message are sent as `image_url` parts. A mention with an
   image but no text defaults to "What's in this image?" rather than the canned help reply.
