"""Translation for dynamic content, with a cache.

Static UI copy belongs in locale dictionaries on the frontend - translating
button labels through an API on every mount is a network round-trip per string
that burns credits on text which never changes.

What genuinely needs the Sarvam translate API is dynamic content: findings,
report prose, document text, news summaries. That is what this module serves,
and it memoises aggressively because the same finding gets re-read far more
often than it is generated.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from app import config
from app.clients import sarvam

logger = logging.getLogger(__name__)

_CACHE: dict[str, str] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 5000


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

    try:
        translated = sarvam.translate(
            text,
            target_language,
            source_language=source_language,
            colloquial=colloquial,
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
