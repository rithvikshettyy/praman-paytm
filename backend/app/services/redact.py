"""Mask identifiers before any text leaves the process (PRD-PAYTM N6).

Applied in app/clients/sarvam.py, the one place text is sent out for
inference. Masked: Aadhaar numbers, PAN, bank account numbers (any run of 9
to 18 digits, which also catches phone numbers), and policy or claim
numbers. Amounts written with Indian grouping (5,00,000), dates, months and
short numbers pass through untouched.

This masks text only. A photo sent to Doc AI is an image and cannot be masked
here; that is covered by asking her consent before any document is read.
"""

from __future__ import annotations

import re

AADHAAR = "[AADHAAR]"
PAN = "[PAN]"
ACCOUNT = "[ACCOUNT]"
POLICY = "[POLICY]"

# "Policy No: X", "policy number X", "Policy #X": the next token, if it has a digit.
_POLICY_LABELLED = re.compile(
    r"(\bpolicy\s*(?:no\.?|number|num\.?|#)\s*[:\-]?\s*)(?=[A-Z0-9/\-]*\d)[A-Z0-9][A-Z0-9/\-]{3,}",
    re.IGNORECASE,
)
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE)
# 12 digits, first 2-9, optionally grouped 4-4-4 by spaces or hyphens.
_AADHAAR = re.compile(r"\b[2-9]\d{3}([ -]?)\d{4}\1\d{4}\b")
# Slash- or hyphen-structured references with at least four digits: P/211100/01/2023/123456.
_STRUCTURED = re.compile(r"\b(?=[A-Z0-9/\-]*\d[A-Z0-9/\-]*\d[A-Z0-9/\-]*\d[A-Z0-9/\-]*\d)[A-Z0-9]{1,10}(?:[/\-][A-Z0-9]{1,12}){2,}\b")
_DATE = re.compile(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2}")
# 9-18 digits unbroken, or in groups of 3-6 separated by single spaces or hyphens.
_ACCOUNT = re.compile(r"\b\d{9,18}\b|\b\d{3,6}(?:[ -]\d{3,6}){1,5}\b")


def _structured(match: re.Match) -> str:
    token = match.group(0)
    return token if _DATE.fullmatch(token) else POLICY


def _account(match: re.Match) -> str:
    digits = sum(ch.isdigit() for ch in match.group(0))
    return ACCOUNT if 9 <= digits <= 18 else match.group(0)


def redact(text: str) -> str:
    """Mask identifiers in ``text``. Safe to run twice."""
    if not text:
        return text
    text = _POLICY_LABELLED.sub(lambda m: m.group(1) + POLICY, text)
    text = _PAN.sub(PAN, text)
    text = _AADHAAR.sub(AADHAAR, text)
    text = _STRUCTURED.sub(_structured, text)
    text = _ACCOUNT.sub(_account, text)
    return text
