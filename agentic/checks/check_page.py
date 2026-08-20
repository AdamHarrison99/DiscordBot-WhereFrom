"""Offline checks for page_reader.py. No network: DNS and HTTP are both faked."""
import asyncio, socket, sys
from pathlib import Path

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import page_reader as P

ok, fail = 0, 0
def check(label, cond):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"  FAIL: {label}")


# --- URL validation ---
check("https accepted", P.validate_url("https://example.com/a") == "https://example.com/a")
check("whitespace trimmed", P.validate_url("  https://example.com  ") == "https://example.com")
check("discord's angle brackets stripped",
      P.validate_url("<https://example.com>") == "https://example.com")
for bad in ("", "example.com", "ftp://example.com", "file:///etc/passwd",
            "javascript:alert(1)", "http://"):
    try:
        P.validate_url(bad)
        check(f"{bad!r} rejected", False)
    except P.PageBadUrl:
        check(f"{bad!r} rejected", True)

# --- the address guard: the model takes the URL from a Discord message ---
def fake_dns(mapping):
    async def resolver(host, port):
        if host not in mapping:
            raise socket.gaierror(f"no such host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]
    P._getaddrinfo = resolver

PRIVATE = ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "0.0.0.0"]
for address in PRIVATE:
    fake_dns({"evil.test": address})
    try:
        asyncio.run(P.ensure_public("http://evil.test/x"))
        check(f"{address} refused", False)
    except P.PageBadUrl:
        check(f"{address} refused", True)

fake_dns({"example.com": "93.184.216.34"})
try:
    asyncio.run(P.ensure_public("https://example.com/x"))
    check("public address allowed", True)
except P.PageBadUrl:
    check("public address allowed", False)

fake_dns({})
try:
    asyncio.run(P.ensure_public("https://nowhere.invalid/x"))
    check("unresolvable host refused", False)
except P.PageBadUrl:
    check("unresolvable host refused", True)


# --- fake HTTP, so redirects and content types are exercised without a socket ---
class FakeResponse:
    def __init__(self, status=200, body=b"", content_type="text/html", headers=None, url=""):
        self.status, self._body, self.content_type = status, body, content_type
        self.headers, self.url = headers or {}, url
        self.content = self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def read(self, n=None): return self._body[:n] if n else self._body
    def get_encoding(self): return "utf-8"


class FakeSession:
    """Serves a queue of responses and records what was requested."""
    def __init__(self, *responses):
        self.responses, self.requested = list(responses), []
    def get(self, url, headers=None, timeout=None, allow_redirects=None):
        self.requested.append(url)
        response = self.responses.pop(0)
        response.url = response.url or url
        return response


PAGE = b"""<html><head><title>Example &amp; Co</title>
<meta property="og:description" content="A description">
<meta property="og:image" content="/img/hero.png">
<script>var junk = "do not read me";</script>
</head><body><nav>Home About</nav><p>First paragraph.</p>
<style>.x{color:red}</style><p>Second   paragraph.</p>
<footer>copyright</footer></body></html>"""

fake_dns({"example.com": "93.184.216.34"})
session = FakeSession(FakeResponse(body=PAGE))
page = asyncio.run(P.fetch_page(session, "https://example.com/a"))
check("title decoded", page["title"] == "Example & Co")
check("og:description picked up", page["description"] == "A description")
check("og:image made absolute", page["image"] == "https://example.com/img/hero.png")
check("body text kept", "First paragraph." in page["text"] and "Second paragraph." in page["text"])
check("script contents dropped", "do not read me" not in page["text"])
check("style contents dropped", "color:red" not in page["text"])
check("nav and footer chrome dropped",
      "Home About" not in page["text"] and "copyright" not in page["text"])
check("short page not marked truncated", page["truncated"] is False)

# --- redirects are followed by hand so every hop is re-checked ---
fake_dns({"a.test": "93.184.216.34", "b.test": "93.184.216.35"})
session = FakeSession(
    FakeResponse(status=302, headers={"Location": "https://b.test/final"}),
    FakeResponse(body=b"<html><body><p>Arrived</p></body></html>"),
)
page = asyncio.run(P.fetch_page(session, "https://a.test/start"))
check("redirect followed", "Arrived" in page["text"])
check("both hops requested", session.requested == ["https://a.test/start", "https://b.test/final"])

# the point of following by hand: a redirect into the private network is caught
fake_dns({"a.test": "93.184.216.34", "internal.test": "169.254.169.254"})
session = FakeSession(FakeResponse(status=302, headers={"Location": "http://internal.test/creds"}))
try:
    asyncio.run(P.fetch_page(session, "https://a.test/start"))
    check("redirect into a private address refused", False)
except P.PageBadUrl:
    check("redirect into a private address refused", True)

# relative redirects resolve against the current URL
fake_dns({"a.test": "93.184.216.34"})
session = FakeSession(
    FakeResponse(status=301, headers={"Location": "/moved"}),
    FakeResponse(body=b"<p>here</p>"),
)
asyncio.run(P.fetch_page(session, "https://a.test/old"))
check("relative redirect resolved", session.requested[1] == "https://a.test/moved")

session = FakeSession(*[FakeResponse(status=302, headers={"Location": "https://a.test/x"})
                        for _ in range(P.MAX_REDIRECTS + 1)])
try:
    asyncio.run(P.fetch_page(session, "https://a.test/loop"))
    check("redirect loop gives up", False)
except P.PageError as exc:
    check("redirect loop gives up", "circles" in str(exc))

# --- status and content type handling ---
for status, expected in [(404, P.PageNotFound), (410, P.PageNotFound), (403, P.PageBlocked),
                         (401, P.PageBlocked), (429, P.PageBlocked), (500, P.PageError)]:
    try:
        asyncio.run(P.fetch_page(FakeSession(FakeResponse(status=status)), "https://a.test/x"))
        check(f"{status} raises", False)
    except expected:
        check(f"{status} -> {expected.__name__}", True)

try:
    asyncio.run(P.fetch_page(FakeSession(FakeResponse(content_type="image/png")), "https://a.test/x.png"))
    check("image link raises PageIsImage", False)
except P.PageIsImage as exc:
    check("image link raises PageIsImage", True)
    check("image URL carried on the exception", exc.url == "https://a.test/x.png")

try:
    asyncio.run(P.fetch_page(FakeSession(FakeResponse(content_type="application/pdf")), "https://a.test/x"))
    check("pdf refused", False)
except P.PageNotText:
    check("pdf refused", True)

check("every failure is a PageError",
      all(issubclass(c, P.PageError) for c in
          (P.PageBadUrl, P.PageIsImage, P.PageNotText, P.PageBlocked, P.PageNotFound)))

# --- size caps ---
long_page = ("<p>" + "word " * 5000 + "</p>").encode()
page = asyncio.run(P.fetch_page(FakeSession(FakeResponse(body=long_page)), "https://a.test/x", 500))
check("long page truncated", page["truncated"] is True)
check("truncated to the limit", len(page["text"]) <= 500)
check("cut at a word boundary", not page["text"].endswith("wor"))
check("byte cap below what a huge page would send", P.MAX_BYTES <= 1_000_000)

# --- text helpers ---
check("whitespace collapsed", P.collapse("a   b\t\tc") == "a b c")
check("blank runs collapsed", P.collapse("a\n\n\n\n\nb") == "a\n\nb")
check("short text not truncated", P.truncate("hello", 10) == ("hello", False))
check("no word boundary -> hard cut", P.truncate("x" * 20, 10) == ("x" * 10, True))

# --- site chrome before <main> is skipped, or the budget goes on menus ---
CHROME = "<div id=head>Jump to content Sign in Menu Language</div>"
BODY = "<main><p>" + ("The actual article. " * 20) + "</p></main>"
page = P.read_html("https://a.test/", f"<html><body>{CHROME}{BODY}</body></html>", 3000)
check("chrome before <main> dropped", "Jump to content" not in page["text"])
check("main content kept", page["text"].startswith("The actual article."))

page = P.read_html("https://a.test/", f"<html><body>{CHROME}{BODY.replace('main', 'article')}</body></html>", 3000)
check("<article> works the same", "Jump to content" not in page["text"])

page = P.read_html(
    "https://a.test/",
    f"<html><body>{CHROME}<div role=main><p>{'Sphinx body. ' * 20}</p></div></body></html>", 3000)
check("role=main works the same - it's what Sphinx emits", "Jump to content" not in page["text"])

# a sidebar marked up as <article> mustn't throw the real text away
page = P.read_html(
    "https://a.test/",
    f"<html><body><p>{'The real text. ' * 20}</p><article>Related links</article></body></html>", 3000)
check("a too-small <article> is ignored", "The real text." in page["text"])

# --- malformed HTML doesn't crash the parser ---
page = P.read_html("https://a.test/", "<html><p>unclosed <b>bold <div>text", 3000)
check("malformed HTML still yields text", "unclosed" in page["text"])
check("empty document is empty, not an error", P.read_html("https://a.test/", "", 3000)["text"] == "")

# --- reddit: any thread is JSON if you ask for it ---
check("thread rewritten to .json",
      P.as_reddit_json("https://www.reddit.com/r/python/comments/abc/title/")
      == "https://www.reddit.com/r/python/comments/abc/title.json")
check("old.reddit rewritten",
      P.as_reddit_json("https://old.reddit.com/r/x/comments/abc/t").endswith(".json"))
check("already .json left alone",
      P.as_reddit_json("https://www.reddit.com/r/x/comments/abc/t.json").count(".json") == 1)
check("subreddit front page left alone",
      P.as_reddit_json("https://www.reddit.com/r/python/") == "https://www.reddit.com/r/python/")
check("other hosts left alone",
      P.as_reddit_json("https://example.com/comments/x") == "https://example.com/comments/x")

REDDIT = [
    {"kind": "Listing", "data": {"children": [{"data": {
        "title": "What is this bird", "selftext": "Found it in my garden.",
        "score": 412, "num_comments": 3, "post_hint": "image",
        "url_overridden_by_dest": "https://i.redd.it/x.jpg"}}]}},
    {"kind": "Listing", "data": {"children": [
        {"data": {"author": "birder", "body": "That's a jay.", "score": 88}},
        {"data": {"author": "other", "body": "Agreed.", "score": 4}},
        {"kind": "more"},
    ]}},
]
thread = P.read_reddit("https://reddit.test/x", REDDIT, 3000)
check("post title and score", thread["title"] == "What is this bird (412 points, 3 comments)")
check("post body kept", "Found it in my garden." in thread["text"])
check("comments kept with authors", "birder (88): That's a jay." in thread["text"])
check("commentless entries skipped", "more" not in thread["text"])
check("image post exposes its image", thread["image"] == "https://i.redd.it/x.jpg")
check("reddit JSON detected", P.read_json("https://reddit.test/x", __import__("json").dumps(REDDIT))["title"].startswith("What is this bird"))

check("plain JSON passed through as text",
      "\"a\"" in P.read_json("https://a.test/x", '{"a": 1}')["text"])
check("unparseable JSON body kept as text",
      P.read_json("https://a.test/x", "not json at all")["text"] == "not json at all")

print(f"page reader: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
