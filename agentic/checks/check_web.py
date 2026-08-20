"""Offline checks for web_search.py. No network, no API key."""
import asyncio, sys
from pathlib import Path

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import web_search as W

ok, fail = 0, 0
def check(label, cond):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"  FAIL: {label}")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self, content_type=None): return self.payload


class FakeSession:
    """Records the params so the request contract is checkable without a call."""
    def __init__(self, payload, status=200):
        self.payload, self.status, self.params = payload, status, None
    def get(self, url, params=None, timeout=None):
        self.params = params
        return FakeResponse(self.payload, self.status)


def run(payload, status=200, query="python release date", num=5):
    session = FakeSession(payload, status)
    result = asyncio.run(W.search_web(session, query, "k", num))
    return session, result


# --- query validation ---
check("query trimmed", W.validate_query("  cats  ") == "cats")
check("query capped", len(W.validate_query("x" * 500)) == W.MAX_QUERY_CHARS)
for empty in ("", "   ", None):
    try:
        W.validate_query(empty)
        check(f"empty query {empty!r} rejected", False)
    except W.WebBadQuery:
        check(f"empty query {empty!r} rejected", True)

# --- request contract ---
session, _ = run({"organic_results": []}, num=3)
check("google engine requested", session.params["engine"] == "google")
check("query sent as q", session.params["q"] == "python release date")
check("result count passed through", session.params["num"] == 3)
check("safe search on", session.params["safe"] == "active")
check("key sent", session.params["api_key"] == "k")

# --- error classification, all reported inside the payload ---
cases = [
    ("Google hasn't returned any results for this query.", 200, W.WebNoResults),
    ("No results found", 200, W.WebNoResults),
    ("Your account has run out of searches.", 200, W.WebQuotaExceeded),
    ("Monthly quota exceeded", 200, W.WebQuotaExceeded),
    ("rate limited", 429, W.WebQuotaExceeded),
    ("Invalid API key", 200, W.WebAuthError),
    ("nope", 401, W.WebAuthError),
    ("something else broke", 200, W.WebSearchError),
]
for message, status, expected in cases:
    try:
        run({"error": message}, status)
        check(f"{message!r} raises", False)
    except expected:
        check(f"{message!r} -> {expected.__name__}", True)
    except W.WebSearchError as exc:
        check(f"{message!r} -> {expected.__name__}, got {type(exc).__name__}", False)

check("every error is a WebSearchError",
      all(issubclass(c, W.WebSearchError)
          for c in (W.WebNoResults, W.WebQuotaExceeded, W.WebAuthError, W.WebBadQuery)))

# a non-200 with no error field still fails
try:
    run({}, 500)
    check("bare 500 raises", False)
except W.WebSearchError:
    check("bare 500 raises", True)

# a non-dict body is a failure, not a crash
class OddSession(FakeSession):
    def get(self, url, params=None, timeout=None): return FakeResponse(["nope"])
try:
    asyncio.run(W.search_web(OddSession(None), "q", "k"))
    check("non-dict payload raises", False)
except W.WebSearchError:
    check("non-dict payload raises", True)

# --- normalising organic results ---
payload = {
    "organic_results": [
        {"title": "Python 3.14", "link": "https://ex.com/a", "snippet": "Released...",
         "source": "example.com", "date": "Oct 7, 2025"},
        {"title": "No link here", "snippet": "orphan"},
        {"link": "https://ex.com/c", "displayed_link": "ex.com › c"},
    ]
}
results = W.normalize_results(payload)
check("linkless results dropped", len(results) == 2)
check("first result kept whole",
      results[0] == {"title": "Python 3.14", "link": "https://ex.com/a",
                     "snippet": "Released...", "source": "example.com", "date": "Oct 7, 2025"})
check("missing title filled in", results[1]["title"] == "Untitled result")
check("missing snippet is empty, not None", results[1]["snippet"] == "")
check("displayed_link used as the source", results[1]["source"] == "ex.com › c")
check("no organic_results -> empty list", W.normalize_results({}) == [])

# --- the answer box, when Google has one ---
check("answer preferred", W.direct_answer({"answer_box": {"answer": "42"}}) == "42")
check("result used next", W.direct_answer({"answer_box": {"result": "43"}}) == "43")
check("snippet used after that", W.direct_answer({"answer_box": {"snippet": "44"}}) == "44")
check("blank answer skipped for the snippet",
      W.direct_answer({"answer_box": {"answer": "   ", "snippet": "real"}}) == "real")
check("knowledge graph is the fallback",
      W.direct_answer({"knowledge_graph": {"description": "a language"}}) == "a language")
check("answer box beats the graph",
      W.direct_answer({"answer_box": {"answer": "box"},
                       "knowledge_graph": {"description": "graph"}}) == "box")
check("nothing there -> empty string", W.direct_answer({}) == "")
check("non-dict answer_box ignored", W.direct_answer({"answer_box": ["a"]}) == "")
check("non-string answer ignored", W.direct_answer({"answer_box": {"answer": 42}}) == "")

print(f"web search: {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
