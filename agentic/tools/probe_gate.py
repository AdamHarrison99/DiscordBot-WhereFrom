"""Does a gate model actually score your criteria, or only check whether it was addressed?

The ambient gate lives or dies on the model behind it, and the catalogue can't tell
you which kind you have. This drives the real path - fake Discord messages through
to_record, the real buffer, the real run_ambient - so what reaches the model is what
production sends, and the score read back is the one the log line carries.

    .venv/Scripts/python.exe agentic/tools/probe_gate.py
    .venv/Scripts/python.exe agentic/tools/probe_gate.py <model> <model> --trials 5
    .venv/Scripts/python.exe agentic/tools/probe_gate.py --show

Uses judge_template.md, or whatever JUDGE_TEMPLATE_FILE points at. The threshold is
raised out of reach first, so a scenario is scored but never answered - the grid
costs gate calls only. Prints a score per scenario per trial, then whether the model
separates what should be answered from what should be ignored. A model scoring
everything 0 and one scoring everything 90 are equally useless. About a cent a model.
"""
import argparse, asyncio, logging, os, sys, types
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
load_dotenv(REPO / ".env")
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("SERPAPI_KEY", "x")
import ambient as A, bot as B

CHANNEL, BOT_ID = 1, 900
GATE_LINE = "Ambient gate in %s: %d/%d, target %s, $%.6f via %s - %s"


class Channel:
    id, nsfw = CHANNEL, False
    def typing(self):
        class T:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
        return T()
    async def send(self, content, **kw): pass
    def get_partial_message(self, mid):
        class P:
            id = mid
            async def reply(s, content, **kw): pass
        return P()


CHAN = Channel()


class Attachment:
    def __init__(self, filename, content_type=None):
        self.filename, self.content_type = filename, content_type
        self.url = "https://raw.githubusercontent.com/python/cpython/main/PC/icons/py.png"


class Msg:
    """What discord.py hands to on_message, reduced to what to_record reads."""
    def __init__(self, text, who=1, is_bot=False, mid=1, attachments=()):
        self.content = self.clean_content = text
        self.author = types.SimpleNamespace(
            id=who, bot=is_bot, display_name="thisbot" if is_bot else "user" + str(who))
        self.attachments = list(attachments)
        self.channel, self.id, self.guild = CHAN, mid, object()


# Invented, never anybody's real messages. "speak" means a reply would be welcome.
SCENARIOS = (
    ("addressed", "speak", lambda: [
        Msg("does anyone know a good pizza place near the station"),
        Msg("wherefrom what do you reckon", who=2, mid=2)]),
    ("image asked", "speak", lambda: [
        Msg("found this on my camera roll, any idea where it came from",
            attachments=[Attachment("photo.png")])]),
    ("joke setup", "speak", lambda: [
        Msg("i have been awake for thirty one hours"),
        Msg("at this point you are legally a raccoon", who=2, mid=2)]),
    ("open question", "speak", lambda: [
        Msg("wait how do submarines even get fresh water"),
        Msg("no idea honestly", who=2, mid=2)]),
    ("chatter", "silent", lambda: [
        Msg("ok heading out, see you tomorrow"),
        Msg("safe travels", who=2, mid=2)]),
    ("noise", "silent", lambda: [
        Msg("testing testing one two three")]),
    ("bot just spoke", "silent", lambda: [
        Msg("so what happened with the car"),
        Msg("sounds like the alternator, they are cheap to replace",
            who=BOT_ID, is_bot=True, mid=2),
        Msg("yeah probably", who=2, mid=3)]),
    ("private aside", "silent", lambda: [
        Msg("user2 did you send that form off yet"),
        Msg("doing it tonight", who=2, mid=2)]),
)


class GateLog(logging.Handler):
    """The score as the log line reports it, so the probe reads what you read."""
    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        if record.msg == GATE_LINE:
            _, score, _, target, cost, model, reason = record.args
            self.seen.append((score, target, cost, model, reason))
        elif record.msg.startswith("Ambient gate failed") or record.msg.startswith("Ambient: quiet"):
            self.seen.append((None, None, 0.0, None, record.getMessage()))


def summarise(scores):
    """A gate is only useful if it separates the two groups and uses the middle."""
    speak = [s for want, s in scores if want == "speak"]
    silent = [s for want, s in scores if want == "silent"]
    if not speak or not silent:
        return "no data"
    gap = min(speak) - max(silent)
    values = sorted({s for _, s in scores})
    verdict = "separates" if gap > 0 else "overlaps"
    if len(values) < 3:
        verdict = "flat"
    return (verdict + ": speak " + str(min(speak)) + "-" + str(max(speak))
            + ", silent " + str(min(silent)) + "-" + str(max(silent))
            + ", gap " + format(gap, "+d") + ", " + str(len(values)) + " distinct " + str(values))


async def probe(model, trials, tap):
    """One model over the whole grid. A dead id ends its own row, not the run."""
    print(model)
    B.AMBIENT_GATE_MODEL = (model,)
    scores, spend = [], 0.0
    for name, want, build in SCENARIOS:
        # A fresh buffer and fresh limits, so no scenario is judged against the last.
        B.ambient_buffer = A.ChannelBuffer(B.AMBIENT_BUFFER_MESSAGES, 600)
        B.ambient_limits = A.AmbientLimits((CHANNEL,), 0)
        marks, first = [], ""
        for _ in range(trials):
            for message in build():
                B.observe_ambient(message)
            tap.seen.clear()
            await B.run_ambient(CHAN)
            if not tap.seen:
                print("  " + name.ljust(15) + " no gate call - refused before the model")
                return scores, spend
            score, _, cost, served, reason = tap.seen[-1]
            if score is None:
                print("  " + name.ljust(15) + " " + reason)
                return scores, spend
            marks.append(score)
            scores.append((want, score))
            spend += cost
            first = first or reason + " [" + str(served) + "]"
        print("  " + name.ljust(15) + " " + want.ljust(7) + " "
              + " ".join(str(s).rjust(3) for s in marks) + "   " + first[:52])
    return scores, spend


async def main(args):
    B.bot._connection.user = types.SimpleNamespace(id=BOT_ID, bot=True)
    B.AMBIENT_ENABLED = True
    B.AMBIENT_CHANNELS = (CHANNEL,)
    B.AMBIENT_STRICT_CONTENT = False
    # Out of reach, so a scenario is scored and never answered.
    B.AMBIENT_THRESHOLD = 101
    if B.JUDGE_PROMPTS is None:
        sys.exit("No usable gate prompt file.")
    tap = GateLog()
    B.log.addHandler(tap)
    if args.show:
        records = [B.to_record(m) for m in SCENARIOS[0][2]()]
        print(A.build_judge_prompt(records, template=B.JUDGE_PROMPTS.template, bot_id=BOT_ID))
        return
    async with aiohttp.ClientSession() as session:
        B.bot.session = session
        for model in args.models:
            scores, spend = await probe(model, args.trials, tap)
            print("  -> " + summarise(scores))
            print("  -> $" + format(spend, ".4f") + "\n")


parser = argparse.ArgumentParser()
parser.add_argument("models", nargs="*")
parser.add_argument("--trials", type=int, default=3)
parser.add_argument("--show", action="store_true", help="print one built prompt and stop")
args = parser.parse_args()
args.models = args.models or list(B.AMBIENT_GATE_MODEL)
asyncio.run(main(args))
