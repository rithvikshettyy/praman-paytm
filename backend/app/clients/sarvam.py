"""The single Sarvam entry point for the whole backend.

Everything that talks to Sarvam goes through here so that retries, rate
limiting and the structured-output quirks live in exactly one place. No other
module imports ``sarvamai`` directly.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Iterable, Sequence

from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError
from sarvamai.errors import (
    BadRequestError,
    ContentTooLargeError,
    ForbiddenError,
    InternalServerError,
    NotFoundError,
    PaymentRequiredError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnauthorizedError,
    UnprocessableEntityError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app import config
from app.services.redact import redact

logger = logging.getLogger(__name__)

# Errors worth another attempt: transient server-side or rate limiting.
RETRYABLE = (TooManyRequestsError, ServiceUnavailableError, InternalServerError)
# Errors that will never succeed on retry - bad schema, bad key, bad input.
FATAL = (
    BadRequestError,
    UnprocessableEntityError,
    UnauthorizedError,
    ForbiddenError,
    NotFoundError,
    PaymentRequiredError,
    ContentTooLargeError,
)


class SarvamUnavailable(RuntimeError):
    """Raised when Sarvam is unreachable or the key is missing/invalid."""


class SarvamBadRequest(ValueError):
    """Raised when we sent something Sarvam will never accept."""


class _TokenBucket:
    """Blocking token bucket.

    Doc AI allows ~10 requests/minute. Without this, two users uploading at the
    same time produce a burst that trips 429 on the third or fourth job. This
    trades a little latency for jobs that actually get submitted.
    """

    def __init__(self, rate_per_minute: int):
        self.capacity = max(1, rate_per_minute)
        self.tokens = float(self.capacity)
        self.refill_per_second = self.capacity / 60.0
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.refill_per_second
                )
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.refill_per_second
            if time.monotonic() + wait > deadline:
                raise SarvamUnavailable(
                    "Timed out waiting for a Doc AI rate-limit slot. Try again shortly."
                )
            time.sleep(min(wait, 1.0))


_doc_ai_bucket = _TokenBucket(config.DOC_AI_RATE_LIMIT_PER_MIN)

_client: SarvamAI | None = None
_client_lock = threading.Lock()


def client() -> SarvamAI:
    """Lazily build and reuse one SDK client for the process."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not config.SARVAM_API_KEY:
                    raise SarvamUnavailable("SARVAM_API_KEY is not configured.")
                _client = SarvamAI(
                    api_subscription_key=config.SARVAM_API_KEY,
                    timeout=config.SARVAM_TIMEOUT,
                )
    return _client


def _translate_errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FATAL as exc:
        raise SarvamBadRequest(f"Sarvam rejected the request: {_describe(exc)}") from exc
    except ApiError as exc:
        raise SarvamUnavailable(f"Sarvam API error: {_describe(exc)}") from exc


def _describe(exc: ApiError) -> str:
    body = getattr(exc, "body", None)
    status = getattr(exc, "status_code", "?")
    return f"[{status}] {body!r}"[:500]


# Tokens sarvam-105b spends thinking before it emits any answer. Measured at
# ~310 for a one-word reply, so this is deliberately generous.
REASONING_HEADROOM = 1500

_retry = retry(
    retry=retry_if_exception_type(RETRYABLE),
    wait=wait_exponential_jitter(initial=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)


# --- Chat -------------------------------------------------------------------


@_retry
def _chat_raw(**kwargs):
    return client().chat.completions(**kwargs)


# /v2 is beta and key-gated. Once a call comes back with the beta refusal there
# is no point retrying it for the rest of the process, so the outcome is cached
# and every later request goes straight to the v1 model.
_V2_AVAILABLE: bool | None = None
_V2_LOCK = threading.Lock()
_BETA_MARKERS = ("beta", "not available", "request beta access")


def v2_available() -> bool | None:
    """True/False once probed, None if never attempted."""
    return _V2_AVAILABLE


def _chat_v2(
    messages: Sequence[dict],
    *,
    model: str,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
) -> str:
    """Open-weight models (glm5.2, gemma4) on /v2/chat/completions.

    The SDK types `model` as Literal["sarvam-105b"], so /v2 needs a direct
    HTTP call.
    """
    import httpx

    payload: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = min(
            max_tokens + REASONING_HEADROOM, config.SARVAM_MAX_TOKENS_CEILING
        )
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
        # On glm5.2 JSON mode returns content: null unless thinking is off.
        payload["enable_thinking"] = False

    response = httpx.post(
        f"{config.SARVAM_BASE_URL}/v2/chat/completions",
        headers={
            "api-subscription-key": config.SARVAM_API_KEY,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=config.SARVAM_TIMEOUT,
    )

    if response.status_code >= 400:
        body = response.text[:400]
        if response.status_code == 400 and any(m in body.lower() for m in _BETA_MARKERS):
            raise _BetaUnavailable(body)
        raise SarvamUnavailable(f"/v2 error [{response.status_code}] {body}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message", {}).get("content") or "").strip()


class _BetaUnavailable(RuntimeError):
    """The /v2 endpoint refused because the key is not whitelisted."""


def chat(
    messages: Sequence[dict],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    json_mode: bool = False,
    wiki_grounding: bool = False,
    reasoning_effort: str | None = None,
) -> str:
    """Run a chat completion and return the assistant's text.

    ``json_mode`` asks for a JSON object. The /v1 Chat Completions API does not
    expose ``response_format`` as a typed SDK parameter, so it is injected via
    ``additional_body_parameters``.
    """
    global _V2_AVAILABLE
    chosen = model or config.CHAT_MODEL
    # Identifiers never leave the process (PRD-PAYTM N6).
    messages = [{**m, "content": redact(m.get("content") or "")} for m in messages]

    # Route open-weight models over /v2, falling back to the v1 model when the
    # key is not whitelisted for the beta.
    if chosen in config.V2_MODELS and _V2_AVAILABLE is not False:
        try:
            result = _chat_v2(
                messages,
                model=chosen,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
            with _V2_LOCK:
                _V2_AVAILABLE = True
            return result
        except _BetaUnavailable as exc:
            with _V2_LOCK:
                _V2_AVAILABLE = False
            logger.warning(
                "Sarvam /v2 (%s) is not enabled for this key; falling back to %s. %s",
                chosen,
                config.CHAT_MODEL,
                str(exc)[:160],
            )
            chosen = config.CHAT_MODEL
        except SarvamUnavailable:
            logger.warning("/v2 call failed; falling back to %s", config.CHAT_MODEL)
            chosen = config.CHAT_MODEL

    if chosen in config.V2_MODELS:
        chosen = config.CHAT_MODEL

    kwargs: dict[str, Any] = {
        "messages": list(messages),
        "model": chosen,
        "temperature": temperature,
    }
    if max_tokens:
        # sarvam-105b reasons before answering, and that reasoning is billed
        # against max_tokens. A budget sized for the answer alone gets consumed
        # entirely by reasoning and returns empty content with
        # finish_reason="stop" - which looks like a model failure but is not.
        # The sum is clamped because the API rejects anything above the tier
        # ceiling outright, and a 400 here would silently drop a whole pass.
        kwargs["max_tokens"] = min(
            max_tokens + REASONING_HEADROOM, config.SARVAM_MAX_TOKENS_CEILING
        )
    if wiki_grounding:
        kwargs["wiki_grounding"] = True
    # Structured extraction wants the answer, not a long deliberation.
    effort = reasoning_effort or ("low" if json_mode else None)
    if effort:
        kwargs["reasoning_effort"] = effort
    if json_mode:
        kwargs["request_options"] = {
            "additional_body_parameters": {"response_format": {"type": "json_object"}}
        }

    response = _translate_errors(_chat_raw, **kwargs)
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    return (getattr(choices[0].message, "content", "") or "").strip()


def chat_json(
    messages: Sequence[dict],
    *,
    default: Any = None,
    **kwargs,
) -> Any:
    """Chat and parse the reply as JSON, tolerating the usual model noise.

    Returns ``default`` rather than raising when the model produces something
    unparseable - a single bad clause analysis must not sink a whole report.
    """
    kwargs.setdefault("json_mode", True)
    raw = chat(messages, **kwargs)
    parsed = parse_json_loose(raw)
    if parsed is None:
        logger.warning("Could not parse JSON from model reply: %s", raw[:300])
        return default
    return parsed


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json_loose(text: str) -> Any:
    """Best-effort JSON extraction from a model reply."""
    if not text:
        return None
    candidate = _FENCE.sub("", text.strip())
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced {...} or [...] in the string.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return _salvage_truncated(candidate)


def _salvage_truncated(text: str) -> Any:
    """Recover whatever survived a reply that was cut off mid-JSON.

    Structured passes ask for a list of objects, and a token budget that runs
    out mid-array would otherwise discard every finding in the batch - the
    model did the work and we throw it away. Complete objects are recovered and
    the trailing partial one is dropped.
    """
    objects = []
    # A stack, not a depth counter: in a truncated {"suggestions": [{...},{...
    # the outer brace never closes, so objects that complete at depth 1 are the
    # only ones there are. Counting closures at depth 0 finds nothing.
    starts: list[int] = []
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            starts.append(i)
        elif ch == "}" and starts:
            start = starts.pop()
            # Only keep objects that are inside the wrapper, not the wrapper
            # itself, so a fully-parsed reply is never double-counted here.
            try:
                parsed = json.loads(text[start : i + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                objects.append((start, parsed))

    if not objects:
        return None

    # The wrapper key the model was emitting. Salvaging {"suggestions": [...]}
    # under the name "findings" would be silently dropped by the caller.
    match = re.search(r'"(\w+)"\s*:\s*\[', text)
    key = match.group(1) if match else "items"

    # If a recovered object already carries the wrapper key, it is the complete
    # reply and its list is what the caller wants.
    for _, obj in objects:
        if key in obj and isinstance(obj[key], list):
            return obj

    items = [obj for _, obj in objects]
    logger.info("Salvaged %d complete %s object(s) from a truncated reply", len(items), key)
    return {key: items}


# --- Doc AI -----------------------------------------------------------------


@_retry
def _doc_ai_call(method: str, **kwargs):
    _doc_ai_bucket.acquire()
    return getattr(client().doc_ai, method)(**kwargs)


def digitise(
    file_bytes: bytes,
    filename: str,
    *,
    language: str | None = None,
    output_format: str = "html",
    content_type: str = "mixed",
) -> str:
    """Submit a digitise job and return its job id.

    ``html`` is requested rather than ``md`` because the HTML output preserves
    block structure (tags, and bounding boxes when the API supplies them),
    which is what anchors report highlights back to the page. Markdown for
    display is derived from it downstream.
    """
    kwargs = {
        "file": [(filename, file_bytes)],
        "output_format": output_format,
        "content_type": content_type,
        "auto_orient": "true",
    }
    # Never pass None to a Doc AI method: the SDK's multipart encoder calls
    # .read() on every supplied value, so an explicit None raises
    # AttributeError before the request is even sent. Omitted is not the same
    # as None here.
    if language:
        kwargs["language"] = language

    response = _translate_errors(_doc_ai_call, "digitise", **kwargs)
    return response.job_id


def extract(
    file_bytes: bytes,
    filename: str,
    schema: dict,
    *,
    language: str | None = None,
) -> str:
    """Submit a schema-based extraction job and return its job id.

    ``schema`` must be serialised - passing a dict raises AttributeError inside
    the SDK. Callers pass a plain dict and this handles the encoding.
    """
    kwargs = {
        "file": [(filename, file_bytes)],
        # Must be serialised - passing a dict raises AttributeError in the SDK.
        "schema": json.dumps(schema),
        "output_format": "json",
        "auto_orient": "true",
    }
    if language:
        kwargs["language"] = language

    response = _translate_errors(_doc_ai_call, "extract", **kwargs)
    return response.job_id


def job_status(job_id: str) -> dict:
    response = _translate_errors(_doc_ai_call, "get_status", job_id=job_id)
    return _dump(response)


def job_results(job_id: str) -> dict:
    response = _translate_errors(_doc_ai_call, "get_results", job_id=job_id)
    return _dump(response)


def _dump(model: Any) -> dict:
    """Model -> plain dict, keeping fields the SDK does not type.

    The SDK's models are configured ``extra="allow"``, so anything Sarvam adds
    (per-block ids, bounding boxes) survives and stays available downstream.
    """
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", exclude_none=False)
    if isinstance(model, dict):
        return model
    return {"value": model}


TERMINAL_STATUSES = {"completed", "partially_completed", "failed", "rejected"}
SUCCESS_STATUSES = {"completed", "partially_completed"}


def is_terminal(status: str | None) -> bool:
    return (status or "").lower() in TERMINAL_STATUSES


# --- Text -------------------------------------------------------------------


@_retry
def _text_call(method: str, **kwargs):
    return getattr(client().text, method)(**kwargs)


# Sarvam's translate endpoint caps a single request; longer text is chunked on
# sentence boundaries so document prose survives the round trip intact.
_TRANSLATE_CHUNK_CHARS = 900


def translate(
    text: str,
    target_language: str,
    *,
    source_language: str = "auto",
    colloquial: bool = False,
) -> str:
    """Translate text into an Indian language, chunking when necessary."""
    if not text or not text.strip():
        return text
    if target_language == source_language:
        return text

    model = config.TRANSLATE_MODEL_COLLOQUIAL if colloquial else config.TRANSLATE_MODEL
    mode = "code-mixed" if colloquial else "formal"

    out = []
    for chunk in _chunk_text(text, _TRANSLATE_CHUNK_CHARS):
        kwargs = {
            "input": redact(chunk),
            "source_language_code": source_language,
            "target_language_code": target_language,
            "model": model,
            "numerals_format": "international",
        }
        # output_script and mode are transliteration features that only
        # mayura:v1 supports; sarvam-translate:v1 rejects them with a 400.
        if model == config.TRANSLATE_MODEL_COLLOQUIAL:
            kwargs["mode"] = mode
            kwargs["output_script"] = "fully-native"
        response = _translate_errors(_text_call, "translate", **kwargs)
        out.append(getattr(response, "translated_text", "") or "")
    return " ".join(part for part in out if part).strip()


def _chunk_text(text: str, limit: int) -> Iterable[str]:
    if len(text) <= limit:
        yield text
        return
    buf = ""
    for sentence in re.split(r"(?<=[.!?।])\s+", text):
        if len(buf) + len(sentence) + 1 > limit and buf:
            yield buf.strip()
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf.strip():
        yield buf.strip()


def identify_language(text: str) -> dict:
    """Detect the language of a snippet. Returns {} when detection fails."""
    if not text or not text.strip():
        return {}
    try:
        response = _text_call("identify_language", input=redact(text[:1000]))
    except Exception as exc:  # detection is advisory, never fatal
        logger.debug("Language identification failed: %s", exc)
        return {}
    return _dump(response)


def transliterate(text: str, target_language: str, *, source_language: str = "auto") -> str:
    """Romanise or nativise a string (used for lender/borrower name matching)."""
    if not text or not text.strip():
        return text
    response = _translate_errors(
        _text_call,
        "transliterate",
        input=text,
        source_language_code=source_language,
        target_language_code=target_language,
        numerals_format="international",
    )
    return getattr(response, "transliterated_text", "") or text


# --- Speech -----------------------------------------------------------------


@_retry
def _speech_call(attr: str, method: str, **kwargs):
    return getattr(getattr(client(), attr), method)(**kwargs)


def speech_to_text(
    audio_bytes: bytes,
    filename: str,
    *,
    language: str | None = None,
    mode: str = "transcribe",
) -> dict:
    """Transcribe audio. Keeps the Sarvam key server-side."""
    response = _translate_errors(
        _speech_call,
        "speech_to_text",
        "transcribe",
        file=(filename, audio_bytes),
        model=config.STT_MODEL,
        mode=mode,
        language_code=language or "unknown",
    )
    return _dump(response)


def text_to_speech(
    text: str,
    *,
    language: str = config.DEFAULT_LANGUAGE,
    speaker: str | None = None,
    codec: str | None = None,
) -> dict:
    """Synthesise speech. Returns the SDK payload (base64 audio list).

    ``codec`` picks the audio format (e.g. "mp3"); WhatsApp will not play the
    default WAV, so the WhatsApp channel asks for opus.
    """
    kwargs = {
        "text": redact(text[:2500]),
        "language_code": language,
        "speaker": speaker or config.TTS_SPEAKER,
        "model": config.TTS_MODEL,
        "enable_preprocessing": True,
    }
    if codec:
        kwargs["output_audio_codec"] = codec
    response = _translate_errors(_speech_call, "text_to_speech", "convert", **kwargs)
    return _dump(response)
