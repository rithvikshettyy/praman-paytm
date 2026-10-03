"""Central configuration. Every tunable lives here, read once at import.

Ported from the earlier Praman backend. Supabase, Clerk, news and poller
settings were dropped: this build is one process on a local store, with no
accounts.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolved from this file, never the working directory, so the server and the
# tests find the same files whichever folder they are started from.
BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"
LADDERS_DIR = DATA_DIR / "ladders"
BILL_HEADS_PATH = DATA_DIR / "bill_heads.yaml"
CHECKLISTS_DIR = DATA_DIR / "checklists"
CORPUS_DIR = DATA_DIR / "corpus"  # RAG sources + sources.yaml
RAG_INDEX_DIR = DATA_DIR / "index"  # git-ignored Chroma index, rebuilt by `python -m app.rag.ingest`
EVAL_DIR = DATA_DIR / "eval"
FIXTURES_DIR = BACKEND_DIR / "tests" / "fixtures"
LOCAL_DIR = BACKEND_DIR / ".local"  # git-ignored: the SQLite store and any kept originals

load_dotenv(BACKEND_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --- Store ------------------------------------------------------------------
# MongoDB holds the cases, the chats and the text of her documents.
# Local: docker run -d -p 27017:27017 mongo:7
MONGO_URI = os.environ.get("MONGO_URI", "").strip() or "mongodb://localhost:27017"
MONGO_DB = os.environ.get("MONGO_DB", "").strip() or "praman"
# Originals land here only when the user consents to keep them (C7).
ORIGINALS_DIR = LOCAL_DIR / "originals"
# The website's words in each language, translated once by Sarvam and reused (git-ignored).
PAGE_TRANSLATIONS_PATH = Path(os.environ.get("PRAMAN_PAGE_TRANSLATIONS", LOCAL_DIR / "page_translations.json"))

# --- Extraction -------------------------------------------------------------
# A field read with less confidence than this is asked, never assumed.
CONFIDENCE_GATE = _float("CONFIDENCE_GATE", 0.8)
# Demo safety net: serve tests/fixtures/<type>_demo.json instead of calling
# Doc AI. Results are marked source="fixture" and carry an example label.
USE_DOC_FIXTURES = _bool("USE_DOC_FIXTURES", False)
DOC_AI_TIMEOUT_SECONDS = _int("DOC_AI_TIMEOUT_SECONDS", 180)
DOC_AI_POLL_SECONDS = _int("DOC_AI_POLL_SECONDS", 3)


# --- Sarvam -----------------------------------------------------------------
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

# One env var for the chat model so it can be swapped in a single line.
# The SDK currently types `model` as Literal["sarvam-105b"]; sarvam-m and
# sarvam-30b are deprecated and rejected by the Chat Completions API.
# sarvam-105b-conversations answers without a long hidden deliberation: about a second where
# sarvam-105b took 40-70s on a long policy and often ran out of tokens mid-thought (empty reply).
CHAT_MODEL = os.environ.get("SARVAM_CHAT_MODEL", "sarvam-105b-conversations")

# Sarvam serves open-weight models (glm5.2, gemma4) on /v2/chat/completions,
# which the SDK does not type, so those go over a direct HTTP transport. /v2 is
# beta and key-whitelisted: an un-whitelisted key gets
#   "This endpoint is currently in beta and not available"
# on every call, so clients/sarvam.py falls back to CHAT_MODEL automatically.
ANALYSIS_MODEL = os.environ.get("SARVAM_ANALYSIS_MODEL", "sarvam-105b")
V2_MODELS = {"glm5.2", "gemma4"}
SARVAM_BASE_URL = os.environ.get("SARVAM_BASE_URL", "https://api.sarvam.ai")
TRANSLATE_MODEL = os.environ.get("SARVAM_TRANSLATE_MODEL", "sarvam-translate:v1")
# mayura:v1 handles code-mixed / colloquial better; used for chat-style text.
TRANSLATE_MODEL_COLLOQUIAL = os.environ.get("SARVAM_TRANSLATE_MODEL_COLLOQUIAL", "mayura:v1")
STT_MODEL = os.environ.get("SARVAM_STT_MODEL", "saaras:v3")
TTS_MODEL = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.environ.get("SARVAM_TTS_SPEAKER", "priya")

SARVAM_TIMEOUT = _int("SARVAM_TIMEOUT_SECONDS", 120)

# Hard ceiling the API enforces per subscription tier. The starter tier rejects
# any request above 4096 with a 400, so every call is clamped to this rather
# than discovering the limit at runtime. Raise it if the plan is upgraded.
SARVAM_MAX_TOKENS_CEILING = _int("SARVAM_MAX_TOKENS_CEILING", 4096)

# Doc AI is documented at 10 requests/minute. The limiter is deliberately set
# one below that so a burst of concurrent uploads degrades into queuing rather
# than a wall of 429s.
DOC_AI_RATE_LIMIT_PER_MIN = _int("DOC_AI_RATE_LIMIT_PER_MIN", 9)
# Doc AI caps a single job at 10 pages; policy wordings routinely run longer,
# so ingestion always splits before submitting.
DOC_AI_MAX_PAGES_PER_JOB = _int("DOC_AI_MAX_PAGES_PER_JOB", 10)

# --- Uploads ----------------------------------------------------------------
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_BYTES", 200 * 1024 * 1024)
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/tiff",
}

# --- RAG (intent=question only) ---------------------------------------------
# "default": Chroma's built-in MiniLM ONNX model (downloaded once, on first use).
# "hashing": offline lexical embeddings, no download; what the tests use.
RAG_EMBEDDINGS = os.environ.get("RAG_EMBEDDINGS", "default").strip().lower()
RAG_TOP_K = _int("RAG_TOP_K", 6)
RAG_CHUNK_CHARS = _int("RAG_CHUNK_CHARS", 2500)
RAG_CHUNK_OVERLAP = _int("RAG_CHUNK_OVERLAP", 300)
# News layer: headlines and summaries from an allowlist of feeds (data/news_sources.yaml), refreshed in
# the background every NEWS_REFRESH_MINUTES (0 = off). Never written to the fact sheet or the engine.
NEWS_SOURCES_FILE = DATA_DIR / "news_sources.yaml"
NEWS_REFRESH_MINUTES = _int("NEWS_REFRESH_MINUTES", 0)
NEWS_MAX_AGE_DAYS = _int("NEWS_MAX_AGE_DAYS", 180)  # older items are removed at the next refresh
NEWS_MAX_PASSAGES = _int("NEWS_MAX_PASSAGES", 2)  # at most this many news passages in one answer's sources

# --- Respondents ------------------------------------------------------------
# Legal name drafts are addressed to when the distributor owes the answer.
# Configuration, not code: the product itself never carries a partner's name.
DISTRIBUTOR_LEGAL_NAME = os.environ.get("DISTRIBUTOR_LEGAL_NAME", "").strip()
# How the Distributor Console's headline names the distributor: "Of N cases, X needed <this>."
DISTRIBUTOR_SHORT_NAME = os.environ.get("DISTRIBUTOR_SHORT_NAME", "").strip() or "the distributor"

# --- n8n (delivery and follow-up workflows) ----------------------------------
# Praman decides what goes to whom; n8n handles how and when it is delivered. Both stay blank
# until a workflow exists: with no URL, sending is refused and nothing leaves the process.
N8N_DISPATCH_URL = os.environ.get("N8N_DISPATCH_URL", "").strip()
# Shared secret in the x-praman-secret header, both ways. Callbacks are refused without it.
N8N_SECRET = os.environ.get("N8N_SECRET", "").strip()
# Where n8n reaches this API (a tunnel in development). Sent to the workflow as callback_base.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
N8N_TIMEOUT_SECONDS = _int("N8N_TIMEOUT_SECONDS", 15)
# Demo only: pretend this many days have passed when n8n asks whether a response window ended, so a
# live demo does not wait two weeks. 0 (the default) is the real calendar.
N8N_DEMO_DAYS_AHEAD = _int("N8N_DEMO_DAYS_AHEAD", 0)
# Premium reminders: the n8n workflow that waits for each date, the address it emails (until the chat
# asks for one; blank = reminders off), and how many days before the due date to remind.
N8N_REMINDER_URL = os.environ.get("N8N_REMINDER_URL", "").strip()
REMINDER_EMAIL = os.environ.get("REMINDER_EMAIL", "").strip()
REMINDER_DAYS_BEFORE = tuple(sorted({int(d) for d in os.environ.get("REMINDER_DAYS_BEFORE", "7,1").split(",") if d.strip().isdigit()}, reverse=True))

# --- App --------------------------------------------------------------------
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

SUPPORTED_LANGUAGES = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "bn-IN": "Bengali",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "mr-IN": "Marathi",
    "gu-IN": "Gujarati",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "pa-IN": "Punjabi",
    "od-IN": "Odia",
}
DEFAULT_LANGUAGE = "en-IN"


def validate() -> list[str]:
    """Return a list of human-readable configuration problems (empty if fine)."""
    problems = []
    if not SARVAM_API_KEY:
        problems.append("SARVAM_API_KEY is not set - every Sarvam call will fail.")
    return problems

# --- Phone calls (Sarvam voice agent, built in the Sarvam dashboard) ---------------------------
# The agent holds the conversation; it calls this backend through an HTTP tool (see voice/README.md) and
# posts a webhook when a call ends. Blank ids = outbound calls off; blank VOICE_AGENT_SECRET = the tool
# and webhook endpoints refuse everything.
VOICE_AGENT_SECRET = os.environ.get("VOICE_AGENT_SECRET", "").strip()
SARVAM_VOICE_API_KEY = os.environ.get("SARVAM_VOICE_API_KEY", "").strip() or SARVAM_API_KEY
SARVAM_VOICE_BASE_URL = os.environ.get("SARVAM_VOICE_BASE_URL", "https://apps.sarvam.ai/api/outbounds").strip().rstrip("/")
SARVAM_VOICE_ORG_ID = os.environ.get("SARVAM_VOICE_ORG_ID", "").strip()
SARVAM_VOICE_WORKSPACE_ID = os.environ.get("SARVAM_VOICE_WORKSPACE_ID", "").strip()
SARVAM_VOICE_APP_ID = os.environ.get("SARVAM_VOICE_APP_ID", "").strip()
SARVAM_VOICE_APP_VERSION = _int("SARVAM_VOICE_APP_VERSION", 1)
SARVAM_VOICE_CONNECTION_ID = os.environ.get("SARVAM_VOICE_CONNECTION_ID", "").strip()
SARVAM_VOICE_AGENT_NUMBER = os.environ.get("SARVAM_VOICE_AGENT_NUMBER", "").strip()
# What the agent is told to say back: long answers are cut at a sentence so the caller is not talked at.
VOICE_REPLY_MAX_CHARS = _int("VOICE_REPLY_MAX_CHARS", 450)

# --- Web search for the buying journey -----------------------------------------
# Firecrawl finds policies online (Sarvam cannot browse). Blank key = feature off; the old text answers.
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_TIMEOUT = _float("FIRECRAWL_TIMEOUT", 45.0)
# Sites the policy search may read (comma-separated hostnames; subdomains count): the distributor's own
# insurance pages. Set in backend/.env, never here. Blank = the whole web.
POLICY_SEARCH_DOMAINS = tuple(d.strip().lower() for d in os.environ.get("POLICY_SEARCH_DOMAINS", "").split(",") if d.strip())
POLICY_PAGES_FILE = DATA_DIR / "policy_pages.yaml"
