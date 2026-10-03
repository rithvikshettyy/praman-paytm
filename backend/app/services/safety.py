"""Sentence checks shared by every answer Praman writes: no promised outcomes."""

from __future__ import annotations

import re

_SENTENCE = re.compile(r"(?<=[.!?।])\s+")
_PROMISE_WORDS = re.compile(r"\b(?:guarantee\w*|definitely|certainly|surely)\b", re.IGNORECASE)
_OUTCOME = re.compile(
    r"\b(?:will|shall|is going to|are going to)\b.*\b(?:approved?|paid|pay|accept\w*|settle\w*|sanction\w*|reimburs\w*)\b",
    re.IGNORECASE,
)
_HER = re.compile(r"\b(?:you|your)\b", re.IGNORECASE)


def promises(sentence: str) -> bool:
    """Whether a sentence promises approval or payment; the insurer decides, not us."""
    return bool(_PROMISE_WORDS.search(sentence) or (_HER.search(sentence) and _OUTCOME.search(sentence)))


def drop_promises(text: str) -> str:
    return " ".join(s for s in _SENTENCE.split(text) if s.strip() and not promises(s)).strip()
