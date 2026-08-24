"""Does a model actually answer about media, and does it still reach for a tool?

The catalogue can't tell you: a model that refuses and one that works advertise the
same input_modalities and the same tools support. Only trying it settles it, so run
this against any id before putting it in OPENROUTER_IMAGE_MODEL or
OPENROUTER_AUDIO_MODEL.

    .venv/Scripts/python.exe agentic/tools/probe_media.py
    .venv/Scripts/python.exe agentic/tools/probe_media.py <model> --mode image --trials 6

Modes: audio hears a spoken clip, image sees a picture, source asks where a picture
came from and looks for a find_image_source call. Costs a few cents. Refusals are
intermittent, so one trial per arm proves nothing.
"""
import argparse, asyncio, base64, json, os, struct, subprocess, sys, zlib
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
load_dotenv(REPO / ".env")
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("SERPAPI_KEY", "x")
import bot as B, chat_agent as ca

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
ARMS = {"both": (True, False), "tools": (True,), "notools": (False,)}
PHRASE = "The quick brown fox jumps over the lazy dog. Purple elephants are dancing on the roof."
HEARD = ("fox", "lazy dog", "elephant", "roof", "quick brown", "pangram")
SEEN = ("red", "green", "blue", "stripe", "band", "bar")
DENIED = ("can't listen", "cannot listen", "can't hear", "cannot hear", "unable to listen",
          "unable to hear", "can't process audio", "only look at images", "can't play",
          "can't see", "cannot see", "unable to see", "can't view", "no image")
STUB_IMAGE = "https://cdn.example/x.png"


def spoken_clip() -> Path:
    """Windows speech synthesis, so the words asked about are known in advance."""
    path = Path(__file__).with_name("probe_clip.wav")
    if path.exists():
        return path
    if sys.platform != "win32":
        sys.exit("No clip to send: pass one with --clip.")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech;"
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                    f"$s.SetOutputToWaveFile('{path}'); $s.Speak('{PHRASE}'); $s.Dispose()"],
                   check=True, capture_output=True)
    return path


def band_image() -> str:
    """Red, green and blue bands as a data URI. No host to 403 the fetcher."""
    size, rows = 120, []
    for y in range(size):
        pixel = [(220, 20, 20), (20, 200, 20), (20, 20, 220)][y * 3 // size]
        rows.append(b"\x00" + bytes(pixel) * size)
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows)))
           + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


def verdict(mode, text, called, known):
    if mode == "source":
        return "CALLED" if "find_image_source" in called else "NO TOOL"
    low = text.lower()
    words = HEARD if mode == "audio" else SEEN
    if known and any(w in low for w in words):
        return "HEARD" if mode == "audio" else "SAW"
    return "DENIED" if any(w in low for w in DENIED) else "UNCLEAR"


async def once(session, mode, payload_in, model, with_tools, known, ask=None):
    question = ask or ("alpha: where is this image from?" if mode == "source"
                       else "alpha: what is this")
    audio = [payload_in] if mode == "audio" else []
    images = [] if mode == "audio" else [payload_in]
    body = ca.build_request(B.AGENT_CONTEXT, question, model, B.OPENROUTER_MAX_TOKENS,
                            images, B.OPENROUTER_MAX_PRICE, B.MAX_CONTEXT_TOKENS, [], audio)
    if with_tools:
        body["tools"] = list(B.AgentTools(STUB_IMAGE, "alpha", False).definitions)
    async with session.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json=body,
    ) as response:
        result = await response.json()
    if result.get("error"):
        return "ERROR", 0, json.dumps(result["error"])[:110]
    message = result["choices"][0]["message"]
    text = (message.get("content") or "").strip()
    called = [(c.get("function") or {}).get("name", "?") for c in message.get("tool_calls") or []]
    for name in called:
        text += f"  [called {name}]"
    usage = result.get("usage") or {}
    media_tokens = (usage.get("prompt_tokens_details") or {}).get("audio_tokens") or 0
    return verdict(mode, text, called, known), media_tokens, text[:120].replace("\n", " ")


async def main(args):
    if args.mode == "audio":
        path = Path(args.clip) if args.clip else spoken_clip()
        fmt = path.suffix.lstrip(".").lower()
        payload_in = (base64.b64encode(path.read_bytes()).decode(),
                      B.AUDIO_FORMATS.get(fmt, fmt))
        print(f"{path.name}, {path.stat().st_size} bytes, sent as {payload_in[1]}\n")
    else:
        payload_in = band_image()
        print("120x120 red/green/blue bands, sent as a data URI\n")
    known = args.clip is None
    async with aiohttp.ClientSession() as session:
        for model in args.models:
            for with_tools in ARMS[args.arms]:
                print(f"{model} {'with tools' if with_tools else 'no tools'}")
                for trial in range(args.trials):
                    result, tokens, text = await once(
                        session, args.mode, payload_in, model, with_tools, known, args.ask
                    )
                    audio_note = f"audio_tokens={tokens:<5}" if args.mode == "audio" else ""
                    print(f"  {trial + 1}. {result:8} {audio_note}{text}")
                print()


parser = argparse.ArgumentParser()
parser.add_argument("models", nargs="*")
parser.add_argument("--mode", choices=("audio", "image", "source"), default="audio")
parser.add_argument("--clip", help="audio file to send instead of synthesised speech")
parser.add_argument("--trials", type=int, default=4)
parser.add_argument("--arms", choices=("both", "tools", "notools"), default="both")
parser.add_argument("--ask", help="question to send instead of the mode's default")
args = parser.parse_args()
args.models = args.models or list(
    B.OPENROUTER_AUDIO_MODEL if args.mode == "audio" else B.OPENROUTER_IMAGE_MODEL
)
asyncio.run(main(args))
