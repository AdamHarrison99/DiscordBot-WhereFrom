"""Offline checks for the bot.py mention wiring. Fakes discord objects."""
import asyncio, os, sys, types
from pathlib import Path

repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo))
os.environ.setdefault("DISCORD_BOT_TOKEN", "d")
os.environ.setdefault("SERPAPI_KEY", "d")
os.environ["LOG_FILE"] = "none"
os.environ["OPENROUTER_API_KEY"] = "sk-test"

import bot as B

ok, fail = 0, 0
def check(label, cond):
    global ok, fail
    if cond: ok += 1
    else:
        fail += 1
        print(f"  FAIL: {label}")

BOT_ID = 4242
B.bot._connection.user = types.SimpleNamespace(id=BOT_ID, bot=True)


class Msg:
    """Minimal stand-in for discord.Message."""
    def __init__(self, content, author_id=1, author_bot=False, attachments=(),
                 mentions=None, mention_everyone=False, reference=None, channel_id=10):
        self.content = content
        self.author = types.SimpleNamespace(id=author_id, bot=author_bot,
                                            display_name=f"user{author_id}")
        self.attachments = list(attachments)
        self.mentions = mentions if mentions is not None else []
        self.mention_everyone = mention_everyone
        self.reference = reference
        self.channel_id = channel_id
        self.guild = None
        self.replies, self.reactions = [], []


def bot_message(id=500, attachments=()):
    """A message authored by the bot, as `reference.resolved` would give it."""
    m = B.discord.Message.__new__(B.discord.Message)
    m.author = types.SimpleNamespace(id=BOT_ID, bot=True)
    m.attachments = list(attachments)
    m.id = id
    return m


def ref(resolved=None, message_id=None):
    return types.SimpleNamespace(resolved=resolved, message_id=message_id)


MENTION = f"<@{BOT_ID}>"
NICK = f"<@!{BOT_ID}>"

# --- mention detection: content, not the mentions array ---
check("plain mention detected", B.mentions_bot(Msg(f"{MENTION} hello")))
check("nickname form detected", B.mentions_bot(Msg(f"{NICK} hello")))
check("no mention -> False", not B.mentions_bot(Msg("hello")))
# The regression: Discord lists the replied-to author in `mentions`, so a reply
# to one of our messages arrives looking mentioned without any @ in the text.
reply_to_bot = Msg("thanks!", mentions=[types.SimpleNamespace(id=BOT_ID)])
check("reply to bot is NOT a mention", not B.mentions_bot(reply_to_bot))
check("other user's mention ignored", not B.mentions_bot(Msg("<@999> hi")))

# --- stripping ---
check("mention stripped", B.strip_bot_mention(Msg(f"{MENTION} what is this")) == "what is this")
check("trailing mention stripped", B.strip_bot_mention(Msg(f"hey {NICK}")) == "hey")
check("bare mention -> empty", B.strip_bot_mention(Msg(MENTION)) == "")

# --- handle_mention gating, without touching the network ---
calls = []
answer_ok = True
async def fake_answer(question, image_urls, history=None, who="someone"):
    calls.append((question, list(image_urls), list(history or [])))
    return ("answer", answer_ok)
B.answer_mention = fake_answer

class FakeTyping:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

async def run(msg):
    async def fetch(mid):
        raise B.discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "gone")
    msg.channel = types.SimpleNamespace(id=msg.channel_id, typing=lambda: FakeTyping(),
                                        fetch_message=fetch, __str__=lambda s: "chan")
    async def reply(content): msg.replies.append(content)
    async def react(emoji): msg.reactions.append(emoji)
    msg.reply, msg.add_reaction = reply, react
    return await B.handle_mention(msg)

def fresh_throttle(limit=4):
    B.mention_throttle = B.MentionThrottle(limit)

def fresh_memory(turns=10):
    B.conversations = B.Conversation(turns, 1800)

fresh_throttle(); fresh_memory()
calls.clear()
m = Msg(f"{MENTION} hello there")
check("mention handled", asyncio.run(run(m)) is True)
check("question carries the speaker, not the mention", calls == [("user1: hello there", [], [])])
check("reply sent", m.replies == ["answer"])

calls.clear(); fresh_memory()
check("bot author ignored", asyncio.run(run(Msg(f"{MENTION} hi", author_bot=True))) is False)
check("no API call for bot author", calls == [])

calls.clear()
check("@everyone ignored", asyncio.run(run(Msg(f"{MENTION} hi", mention_everyone=True))) is False)
check("no API call for @everyone", calls == [])

# The mentions array still isn't trusted - only a real reference counts.
calls.clear()
check("bare mentions-array entry ignored",
      asyncio.run(run(Msg("thanks!", mentions=[types.SimpleNamespace(id=BOT_ID)]))) is False)
check("no API call for a phantom mention", calls == [])

# --- replying to the bot continues the conversation without an @ ---
calls.clear(); fresh_throttle(); fresh_memory()
check("reply to bot handled",
      asyncio.run(run(Msg("what about the other one?", reference=ref(resolved=bot_message())))) is True)
check("reply forwarded", calls == [("user1: what about the other one?", [], [])])

calls.clear()
other = B.discord.Message.__new__(B.discord.Message)
other.author = types.SimpleNamespace(id=999, bot=False)
other.attachments = []
check("reply to someone else ignored",
      asyncio.run(run(Msg("ok", reference=ref(resolved=other)))) is False)
check("no API call for a reply to someone else", calls == [])

calls.clear()
check("unresolvable reference ignored",
      asyncio.run(run(Msg("ok", reference=ref(resolved=None, message_id=1)))) is False)

# bare mention -> canned reply, no spend
calls.clear()
fresh_throttle()
m = Msg(MENTION)
asyncio.run(run(m))
check("bare mention gets canned reply", m.replies == [B.NO_QUESTION_REPLY])
check("bare mention spends nothing", calls == [])

# image with no text -> default question
calls.clear()
fresh_throttle(); fresh_memory()
img = types.SimpleNamespace(url="https://cdn/x.png", filename="x.png", content_type="image/png")
asyncio.run(run(Msg(MENTION, attachments=[img])))
check("image-only uses default question",
      calls == [(f"user1: {B.DESCRIBE_IMAGE_QUESTION}", ["https://cdn/x.png"], [])])

# non-image attachments are not sent
calls.clear()
fresh_throttle(); fresh_memory()
doc = types.SimpleNamespace(url="https://cdn/x.pdf", filename="x.pdf", content_type="application/pdf")
asyncio.run(run(Msg(f"{MENTION} look", attachments=[doc])))
check("non-image attachment dropped", calls == [("user1: look", [], [])])

# --- images on the replied-to message are picked up ---
def bot_message_with(images=(), author_id=BOT_ID):
    m = B.discord.Message.__new__(B.discord.Message)
    m.author = types.SimpleNamespace(id=author_id, bot=author_id == BOT_ID)
    m.attachments = list(images)
    m.id = 501
    return m

calls.clear(); fresh_throttle(); fresh_memory()
old = bot_message_with([img], author_id=777)          # someone else's image
asyncio.run(run(Msg(f"{MENTION} where is this from", reference=ref(resolved=old))))
check("image taken from the replied-to message",
      calls == [("user1: where is this from", ["https://cdn/x.png"], [])])

calls.clear(); fresh_throttle(); fresh_memory()
own = types.SimpleNamespace(url="https://cdn/mine.png", filename="mine.png", content_type="image/png")
asyncio.run(run(Msg(f"{MENTION} compare", attachments=[own], reference=ref(resolved=old))))
check("own image first, replied-to image after",
      calls[0][1] == ["https://cdn/mine.png", "https://cdn/x.png"])

calls.clear(); fresh_throttle(); fresh_memory()
asyncio.run(run(Msg(f"{MENTION} again", attachments=[img], reference=ref(resolved=old))))
check("the same image isn't sent twice", calls[0][1] == ["https://cdn/x.png"])

# --- memory carries across messages, and is per channel ---
calls.clear(); fresh_throttle(); fresh_memory()
asyncio.run(run(Msg(f"{MENTION} first question")))
asyncio.run(run(Msg(f"{MENTION} follow up")))
check("second call sees the first exchange",
      [m["content"] for m in calls[1][2]] == ["user1: first question", "answer"])
asyncio.run(run(Msg(f"{MENTION} in another channel", channel_id=99)))
check("other channel starts empty", calls[2][2] == [])

# a reply continues the same channel's thread
asyncio.run(run(Msg("and that?", reference=ref(resolved=bot_message()))))
check("reply sees prior history", len(calls[3][2]) == 4)

# failures never enter history
calls.clear(); fresh_throttle(); fresh_memory()
answer_ok = False
asyncio.run(run(Msg(f"{MENTION} this one errors")))
answer_ok = True
asyncio.run(run(Msg(f"{MENTION} next")))
check("failed exchange not remembered", calls[1][2] == [])

# a different speaker is named in the shared history
calls.clear(); fresh_throttle(); fresh_memory()
asyncio.run(run(Msg(f"{MENTION} hi", author_id=1)))
asyncio.run(run(Msg(f"{MENTION} who said that", author_id=2)))
check("history names each speaker", calls[1][2][0]["content"] == "user1: hi")

# memory off
calls.clear(); fresh_throttle(); fresh_memory(turns=0)
asyncio.run(run(Msg(f"{MENTION} one")))
asyncio.run(run(Msg(f"{MENTION} two")))
check("0 turns keeps every message standalone", calls[1][2] == [])

# throttle
calls.clear()
fresh_throttle(limit=2)
msgs = [Msg(f"{MENTION} q{i}") for i in range(3)]
for msg in msgs:
    asyncio.run(run(msg))
check("throttle allows up to limit", len(calls) == 2)
check("throttled message reacts instead of replying",
      msgs[2].reactions == [B.RATE_LIMITED_EMOJI] and msgs[2].replies == [])
check("throttle is per user", asyncio.run(run(Msg(f"{MENTION} q", author_id=77))) is True)

# --- max price parsing: blank vs zero ---
import importlib
def reload_with(value):
    if value is None:
        os.environ.pop("OPENROUTER_MAX_PRICE", None)
    else:
        os.environ["OPENROUTER_MAX_PRICE"] = value
    return importlib.reload(B).OPENROUTER_MAX_PRICE

check("unset -> no ceiling", reload_with(None) is None)
check("blank -> no ceiling", reload_with("") is None)
check("zero -> real zero ceiling", reload_with("0") == 0.0)
check("value parsed", reload_with("5.5") == 5.5)
reload_with(None)

# --- the reverse-search tool ---
check("tool takes no parameters",
      B.FIND_SOURCE_TOOL["function"]["parameters"]["properties"] == {})
check("tool named for the model", B.FIND_SOURCE_TOOL["function"]["name"] == "find_image_source")

searched = []
async def fake_lookup(url):
    searched.append(url)
    return ([{"title": "Some Page", "link": "https://ex.com/a", "source": "ex.com",
              "thumbnail": None, "similarity": 92.5}], "SauceNAO", None)
B.lookup_source = fake_lookup

runner = B.SourceFinder("https://cdn/x.png", "tester")
call = {"function": {"name": "find_image_source"}}
out = asyncio.run(runner(call))
check("tool searches the image the bot has", searched == ["https://cdn/x.png"])
check("result carries the link", "https://ex.com/a" in out)
check("result carries the similarity", "92% similar" in out or "93% similar" in out)
check("top link recorded for the fallback", runner.top_link == "https://ex.com/a")

# one search per message - the model can't drain SerpApi's 100/month
out2 = asyncio.run(runner(call))
check("second call does not search again", searched == ["https://cdn/x.png"])
check("second call says so", "Already searched" in out2)

check("unknown tool name refused",
      "No such tool" in asyncio.run(B.SourceFinder("u", "t")({"function": {"name": "rm"}})))

async def no_matches(url): return ([], "SauceNAO", None)
B.lookup_source = no_matches
check("no matches told plainly",
      "No source found" in asyncio.run(B.SourceFinder("u", "t")(call)))

async def failed(url): return ([], "Google Lens", "quota exhausted")
B.lookup_source = failed
check("search error surfaced to the model",
      "couldn't run" in asyncio.run(B.SourceFinder("u", "t")(call)))

async def explodes(url): raise RuntimeError("boom")
B.lookup_source = explodes
check("tool exception does not escape",
      "didn't work" in asyncio.run(B.SourceFinder("u", "t")(call)))
check("no link recorded when the search fails", B.SourceFinder("u", "t").top_link is None)

# --- model selection ---
# load_dotenv() repopulates from the real .env on reload and only skips keys
# already present, so "unset" is exercised as blank - the same env_str branch.
def reload_models(text="", image="", base="openrouter/auto"):
    os.environ["OPENROUTER_TEXT_MODEL"] = text
    os.environ["OPENROUTER_IMAGE_MODEL"] = image
    os.environ["OPENROUTER_MODEL"] = base
    import importlib
    m = importlib.reload(B)
    return m.OPENROUTER_TEXT_MODEL, m.OPENROUTER_IMAGE_MODEL

check("blank text model falls back to auto", reload_models()[0] == "openrouter/auto")
check("blank image model falls back to auto", reload_models()[1] == "openrouter/auto")
check("both fall back to OPENROUTER_MODEL",
      reload_models(base="some/model") == ("some/model", "some/model"))
check("explicit models win",
      reload_models(text="text/one", image="image/one", base="some/model")
      == ("text/one", "image/one"))
check("one set, one blank",
      reload_models(image="image/only", base="some/model") == ("some/model", "image/only"))
reload_models()

# --- /forget clears the channel's memory ---
fresh_memory()
B.conversations.remember(10, "user", "something", now=0.0)
check("memory populated before forget", B.conversations.history(10, now=1.0) != [])
check("forget clears that channel", B.conversations.forget(10))
check("channel is empty afterwards", B.conversations.history(10, now=1.0) == [])
check("forget on an untouched channel reports nothing", not B.conversations.forget(11))
check("forget is registered as a command",
      "forget" in {c.name for c in B.bot.tree.get_commands()})

# --- the web search tool ---
check("web tool takes a query",
      list(B.SEARCH_WEB_TOOL["function"]["parameters"]["properties"]) == ["query"])
check("query is required", B.SEARCH_WEB_TOOL["function"]["parameters"]["required"] == ["query"])
check("web tool named for the model", B.SEARCH_WEB_TOOL["function"]["name"] == "search_web")

# arguments arrive as a JSON string the model wrote, so all of this is reachable
check("argument parsed", B.tool_argument('{"query": "cats"}', "query") == "cats")
check("argument trimmed", B.tool_argument('{"query": "  cats "}', "query") == "cats")
check("malformed JSON -> empty", B.tool_argument("{not json", "query") == "")
check("missing key -> empty", B.tool_argument('{"other": 1}', "query") == "")
check("non-string value -> empty", B.tool_argument('{"query": 5}', "query") == "")
check("JSON that isn't an object -> empty", B.tool_argument('["cats"]', "query") == "")
check("no arguments at all -> empty", B.tool_argument(None, "query") == "")

# --- daily budget ---
from datetime import date
day1, day2 = date(2026, 8, 16), date(2026, 8, 17)
budget = B.DailyBudget(2)
check("first spend allowed", budget.spend(day1))
check("second spend allowed", budget.spend(day1))
check("third spend refused", not budget.spend(day1))
check("next day resets", budget.spend(day2))
check("limit 0 never spends", not B.DailyBudget(0).spend(day1))

# --- flag parsing ---
for raw, expected in [("1", True), ("true", True), ("on", True), ("yes", True),
                      ("0", False), ("false", False), ("off", False), ("no", False)]:
    os.environ["FLAG_UNDER_TEST"] = raw
    check(f"{raw!r} -> {expected}", B.env_flag("FLAG_UNDER_TEST", not expected) is expected)
os.environ["FLAG_UNDER_TEST"] = ""
check("blank flag keeps the default", B.env_flag("FLAG_UNDER_TEST", True) is True)
os.environ.pop("FLAG_UNDER_TEST")
check("unset flag keeps the default", B.env_flag("FLAG_UNDER_TEST", False) is False)

# --- which tools get offered ---
B.WEB_SEARCH_ENABLED, B.WEB_SEARCH_DAILY_LIMIT = True, 10
B.PAGE_READ_ENABLED, B.PAGE_READ_PER_MESSAGE = True, 2
names = lambda tools: [t["function"]["name"] for t in tools.definitions]
check("image message offers all three tools",
      names(B.AgentTools("https://cdn/x.png", "t")) ==
      ["find_image_source", "search_web", "read_page"])
check("plain text message doesn't offer the image tool",
      names(B.AgentTools(None, "t")) == ["search_web", "read_page"])
check("a link offers the image tool too - the page may contain one",
      names(B.AgentTools(None, "t", has_links=True)) ==
      ["find_image_source", "search_web", "read_page"])
check("no image means no source finder", B.AgentTools(None, "t").finder is None)
check("no top link without a finder", B.AgentTools(None, "t").top_link is None)

B.WEB_SEARCH_ENABLED = False
check("disabled -> web tool withheld", names(B.AgentTools(None, "t")) == ["read_page"])
check("disabled still offers the image tool",
      names(B.AgentTools("https://cdn/x.png", "t")) == ["find_image_source", "read_page"])
B.WEB_SEARCH_ENABLED, B.WEB_SEARCH_DAILY_LIMIT = True, 0
check("zero daily limit -> web tool withheld", names(B.AgentTools(None, "t")) == ["read_page"])
B.WEB_SEARCH_DAILY_LIMIT = 10

B.PAGE_READ_ENABLED = False
check("page reading off -> tool withheld", names(B.AgentTools(None, "t")) == ["search_web"])
check("page reading off -> a link no longer implies an image",
      names(B.AgentTools(None, "t", has_links=True)) == ["search_web"])
B.PAGE_READ_ENABLED = True
B.PAGE_READ_PER_MESSAGE = 0
check("zero pages per message -> tool withheld", names(B.AgentTools(None, "t")) == ["search_web"])
B.PAGE_READ_PER_MESSAGE = 2

# --- dispatch, without touching the network ---
B.bot.session = None                      # lookup_web reads it before calling out
queries = []
async def fake_search(session, query, key, num=5):
    queries.append(query)
    return {"organic_results": [
        {"title": "Result one", "link": "https://ex.com/1", "snippet": "first"},
        {"title": "Result two", "link": "https://ex.com/2", "snippet": "second"},
    ]}
B.search_web = fake_search
B.web_budget = B.DailyBudget(10)

def call(name, arguments=None):
    return {"function": {"name": name, "arguments": arguments}}

tools = B.AgentTools(None, "tester")
out = asyncio.run(tools(call("search_web", '{"query": "when is python 3.15 out"}')))
check("web tool searches what the model asked", queries == ["when is python 3.15 out"])
check("results reach the model", "https://ex.com/1" in out and "Result two" in out)

# one search per message, whatever the model does with its rounds
out2 = asyncio.run(tools(call("search_web", '{"query": "again"}')))
check("second web search in one message refused", queries == ["when is python 3.15 out"])
check("second call says so", "Already searched" in out2)

queries.clear()
check("empty query spends nothing",
      "no query" in asyncio.run(B.AgentTools(None, "t")(call("search_web", "{}"))) and queries == [])

# the daily cap holds even for the first call of a message
B.web_budget = B.DailyBudget(0)
queries.clear()
out = asyncio.run(B.AgentTools(None, "t")(call("search_web", '{"query": "x"}')))
check("daily cap refuses the search", queries == [] and "allowance is used up" in out)
B.web_budget = B.DailyBudget(10)

B.WEB_SEARCH_ENABLED = False
check("disabled tool refuses if called anyway",
      "turned off" in asyncio.run(B.AgentTools(None, "t")(call("search_web", '{"query": "x"}'))))
B.WEB_SEARCH_ENABLED = True

check("unknown tool name refused by the dispatcher",
      "No such tool" in asyncio.run(B.AgentTools(None, "t")(call("rm"))))
check("image tool says so when there's no image",
      "no image to search" in asyncio.run(B.AgentTools(None, "t")(call("find_image_source"))))

# the image tool still routes through, and its link is still recovered
B.lookup_source = fake_lookup
searched.clear()
tools = B.AgentTools("https://cdn/x.png", "tester")
asyncio.run(tools(call("find_image_source")))
check("image tool dispatched to the finder", searched == ["https://cdn/x.png"])
check("top link exposed through the dispatcher", tools.top_link == "https://ex.com/a")

# --- how results are described to the model ---
described = B.describe_web_results("q", "the answer", [
    {"title": "T", "link": "https://ex.com/1", "snippet": "s", "date": "Oct 7, 2025"}])
check("query named first", described.splitlines()[0].startswith('Web search results for "q"'))
check("rules are in the first line, not the truncatable tail",
      "invent nothing" in described.splitlines()[0])
check("answer box surfaced", "the answer" in described)
check("date kept", "Oct 7, 2025" in described)
check("link kept", "https://ex.com/1" in described)

B.WEB_SEARCH_RESULTS = 2
many = B.describe_web_results("q", "", [
    {"title": f"T{i}", "link": f"https://ex.com/{i}", "snippet": ""} for i in range(6)])
check("result count capped", "https://ex.com/2" not in many)
check("results fit the tool-result cap", len(many) <= 2000)
B.WEB_SEARCH_RESULTS = 5

# --- lookup_web turns every failure into something the model can say ---
import web_search as W
def raiser(exc):
    async def fake(session, query, key, num=5): raise exc
    return fake

for exc, expected in [(W.WebNoResults("x"), "couldn't find anything"),
                      (W.WebBadQuery("x"), "no query"),
                      (W.WebQuotaExceeded("x"), "quota is used up"),
                      (W.WebAuthError("x"), "isn't configured"),
                      (W.WebSearchError("boom"), "couldn't run"),
                      (RuntimeError("boom"), "didn't work")]:
    B.search_web = raiser(exc)
    check(f"{type(exc).__name__} answered, not raised",
          expected in asyncio.run(B.lookup_web("q", "t")))

async def empty(session, query, key, num=5): return {}
B.search_web = empty
check("empty payload told plainly", "couldn't find anything" in asyncio.run(B.lookup_web("q", "t")))

async def answer_only(session, query, key, num=5):
    return {"answer_box": {"answer": "1991"}}
B.search_web = answer_only
check("an answer box alone is enough", "1991" in asyncio.run(B.lookup_web("q", "t")))

# --- the read_page tool ---
check("page tool takes a url",
      list(B.READ_PAGE_TOOL["function"]["parameters"]["properties"]) == ["url"])
check("url is required", B.READ_PAGE_TOOL["function"]["parameters"]["required"] == ["url"])
check("page tool named for the model", B.READ_PAGE_TOOL["function"]["name"] == "read_page")

fetched = []
def page_returning(page):
    async def fake(session, url, max_chars=3000):
        fetched.append(url)
        return page
    return fake

ARTICLE = {"url": "https://ex.com/a", "title": "A Headline", "description": "The standfirst",
           "image": "https://ex.com/hero.png", "text": "The body of the piece.", "truncated": False}
B.fetch_page = page_returning(ARTICLE)

tools = B.AgentTools(None, "tester", has_links=True)
out = asyncio.run(tools(call("read_page", '{"url": "https://ex.com/a"}')))
check("page tool fetches the link the model gave", fetched == ["https://ex.com/a"])
check("title reaches the model", "A Headline" in out)
check("body reaches the model", "The body of the piece." in out)

# a second read is allowed, a third isn't - each one costs prompt tokens
asyncio.run(tools(call("read_page", '{"url": "https://ex.com/b"}')))
capped = asyncio.run(tools(call("read_page", '{"url": "https://ex.com/c"}')))
check("two pages per message allowed", len(fetched) == 2)
check("third read refused", "as many links as I can open" in capped)

fetched.clear()
check("missing url spends nothing",
      "no link" in asyncio.run(B.AgentTools(None, "t")(call("read_page", "{}"))) and fetched == [])

B.PAGE_READ_ENABLED = False
check("disabled tool refuses if called anyway",
      "turned off" in asyncio.run(B.AgentTools(None, "t")(call("read_page", '{"url": "https://ex.com"}'))))
B.PAGE_READ_ENABLED = True

# --- a page's image chains into the reverse image search ---
B.lookup_source = fake_lookup
searched.clear()
tools = B.AgentTools(None, "tester", has_links=True)
check("no image before the page is read", tools.discovered_image == "")
asyncio.run(tools(call("read_page", '{"url": "https://ex.com/a"}')))
check("og:image remembered", tools.discovered_image == "https://ex.com/hero.png")
asyncio.run(tools(call("find_image_source")))
check("the page's image is what gets searched", searched == ["https://ex.com/hero.png"])
check("its top link is still recovered for the reply", tools.top_link == "https://ex.com/a")

# an attachment always wins - a link can't redirect the search away from it
searched.clear()
tools = B.AgentTools("https://cdn/mine.png", "tester", has_links=True)
asyncio.run(tools(call("read_page", '{"url": "https://ex.com/a"}')))
asyncio.run(tools(call("find_image_source")))
check("the attached image is searched, not the page's", searched == ["https://cdn/mine.png"])

# --- read_link turns every failure into something the model can say ---
import page_reader as PR
def page_raising(exc):
    async def fake(session, url, max_chars=3000): raise exc
    return fake

for exc, expected in [(PR.PageNotFound("x"), "dead"),
                      (PR.PageBlocked("x"), "refused"),
                      (PR.PageBadUrl("not a web address"), "Can't read that"),
                      (PR.PageNotText("it's a application/pdf"), "Can't read that"),
                      (PR.PageError("boom"), "Couldn't read that page"),
                      (RuntimeError("boom"), "didn't work")]:
    B.fetch_page = page_raising(exc)
    text, image = asyncio.run(B.read_link("https://ex.com/x", "t"))
    check(f"{type(exc).__name__} answered, not raised", expected in text and image == "")

B.fetch_page = page_raising(PR.PageIsImage("https://ex.com/x.png"))
text, image = asyncio.run(B.read_link("https://ex.com/x.png", "t"))
check("an image link is handed to the image search, not read as a page",
      image == "https://ex.com/x.png" and "find_image_source" in text)

B.fetch_page = page_returning({"url": "https://ex.com/e", "title": "", "description": "",
                               "image": "", "text": "", "truncated": False})
text, _ = asyncio.run(B.read_link("https://ex.com/e", "t"))
check("an empty page is reported as empty", "no readable text" in text)

# --- how a page is described to the model ---
described = B.describe_page(dict(ARTICLE, truncated=True))
check("url named first", described.splitlines()[0].startswith("Contents of https://ex.com/a"))
check("rules are in the first line, not the truncatable tail",
      "inventing nothing" in described.splitlines()[0])
# Anyone can post a link to a page they wrote, so the page's text is hostile input.
check("page text framed as quoted material, not instructions",
      "not instructions" in described.splitlines()[0])
check("title and summary carried", "A Headline" in described and "The standfirst" in described)
check("truncation admitted", "only the start" in described)
check("a page read fits the tool-result cap",
      B.PAGE_READ_MAX_CHARS + 300 <= __import__("chat_agent").MAX_TOOL_RESULT_CHARS)

# --- every call and every refusal reaches the log ---
import logging
logged = []
class Capture(logging.Handler):
    def emit(self, record): logged.append(record.getMessage())
B.log.addHandler(Capture())
B.log.setLevel(logging.INFO)
B.search_web = page_returning({"organic_results": [{"title": "T", "link": "https://ex.com/1"}]})
B.web_budget = B.DailyBudget(10)

tools = B.AgentTools(None, "asker in #general")
logged.clear()
asyncio.run(tools(call("search_web", '{"query": "how tall is nelson\'s column"}')))
check("the call is logged with the arguments the model wrote",
      any('search_web({"query": "how tall is nelson\'s column"})' in m for m in logged))
check("who asked is logged", any("asker in #general" in m for m in logged))
check("the outcome is logged", any("returned" in m and "search_web" in m for m in logged))

logged.clear()
asyncio.run(tools(call("search_web", '{"query": "twice"}')))
check("a refused call is logged with its reason",
      any("refused" in m and "already searched" in m for m in logged))

logged.clear()
asyncio.run(B.AgentTools(None, "asker")(call("read_page", "{}")))
check("a malformed call is logged", any("refused" in m and "no url" in m for m in logged))

logged.clear()
asyncio.run(B.AgentTools(None, "asker")(call("rm -rf", "{}")))
check("an invented tool name is logged",
      any("refused" in m and "no tool by that name" in m for m in logged))

# control characters in a model-written argument can't forge a log line
logged.clear()
asyncio.run(B.AgentTools(None, "asker")(call("read_page", '{"url": "http://x\nINFO forged"}')))
check("newlines in arguments are scrubbed", not any(m.endswith("INFO forged") for m in logged))

print(f"bot wiring: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
