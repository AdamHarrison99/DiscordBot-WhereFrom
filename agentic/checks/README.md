# Checks

Offline verification for the bot modules. No pytest, no network, no API key — plain scripts
that print `N passed, M failed` and exit non-zero on failure.

Run them from the repo root so the venv is on hand:

```bash
.venv/Scripts/python.exe agentic/checks/check_chat.py   # chat_agent.py
.venv/Scripts/python.exe agentic/checks/check_bot.py    # bot.py wiring
.venv/Scripts/python.exe agentic/checks/check_web.py    # web_search.py
.venv/Scripts/python.exe agentic/checks/check_page.py   # page_reader.py
```

All default to the repo root and need no arguments; pass a path to point one elsewhere.

`check_bot.py` fakes discord objects rather than importing a gateway, so it covers the
parts that only misbehave against real Discord: mention detection, the reply trigger,
image collection, throttling, and conversation memory. It also covers the agent's tool
dispatch — which tools are offered, the per-message and daily rationing, and every
failure path turning into something the model can say instead of an exception.

`check_page.py` fakes DNS as well as HTTP, so the address guard is exercised for real:
private, loopback and link-local targets, and redirects into them.

## The live ones cost money

```bash
.venv/Scripts/python.exe agentic/checks/live_convo.py   # multi-speaker memory
.venv/Scripts/python.exe agentic/checks/live_tool.py    # agent-driven source lookup
```

Both hit the real OpenRouter API (~$0.001 a turn) and read the real `.env`.
`live_tool.py` stubs the reverse image search by default so it costs no SerpApi quota;
pass `--real` to spend one of the 100/month.

## Why they live in `agentic/`

They're agent-facing verification, kept with the rest of the agent docs rather than beside
the modules they exercise. Nothing imports them, so they carry no runtime weight in a
clone — they only need the venv the bot already uses.
