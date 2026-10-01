"""Translation through Sarvam, with caches.

Dynamic content (findings, answers, document text) is translated per call and
memoised in process. The website's own words go through ``translate_page``:
each string is translated once per language and kept on disk, so translating a
button label costs one call ever, not one per visit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app import config
from app.clients import sarvam

logger = logging.getLogger(__name__)

_CACHE: dict[str, str] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 5000
_NAME_TOKEN = "[[90]]"  # apart from the [[0]], [[1]] ... the answer and page paths use


def _key(text: str, target: str, colloquial: bool) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"{target}:{int(colloquial)}:{digest}"


def translate(
    text: str,
    target_language: str,
    *,
    source_language: str = "en-IN",
    colloquial: bool = False,
) -> str:
    """Translate, returning the original text on any failure."""
    if not text or not text.strip():
        return text
    if not target_language or target_language == source_language:
        return text
    if target_language not in config.SUPPORTED_LANGUAGES:
        return text

    key = _key(text, target_language, colloquial)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    # Our name stays "Praman" in every language (it was spelt प्रमान, प्रमण, प्रमाण by turns).
    held = _BRAND.sub(_NAME_TOKEN, text) if source_language == config.DEFAULT_LANGUAGE else text
    try:
        translated = sarvam.translate(
            held,
            target_language,
            source_language=source_language,
            colloquial=colloquial,
        )
        if held != text:
            if translated.count(_NAME_TOKEN) == held.count(_NAME_TOKEN):
                translated = translated.replace(_NAME_TOKEN, "Praman")
            else:  # the name could not be held: translate the text as written
                translated = sarvam.translate(
                    text, target_language, source_language=source_language, colloquial=colloquial
                )
    except Exception as exc:
        # Untranslated English beats an error page.
        logger.warning("Translation to %s failed: %s", target_language, exc)
        return text

    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = translated
    return translated


def translate_fields(payload: dict, fields: list[str], target_language: str) -> dict:
    """Translate named string fields of a dict in place-ish (returns a copy)."""
    if not target_language or target_language == config.DEFAULT_LANGUAGE:
        return payload
    out = dict(payload)
    for name in fields:
        value = out.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = translate(value, target_language)
    return out


def translate_findings(findings: list[dict], target_language: str) -> list[dict]:
    """Translate the borrower-facing prose of findings, never the clause text.

    ``clause_text`` stays in the document's own language on purpose: it is a
    verbatim quote that anchors a highlight, and translating it would break
    both the quotation and the character offsets.
    """
    if not target_language or target_language == config.DEFAULT_LANGUAGE:
        return findings
    return [translate_fields(f, ["title", "explanation"], target_language) for f in findings]


def detect(text: str) -> str:
    """Best-effort language detection, defaulting to the configured language."""
    result = sarvam.identify_language(text)
    code = (result or {}).get("language_code")
    return code if code in config.SUPPORTED_LANGUAGES else config.DEFAULT_LANGUAGE


# --- The website, translated: every visible string, cached on disk ------------------

# Values that must reach her exactly as printed: rupee amounts, percentages, dates, long numbers.
_KEEP = re.compile(
    r"(?:₹|Rs\.?\s?|INR\s?)\d[\d,]*(?:\.\d+)?"
    r"|\d[\d,]*(?:\.\d+)?\s?%"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d[\d,]{3,}(?:\.\d+)?"
)
_LEFTOVER = re.compile(r"\[\[|\]\]")
# Words the formal model gets wrong in an insurance site ("policy" as a government policy:
# धोरण, नीति). Held like an amount and replaced with the word people use for an insurance policy.
_DOUBLED = re.compile(r"([%₹])\s*\1")
POLICY_WORD = {
    "hi-IN": "पॉलिसी", "mr-IN": "पॉलिसी", "gu-IN": "પોલિસી", "bn-IN": "পলিসি", "ta-IN": "பாலிசி",
    "te-IN": "పాలసీ", "kn-IN": "ಪಾಲಿಸಿ", "ml-IN": "പോളിസി", "pa-IN": "ਪਾਲਿਸੀ", "od-IN": "ପଲିସି",
}
_GLOSSARY = (
    (re.compile(r"\b[Pp]olic(?:y|ies)\b"), POLICY_WORD),
    (re.compile(r"\b[Cc]ases?\b"), {"hi-IN": "केस", "mr-IN": "केस"}),  # not खटला, a lawsuit
)
_BRAND = re.compile(r"\bPraman\b")  # our name stays as written, never spelt out (प्रमण)
_PAGE_LOCK = threading.Lock()
_PAGE_CACHE: dict[str, dict[str, str]] | None = None  # language -> English -> translation
MAX_TEXTS = 300
MAX_CHARS = 1000
PARALLEL_CALLS = 3  # six at once ran into Sarvam's rate limit (429) on a fresh cache
RATE_LIMIT_RETRIES = 3
_sleep = time.sleep


def _page_cache() -> dict[str, dict[str, str]]:
    global _PAGE_CACHE
    if _PAGE_CACHE is None:
        try:
            _PAGE_CACHE = json.loads(config.PAGE_TRANSLATIONS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _PAGE_CACHE = {}
    return _PAGE_CACHE


def _save_page_cache() -> None:
    try:
        config.PAGE_TRANSLATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.PAGE_TRANSLATIONS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_page_cache(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(config.PAGE_TRANSLATIONS_PATH)
    except OSError as exc:  # a cache that cannot be saved is only slower next time
        logger.warning("Could not save page translations: %s", exc)


def _translate_kept(text: str, language: str) -> str | None:
    """One string, with its amounts, percentages, dates and the word "policy" held as [[n]]
    placeholders. None when Sarvam fails or a held value does not come back exactly once."""
    held: dict[str, str] = {}

    def hold(match: re.Match, value: str | None = None) -> str:
        token = f"[[{len(held)}]]"
        held[token] = value if value is not None else match.group(0)
        return token

    protected = _BRAND.sub(hold, _KEEP.sub(hold, text))
    for pattern, words in _GLOSSARY:
        if language in words:
            protected = pattern.sub(lambda m, word=words[language]: hold(m, word), protected)
    out = None
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            out = sarvam.translate(protected, language, source_language=config.DEFAULT_LANGUAGE)
            break
        except Exception as exc:
            if "429" in str(exc) and attempt < RATE_LIMIT_RETRIES:
                _sleep(1.5 * (attempt + 1))  # Sarvam's rate limit: wait and try again
                continue
            logger.warning("Page translation to %s failed: %s", language, exc)
            return None
    if not out or any(out.count(token) != 1 for token in held):
        return None
    for token, value in held.items():
        out = out.replace(token, value)
    out = _DOUBLED.sub(r"\1", out)  # the translator sometimes repeats a held % or ₹ ("1%%")
    if not text.rstrip().endswith((".", "?", "!", ":")):
        out = out.rstrip().rstrip(".।")  # a label stays a label: no full stop the English did not have
    return None if _LEFTOVER.search(out) else out


def translate_page(texts: list[str], language: str) -> list[str]:
    """The website's own words in her language, one Sarvam call per string never seen before.

    Each string is translated once per language and kept on disk (backend/.local), so a page
    costs nothing the second time. Anything that cannot be translated safely stays in English.
    """
    if language == config.DEFAULT_LANGUAGE or language not in config.SUPPORTED_LANGUAGES:
        return list(texts)
    with _PAGE_LOCK:
        known = dict(_page_cache().get(language, {}))
    missing = list(dict.fromkeys(t for t in texts if t.strip() and t not in known))
    if missing:
        with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
            done = dict(zip(missing, pool.map(lambda t: _translate_kept(t, language), missing)))
        fresh = {t: out for t, out in done.items() if out}
        if fresh:
            with _PAGE_LOCK:
                _page_cache().setdefault(language, {}).update(fresh)
                _save_page_cache()
            known.update(fresh)
    return [known.get(t, t) for t in texts]
