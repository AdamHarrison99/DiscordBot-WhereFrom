"""Offline checks for chat_agent + the bot wiring. No network, no API key."""
import asyncio, sys, types
from copy import deepcopy
from pathlib import Path

import tempfile
REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
TMP = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp())
sys.path.insert(0, str(REPO))
import chat_agent as ca

ok, fail = 0, 0
def check(label, cond):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"  FAIL: {label}")

# --- request body ---
body = ca.build_request("ctx", "q", ca.AUTO_ROUTER_MODEL, 300)
check("no provider block by default", "provider" not in body)
check("model passed through", body["model"] == "openrouter/auto")
# One id sends `model`; several send `models`, which OpenRouter walks in order.
check("a single model sends no models array", "models" not in body)
chain = ca.build_request("ctx", "q", ["a/one", "b/two"], 300)
check("a chain sends models, in order", chain["models"] == ["a/one", "b/two"])
check("a chain sends no single model key", "model" not in chain)
check("a one-entry chain is a plain model",
      ca.build_request("ctx", "q", ["a/one"], 300)["model"] == "a/one")
check("model_field drops blanks", ca.model_field(["a/one", "", "b/two"])["models"] == ["a/one", "b/two"])
check("an empty chain falls back rather than sending models: []",
      ca.model_field([]) == {"model": ca.FREE_ROUTER_MODEL})
# Audio has no URL form, so it rides in the body as base64.
_audio_body = ca.build_request("s", "q", "m/1", 50, audio=[("AAAA", "ogg")])
_audio_parts = _audio_body["messages"][-1]["content"]
check("audio becomes an input_audio part",
      _audio_parts[-1] == {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "ogg"}})
check("the question still leads the parts", _audio_parts[0]["type"] == "text")
# Without the label the model reads any attachment as an image.
check("the text part says the clip is audio", "not an image" in _audio_parts[0]["text"])
check("no audio means no label",
      ca.AUDIO_NOTE not in ca.build_request("s", "q", "m/1", 50)["messages"][-1]["content"])
check("no audio and no image stays a plain string",
      isinstance(ca.build_request("s", "q", "m/1", 50)["messages"][-1]["content"], str))
check("audio is capped per message",
      sum(1 for p in ca.build_request(
          "s", "q", "m/1", 50,
          audio=[("A", "ogg")] * (ca.MAX_AUDIO_CLIPS + 2))["messages"][-1]["content"]
          if p["type"] == "input_audio") == ca.MAX_AUDIO_CLIPS)
check("images and audio travel together",
      [p["type"] for p in ca.build_request("s", "q", "m/1", 50, ["http://x/a.png"],
                                           audio=[("A", "ogg")])["messages"][-1]["content"]]
      == ["text", "image_url", "input_audio"])

# OpenRouter 400s on a fourth entry, so the cap binds where the body is built.
check("model_field caps the array at three",
      ca.model_field(["a/1", "b/2", "c/3", "d/4"]) == {"models": ["a/1", "b/2", "c/3"]})
check("the cap keeps the order given",
      ca.model_field(["a/1", "b/2", "c/3", "d/4"])["models"][0] == "a/1")
check("system prompt first", body["messages"][0]["role"] == "system")
long_q = "x" * 5000
check("question capped", len(ca.build_request("c", long_q, ca.AUTO_ROUTER_MODEL, 300)["messages"][1]["content"]) == ca.MAX_QUESTION_CHARS)

# --- optional price ceiling ---
capped = ca.build_request("c", "q", ca.AUTO_ROUTER_MODEL, 300, (), 5.0)
check("ceiling applied when set", capped["provider"]["max_price"] == {"prompt": 5.0, "completion": 5.0})
check("ceiling omitted when None", "provider" not in ca.build_request("c", "q", ca.AUTO_ROUTER_MODEL, 300, (), None))
check("zero ceiling is still sent", ca.build_request("c", "q", ca.AUTO_ROUTER_MODEL, 300, (), 0.0)["provider"]["max_price"]["prompt"] == 0.0)

# --- vision: images become multimodal parts ---
check("no images -> plain string", isinstance(body["messages"][1]["content"], str))
vis = ca.build_request("c", "what is this", ca.AUTO_ROUTER_MODEL, 300, ["https://cdn/1.png"])
parts = vis["messages"][1]["content"]
check("images -> list of parts", isinstance(parts, list) and len(parts) == 2)
check("text part first", parts[0] == {"type": "text", "text": "what is this"})
check("image part shape", parts[1] == {"type": "image_url", "image_url": {"url": "https://cdn/1.png"}})
many = ca.build_request("c", "q", ca.AUTO_ROUTER_MODEL, 300, [f"u{i}" for i in range(10)])
check("image count capped", len(many["messages"][1]["content"]) == ca.MAX_IMAGES + 1)
check("question still capped with images",
      len(ca.build_request("c", long_q, ca.AUTO_ROUTER_MODEL, 300, ["u"])["messages"][1]["content"][0]["text"]) == ca.MAX_QUESTION_CHARS)

# --- privacy/no-endpoints error is its own class ---
try:
    ca._raise_for_status(404, {"error": {"message": "No endpoints available matching your guardrail restrictions and data policy."}})
    check("no-endpoints raises", False)
except ca.ChatNoEndpoints:
    check("no-endpoints -> ChatNoEndpoints", True)
check("ChatNoEndpoints is a ChatUnavailable", issubclass(ca.ChatNoEndpoints, ca.ChatUnavailable))

# --- status -> exception mapping ---
cases = [
    (401, {}, ca.ChatAuthError),
    (429, {}, ca.ChatRateLimited),
    (402, {}, ca.ChatError),
    (502, {}, ca.ChatUnavailable),
    (503, {}, ca.ChatUnavailable),
    (500, {}, ca.ChatError),
    (200, {"error": {"message": "boom"}}, ca.ChatError),
]
for status, payload, expected in cases:
    try:
        ca._raise_for_status(status, payload)
        check(f"{status} raises", False)
    except expected:
        check(f"{status} -> {expected.__name__}", True)
    except Exception as exc:
        check(f"{status} -> {expected.__name__} (got {type(exc).__name__})", False)
ca._raise_for_status(200, {"choices": []})  # must not raise
check("200 clean passes", True)

# --- malformed payloads don't KeyError ---
for payload in ({}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]},
                {"choices": [{"message": {"content": None}}]},
                {"choices": [{"message": {"content": "   "}}]},
                {"choices": [{"message": {"content": "", "reasoning": "thinking"}}]}):
    try:
        ca._extract_reply(payload)
        check(f"empty reply raises for {payload}", False)
    except ca.ChatError:
        check("empty reply raises ChatError", True)
    except Exception as exc:
        check(f"wrong exception {type(exc).__name__} for {payload}", False)
check("good reply extracted", ca._extract_reply({"choices": [{"message": {"content": " hi "}}]}) == "hi")

# --- cost parsing ---
check("no usage -> 0", ca._reported_cost({}) == 0.0)
check("null cost -> 0", ca._reported_cost({"usage": {"cost": None}}) == 0.0)
check("junk cost -> 0", ca._reported_cost({"usage": {"cost": "abc"}}) == 0.0)
check("real cost parsed", ca._reported_cost({"usage": {"cost": 0.002}}) == 0.002)

# --- context loading ---
tmp = TMP
missing = tmp / "nope.md"
try:
    ca.load_agent_context(missing); check("missing file raises", False)
except ca.ChatError: check("missing file raises", True)
empty = tmp / "empty.md"; empty.write_text("   \n", encoding="utf-8")
try:
    ca.load_agent_context(empty); check("empty file raises", False)
except ca.ChatError: check("empty file raises", True)
good = tmp / "good.md"; good.write_text("system prompt", encoding="utf-8")
check("good file loads", ca.load_agent_context(good) == "system prompt")

# --- throttle ---
t = ca.MentionThrottle(limit=3, window_seconds=60.0)
check("admits up to limit", all(t.allow(1, now=100.0) for _ in range(3)))
check("rejects limit+1", not t.allow(1, now=100.0))
check("other user unaffected", t.allow(2, now=100.0))
check("still blocked mid-window", not t.allow(1, now=159.0))
check("admits after window", t.allow(1, now=161.0))
t2 = ca.MentionThrottle(limit=2, window_seconds=60.0)
t2.allow(99, now=0.0)
t2.allow(100, now=0.0)
t2.allow(101, now=200.0)          # sweeps the two stale users
check("stale users evicted", set(t2._hits) == {101})

# --- attribution headers on every call ---
h = ca.build_headers("sk-secret")
check("Authorization set", h["Authorization"] == "Bearer sk-secret")
check("X-Title is the app name", h["X-Title"] == ca.APP_NAME == "WhereFrom")
check("HTTP-Referer is the app url", h["HTTP-Referer"] == ca.APP_URL)
check("ask() uses build_headers", "build_headers(api_key)" in (repo_src := (REPO / "chat_agent.py").read_text(encoding="utf-8")))
check("no other header dict in ask", repo_src.count("Authorization") == 1)

# --- reply length backstop ---
check("default reply budget is 150", ca.DEFAULT_MAX_TOKENS == 150)
check("3 sentences fit in the budget",
      ca.estimate_tokens("This is a sentence of fairly ordinary length. " * 3) < ca.DEFAULT_MAX_TOKENS)

def reply(content, finish="stop"):
    return ca._extract_reply({"choices": [{"message": {"content": content}, "finish_reason": finish}]})

check("finish=stop is never trimmed", reply("No trailing period here") == "No trailing period here")
check("complete reply untouched", reply("One. Two. Three.", "length") == "One. Two. Three.")
cut = "The Python logo! It's blue and yellow. And it has two snak"
check("cut-off fragment dropped", reply(cut, "length") == "The Python logo! It's blue and yellow.")
# The persona opens with "Eto..." - a naive sentence split would keep only that.
ellipsis = "Eto... the ?sauce command takes an image and finds where it came fr"
check("early ellipsis doesn't gut the reply", reply(ellipsis, "length") == ellipsis)
check("no terminator at all is kept", reply("just one long clause with no end", "length") == "just one long clause with no end")
check("trailing whitespace stripped", reply("  hello.  ") == "hello.")

# --- empty replies say why ---
def extract(choice):
    return ca._extract_reply({"choices": [choice]})

def expect(label, choice, exc_type):
    try:
        extract(choice)
        check(label, False)
    except exc_type:
        check(label, True)
    except Exception as e:
        check(f"{label} (got {type(e).__name__})", False)

expect("refusal field -> ChatRefused",
       {"message": {"content": "", "refusal": "I can't help with that"}, "finish_reason": "stop"},
       ca.ChatRefused)
expect("content_filter -> ChatRefused",
       {"message": {"content": ""}, "finish_reason": "content_filter"},
       ca.ChatRefused)
expect("plain empty -> ChatEmptyReply",
       {"message": {"content": ""}, "finish_reason": "stop"},
       ca.ChatEmptyReply)
try:
    extract({"message": {"content": ""}, "finish_reason": "stop", "native_finish_reason": "STOP"})
except ca.ChatEmptyReply as e:
    # The channel sees this, so it must stay free of internals.
    check("user-facing text is plain", str(e) == "the model returned an empty reply")
    check("detail kept for the log", "finish_reason=stop" in e.detail and "native=STOP" in e.detail)

# the real deepseek-v4-pro failure: reasoning ran despite reasoning.enabled=false
try:
    ca._extract_reply({
        "choices": [{"message": {"content": "", "reasoning": "We need to respond as..."},
                     "finish_reason": "length", "native_finish_reason": "length"}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 150}},
    })
    check("reasoning-starved reply raises", False)
except ca.ChatEmptyReply as e:
    check("reasoning-starved reply raises", True)
    check("detail names the reasoning burn", "reasoning" in e.detail and "150" in e.detail)
    check("user-facing text still plain", str(e) == "the model returned an empty reply")

check("ChatRefused is a ChatError", issubclass(ca.ChatRefused, ca.ChatError))
check("ChatEmptyReply is a ChatError", issubclass(ca.ChatEmptyReply, ca.ChatError))
check("ChatRefused is not ChatUnavailable", not issubclass(ca.ChatRefused, ca.ChatUnavailable))

# --- context budget ---
CPT = ca.CHARS_PER_TOKEN
check("token estimate rounds up", ca.estimate_tokens("x" * (CPT + 1)) == 2)
check("empty text is 0 tokens", ca.estimate_tokens("") == 0)

p = ca.fit_to_budget("ctx", [], "q", 2000)
check("under budget is untouched", p == ("ctx", [], "q", 0))

# the system prompt is never trimmed; the budget covers history and question
ctx, q = "c" * 400, "q" * 400
p = ca.fit_to_budget(ctx, [], q, 50)                       # budget 200 chars
check("context kept intact", p.context == ctx)
check("question trimmed to the budget", len(p.question) == 200)
check("dropped count reported", p.dropped_chars == 200)

big = "c" * 5000
p = ca.fit_to_budget(big, [], "q" * 500, 200)              # budget 800 chars
check("oversized context survives whole", p.context == big)
check("question untouched when it fits", p.question == "q" * 500)
check("context excluded from the budget",
      len(p.question) + sum(len(m["content"]) for m in p.history) <= 200 * CPT)

# even a tiny budget leaves the prompt alone and keeps a usable question
p = ca.fit_to_budget(big, [], "q" * 5000, 1)
check("tiny budget still sends the whole prompt", p.context == big)
check("question keeps its floor", len(p.question) == ca.MIN_QUESTION_CHARS)

check("zero budget disables trimming", ca.fit_to_budget(big, [], "q", 0) == (big, [], "q", 0))
check("negative budget disables trimming", ca.fit_to_budget(big, [], "q", -5) == (big, [], "q", 0))

# build_request enforces the budget, over the question, never the system prompt
b = ca.build_request(big, "q" * 900, ca.AUTO_ROUTER_MODEL, 300, max_context_tokens=200)
check("system prompt sent whole", b["messages"][0]["content"] == big)
check("build_request enforces the budget", len(b["messages"][1]["content"]) <= 200 * CPT)
b_img = ca.build_request(big, "q" * 900, ca.AUTO_ROUTER_MODEL, 300, ["u"], None, 200)
check("system prompt whole on the vision path", b_img["messages"][0]["content"] == big)
check("budget enforced on the vision path too",
      len(b_img["messages"][1]["content"][0]["text"]) <= 200 * CPT)
check("default budget is 2000", ca.DEFAULT_MAX_CONTEXT_TOKENS == 2000)

# --- reasoning always disabled ---
check("reasoning disabled", body["reasoning"] == {"enabled": False})
check("reasoning disabled with images too", vis["reasoning"] == {"enabled": False})

# --- conversation memory ---
H = [{"role": "user", "content": "who are you"}, {"role": "assistant", "content": "a bot"}]
b = ca.build_request("ctx", "and what else?", ca.AUTO_ROUTER_MODEL, 150, history=H)
roles = [m["role"] for m in b["messages"]]
check("history sits between system and question", roles == ["system", "user", "assistant", "user"])
check("current question is last", b["messages"][-1]["content"] == "and what else?")
check("history content preserved", b["messages"][1]["content"] == "who are you")
check("no history -> just system+user",
      [m["role"] for m in ca.build_request("c", "q", ca.AUTO_ROUTER_MODEL, 150)["messages"]] == ["system", "user"])

c = ca.Conversation(max_turns=2, ttl_seconds=1800)
check("starts empty", c.history(1) == [])
c.remember(1, "user", "first", now=0.0)
c.remember(1, "assistant", "reply one", now=1.0)
check("turn recorded", [m["content"] for m in c.history(1, now=2.0)] == ["first", "reply one"])
c.remember(1, "user", "second", now=2.0)
c.remember(1, "assistant", "reply two", now=3.0)
c.remember(1, "user", "third", now=4.0)
c.remember(1, "assistant", "reply three", now=5.0)
kept = [m["content"] for m in c.history(1, now=6.0)]
check("oldest turns fall off", kept == ["second", "reply two", "third", "reply three"])
check("channels are separate", c.history(2, now=6.0) == [])
c.remember(2, "user", "elsewhere", now=6.0)
check("other channel unaffected", [m["content"] for m in c.history(1, now=6.0)] == kept)
check("expired conversation is dropped", c.history(1, now=6.0 + 1800) == [])
c.remember(3, "user", "x", now=0.0)
check("forget clears", c.forget(3) and c.history(3, now=1.0) == [])
check("forget on unknown key is False", not c.forget(999))
check("empty content not stored", (c.remember(4, "user", "", now=0.0), c.history(4, now=0.0) == [])[1])

# Ids, so the mention path can ask whether a message is already a turn.
ided = ca.Conversation(max_turns=4, ttl_seconds=1800)
ided.remember(1, "assistant", "said this", now=0.0, message_id=77)
ided.remember(1, "user", "and this", now=1.0)
check("a remembered id is known", ided.knows(1, 77))
check("an unremembered id is not", not ided.knows(1, 78))
check("a turn stored without an id is not known by None", not ided.knows(1, None))
check("knows is per channel", not ided.knows(2, 77))
check("history still carries only role and content",
      all(set(m) == {"role", "content"} for m in ided.history(1, now=2.0)))

# The message a reply points at, when the memory hasn't got it.
turn = ca.replied_turn("alpha", "the original", from_bot=False)
check("someone else's replied turn is a named user turn",
      turn == {"role": "user", "content": "alpha: the original"})
check("the bot's own replied turn is an unnamed assistant turn",
      ca.replied_turn("wherefrom", "what I said", from_bot=True)
      == {"role": "assistant", "content": "what I said"})
check("a long replied message is cut",
      len(ca.replied_turn("a", "x" * 5000, from_bot=True)["content"])
      == ca.MAX_QUESTION_CHARS)
off = ca.Conversation(max_turns=0, ttl_seconds=1800)
off.remember(1, "user", "ignored", now=0.0)
check("0 turns disables memory", off.history(1, now=0.0) == [])

# oldest history is dropped first when the budget bites
long_hist = [{"role": "user", "content": "h" * 400} for _ in range(5)]
p = ca.fit_to_budget("c" * 100, long_hist, "q" * 100, 200)   # budget 800 chars
check("history trimmed to fit", len(p.history) < 5)
check("budget respected with history",
      len(p.context) + len(p.question) + sum(len(m["content"]) for m in p.history) <= 800)
check("newest history survives", p.history == long_hist[-len(p.history):] if p.history else True)
b = ca.build_request("c" * 100, "q" * 100, ca.AUTO_ROUTER_MODEL, 150,
                     max_context_tokens=200, history=long_hist)
sent = sum(len(m["content"]) for m in b["messages"] if isinstance(m["content"], str))
check("build_request enforces budget across history", sent <= 800)

# --- tool calling ---
class FakeResp:
    def __init__(self, payload): self.payload, self.status = payload, 200
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self, content_type=None): return self.payload

class FakeSession:
    """Replays queued payloads and snapshots each outgoing body.

    ask() mutates one dict across rounds."""
    def __init__(self, payloads): self.payloads, self.bodies = list(payloads), []
    def post(self, url, json=None, headers=None, timeout=None):
        self.bodies.append(deepcopy(json))
        return FakeResp(self.payloads.pop(0))

def tool_payload(name="find_image_source", call_id="c1"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]},
        "finish_reason": "tool_calls"}], "model": "m", "usage": {"cost": 0.001}}

def text_payload(text="found it: example.com"):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "model": "m2", "usage": {"cost": 0.002}}

ran = []
async def runner(call):
    ran.append((call.get("function") or {}).get("name"))
    return "Reverse image search results: - Foo | example.com"

s = FakeSession([tool_payload(), text_payload()])
r = asyncio.run(ca.ask(s, "ctx", "where is this from", api_key="k",
                       image_urls=["https://cdn/x.png"],
                       tools=[{"type": "function", "function": {"name": "find_image_source"}}],
                       tool_runner=runner))
check("tool was executed", ran == ["find_image_source"])
check("final text returned", r.text == "found it: example.com")
check("cost summed across rounds", abs(r.cost - 0.003) < 1e-9)
check("tools_used reported", r.tools_used == ("find_image_source",))
check("two requests made", len(s.bodies) == 2)
check("tools sent on the first request", "tools" in s.bodies[0])
second = s.bodies[1]["messages"]
check("assistant tool_calls turn replayed", second[-2].get("tool_calls") is not None)
check("tool result appended", second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1")
check("tool result carries the search output", "example.com" in second[-1]["content"])

# no runner -> tools are never advertised, and a tool_calls reply isn't looped
s2 = FakeSession([text_payload("plain")])
r2 = asyncio.run(ca.ask(s2, "ctx", "hi", api_key="k",
                        tools=[{"type": "function", "function": {"name": "x"}}]))
check("no runner means no tools sent", "tools" not in s2.bodies[0])
check("plain answer still works", r2.text == "plain")
check("no tools reported", r2.tools_used == ())

# the round cap holds: a model that keeps calling tools doesn't loop forever
ran.clear()
s3 = FakeSession([tool_payload(), tool_payload(call_id="c2"), text_payload()])
try:
    asyncio.run(ca.ask(s3, "ctx", "q", api_key="k", image_urls=["u"],
                       tools=[{"type": "function", "function": {"name": "find_image_source"}}],
                       tool_runner=runner))
    check("round cap stops the loop", False)
except ca.ChatEmptyReply:
    check("round cap stops the loop", True)
check("only one extra round ran", len(s3.bodies) == 2 and len(ran) == 1)

# ...and the last round forbids tools, which would return empty content.
check("first round leaves tool choice open", "tool_choice" not in s3.bodies[0])
check("last round forbids further tool calls", s3.bodies[1].get("tool_choice") == "none")

ran.clear()
s5 = FakeSession([tool_payload(), tool_payload(call_id="c2"), text_payload()])
r5 = asyncio.run(ca.ask(s5, "ctx", "q", api_key="k", image_urls=["u"],
                        tools=[{"type": "function", "function": {"name": "find_image_source"}}],
                        tool_runner=runner, max_tool_rounds=2))
check("two rounds of tools run when allowed", len(ran) == 2)
check("only the final request forbids tools",
      [b.get("tool_choice") for b in s5.bodies] == [None, None, "none"])
check("an answer still comes back", r5.text == "found it: example.com")

check("tools_used default is immutable", ca.ChatReply("t", "m", 0.0).tools_used == ())

# tool output lands after the budget has been applied, so it is capped on its own
async def flood(call): return "x" * 50_000
s4 = FakeSession([tool_payload(), text_payload()])
asyncio.run(ca.ask(s4, "ctx", "q", api_key="k", image_urls=["u"],
                   tools=[{"type": "function", "function": {"name": "find_image_source"}}],
                   tool_runner=flood))
check("runaway tool output is capped",
      len(s4.bodies[1]["messages"][-1]["content"]) == ca.MAX_TOOL_RESULT_CHARS)

print(f"chat_agent: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
