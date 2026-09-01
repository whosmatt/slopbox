"""Central configuration, read once from the environment."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))

QWEN_API_KEY = os.getenv("QWEN_API_KEY", "")
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "https://inference.hetzner.com/api/v1")
QWEN_MODEL = os.getenv("QWEN_MODEL", "Qwen/Qwen3.6-35B-A3B-FP8")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

MAX_STEPS = int(os.getenv("MAX_STEPS", "14"))
# Qwen3.6 is a reasoning model and will happily spend thousands of tokens
# deliberating. "low" keeps it deliberating usefully without stalling the UI.
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "12000"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "240000"))
MAX_CSS_BYTES = int(os.getenv("MAX_CSS_BYTES", "100000"))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "600"))

RATE_LIMIT_COUNT = int(os.getenv("RATE_LIMIT_COUNT", "5"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "120"))

# How many published CSS versions to retain. Oldest are pruned; nothing else
# is ever written to disk, so the data dir has a hard size ceiling.
HISTORY_KEEP = int(os.getenv("HISTORY_KEEP", "20"))

# Peers whose X-Forwarded-For header may be believed. Empty means trust none:
# a header alone must never be able to impersonate a local caller or dodge the
# rate limiter. Set to your reverse proxy's address when you put one in front.
TRUSTED_PROXIES = tuple(
    p.strip() for p in os.getenv("TRUSTED_PROXIES", "").split(",") if p.strip()
)

# Internal URL the validator/screenshotter points its browser at.
#
# This is always loopback plus our own port - Chromium runs inside this
# container, so the public hostname is irrelevant. The port is learned from the
# ASGI scope on the first request (uvicorn fills it from the bound socket), so
# normally nothing needs configuring. PORT is only the fallback for the window
# before any request has arrived.
#
# Deliberately NOT derived from request.base_url or the Host header: those are
# attacker-controlled, and this URL is where we send a browser whose rendering
# is screenshotted and fed to the model. A forged Host would turn that into an
# SSRF with a prompt-injection payload attached.
SELF_URL_OVERRIDE = os.getenv("SELF_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8000"))

_observed_port = None


def note_server_port(port):
    """Record the port uvicorn actually bound, from the ASGI scope."""
    global _observed_port
    if isinstance(port, int) and 0 < port < 65536:
        _observed_port = port


def self_url():
    return SELF_URL_OVERRIDE or ("http://127.0.0.1:%d" % (_observed_port or PORT))

# Viewports the validator must pass. (label, width, height)
VIEWPORTS = [("desktop", 1280, 800), ("mobile", 390, 844)]

# Seconds after load at which the input must still be usable. Guards against
# animations that park the field off-screen after a delay.
SAMPLE_TIMES = [0.0, 0.6, 1.8, 3.2]

# url() targets the sanitizer permits. Empty-ish by default: only inline raster
# data URIs, so custom CSS can never make an outbound request.
ALLOWED_URL_PREFIXES = tuple(
    p for p in os.getenv("ALLOWED_URL_PREFIXES", "").split(",") if p.strip()
) or (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/gif;base64,",
    "data:image/webp;base64,",
)
