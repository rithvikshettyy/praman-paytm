"""Answer a coverage question from retrieved sources only (intent=question).

The model sees numbered sources and must cite them as [S1], [S2]; the
citations she reads, [insurer, doc_type, p.X], are built here from the index
metadata, never taken from the model's text. An answer with no valid
citation becomes NO_SOURCE. Sentences that promise a claim will be approved
or paid are removed. NO_SOURCE is handed to the N5 router: for a coverage
question the next step is a written coverage query to her insurer.

Questions are translated to English before retrieval and answers back to her
language through the existing Sarvam translation. Amounts, durations,
citations and legal names are swapped for placeholders during translation so
they reach her exactly as the source wrote them.

This module returns text. It never writes to the fact sheet, the store or a
verdict.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Mapping

from app import config
from app.clients import sarvam
from app.core import routing
from app.rag import retrieve as retrieval
from app.services import i18n

logger = logging.getLogger(__name__)

ANSWERED = "answered"
NO_SOURCE = "no_source"
UNVERIFIED = "UNVERIFIED"
ENGLISH = "en-IN"


class NotAQuestion(ValueError):
    """RAG answers intent=question only. Grievances and pre-decisions go to the engine."""


@dataclass(frozen=True)
class Citation:
    insurer: str  # the insurer, or the layer ("regulation") for regulatory text
    doc_type: str
    page: int
    source_url: str
    verified_by: str

    @property
    def label(self) -> str:
        return f"[{self.insurer}, {self.doc_type}, p.{self.page}]"


@dataclass(frozen=True)
class Answer:
    status: str  # answered | no_source
    text: str  # in her language
    text_en: str
    citations: tuple[Citation, ...]
    unverified: bool  # a cited source is still UNVERIFIED: show the badge
    handoff: dict | None  # NO_SOURCE only: where the N5 router sends the question next
    language: str
    translated: bool


SYSTEM_PROMPT = """You answer a policyholder's question about her insurance using ONLY the numbered sources you are given.

Rules:
- Use only what the sources say. If they do not answer the question, reply with exactly {"answer": "NO_SOURCE", "sources": []}.
- Cite every statement with the numbers of the sources it comes from, written like [S2].
- Never say or suggest that a claim will be approved, accepted or paid. Explain what the documents say; the insurer decides.
- Copy amounts, percentages and time periods exactly as the source states them. Do not calculate anything.
- Plain, simple English. At most 80 words.

Reply as JSON: {"answer": "<answer with [S#] citations>", "sources": ["S1", ...]}"""

_MARKER = re.compile(r"\[\s*(S\d+(?:\s*,\s*S\d+)*)\s*\]")
_SENTENCE = re.compile(r"(?<=[.!?।])\s+")
_PROMISE_WORDS = re.compile(r"\b(?:guarantee\w*|definitely|certainly|surely)\b", re.IGNORECASE)
_OUTCOME = re.compile(
    r"\b(?:will|shall|is going to|are going to)\b.*\b(?:approved?|paid|pay|accept\w*|settle\w*|sanction\w*|reimburs\w*)\b",
    re.IGNORECASE,
)
_HER = re.compile(r"\b(?:you|your)\b", re.IGNORECASE)
_CITATION = r"\[[^\[\]]+, [^\[\]]+, p\.\d+\]"
_BROKEN_TOKEN = re.compile(r"ZQ\d*|\d+ZQ")  # what is left of a placeholder the translator mangled
_REPEATED_LABEL = re.compile(r"(" + _CITATION + r")(?:\s*\1)+")
_PROTECT = re.compile(
    _CITATION  # citations
    + r"|(?:₹|Rs\.?\s?|INR\s?)\d[\d,]*(?:\.\d+)?"  # rupee amounts
    r"|\d[\d,]*(?:\.\d+)?\s?(?:%|per cent|percent)"  # percentages
    r"|\d[\d,]*(?:\.\d+)?\s(?:months?|days?|years?|hours?|lakhs?|crores?)\b"  # periods and sums
)


def _promises(sentence: str) -> bool:
    return bool(_PROMISE_WORDS.search(sentence) or (_HER.search(sentence) and _OUTCOME.search(sentence)))


def _source_line(sid: str, passage: retrieval.Passage) -> str:
    owner = passage.insurer or passage.layer
    return f"[{sid}] ({owner}, {passage.doc_type}, p.{passage.page})\n{passage.text.strip()}"


def _citation(passage: retrieval.Passage) -> Citation:
    return Citation(passage.insurer or passage.layer, passage.doc_type, passage.page, passage.source_url, passage.verified_by)


# --- Translation that keeps amounts, citations and names intact ---------------


def _protect(text: str, literals: tuple[str, ...] = ()) -> tuple[str, dict[str, str]]:
    tokens: dict[str, str] = {}

    def keep(value: str) -> str:
        token = f"ZQ{len(tokens)}ZQ"
        tokens[token] = value
        return token

    for literal in literals:
        if literal and literal in text:
            text = text.replace(literal, keep(literal))
    return _PROTECT.sub(lambda m: keep(m.group(0)), text), tokens


def _to_her_language(text_en: str, language: str, literals: tuple[str, ...] = ()) -> tuple[str, bool]:
    """Translate an English answer; fall back to English if a protected value did not survive.

    Citations are taken out before translating and put back at the end, once each: the
    translator dropped or broke them when there were several. Amounts, periods and names
    stay in place as placeholders; any lost, doubled or broken one sends the English answer.
    """
    if language == ENGLISH:
        return text_en, False
    labels = list(dict.fromkeys(re.findall(_CITATION, text_en)))
    bare = re.sub(r"\s+([.,;:!?।])", r"\1", " ".join(re.sub(_CITATION, " ", text_en).split()))
    protected, tokens = _protect(bare, literals)
    translated = i18n.translate(protected, language, source_language=ENGLISH)
    if translated == protected or any(translated.count(token) != 1 for token in tokens):
        return text_en, False
    for token, value in tokens.items():
        translated = translated.replace(token, value)
    if _BROKEN_TOKEN.search(translated):
        return text_en, False
    return " ".join([translated, *labels]).strip(), True


# --- NO_SOURCE ---------------------------------------------------------------


def _no_source(language: str, product: str | None, names: Mapping[str, str] | None) -> Answer:
    route = routing.route(product, None, names=names)
    if route is None:
        text_en = (
            "I could not find this in the documents I have, so I will not guess. "
            "Tell me which policy this is about and I will look again."
        )
        handoff = None
        literals: tuple[str, ...] = ()
    else:
        step = route.steps[1] if route.first_step == "answered_in_chat" and len(route.steps) > 1 else route.first_step
        handoff = {
            "respondent": route.respondent,
            "respondent_name": route.respondent_name,
            "step": step,
            "ladder": route.ladder,
            "distributor_owned": route.distributor_owned,
        }
        to = route.respondent_name or f"your {route.respondent}"
        text_en = (
            "I could not find this in your policy documents or the regulations I have, so I will not guess. "
            f"I can write a coverage question to {to} for you."
        )
        literals = (route.respondent_name,) if route.respondent_name else ()
    text, translated = _to_her_language(text_en, language, literals)
    return Answer(NO_SOURCE, text, text_en, (), False, handoff, language, translated)


# --- Answer ------------------------------------------------------------------


def answer(
    question: str,
    *,
    intent: str,
    language: str = ENGLISH,
    insurer: str | None = None,
    product: str | None = None,
    names: Mapping[str, str] | None = None,
    collection=None,
    k: int | None = None,
    question_language: str | None = None,
) -> Answer:
    """Answer ``question`` from her insurer's documents for her product, or regulation.

    ``insurer`` is written as in sources.yaml; ``names`` maps respondent kinds to
    legal names for the NO_SOURCE handoff (see cases.respondent_names). ``question_language``
    is the question's own language when it differs from the reply's (default: ``language``).
    """
    if intent != "question":
        raise NotAQuestion(f"RAG answers questions only, not {intent!r}")
    language = language if language in config.SUPPORTED_LANGUAGES else ENGLISH

    # mayura (colloquial) keeps who-does-what in everyday Marathi questions; the formal model inverted it.
    asked_in = question_language or language
    question_en = (
        question if asked_in == ENGLISH else i18n.translate(question, ENGLISH, source_language=asked_in, colloquial=True)
    )
    passages = retrieval.retrieve(question_en, insurer=insurer, product=product, k=k, collection=collection)
    if not passages:
        return _no_source(language, product, names)

    numbered = {f"S{i}": passage for i, passage in enumerate(passages, start=1)}
    prompt = "SOURCES\n\n" + "\n\n".join(_source_line(sid, p) for sid, p in numbered.items())
    prompt += f"\n\nQUESTION\n{question_en}"
    if asked_in != ENGLISH:
        # Machine translation can drop the key word (चोरी, "theft"); the model reads her own words too.
        prompt += f"\n\nHER OWN WORDS ({asked_in}; the English above is a machine translation)\n{question}"
    payload = None
    for _ in range(2):  # sarvam-105b now and then reasons past its budget and returns nothing; try once more
        try:
            payload = sarvam.chat_json(
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=2500,  # reasoning shares this budget; 500 ran out mid-reasoning
                default=None,
            )
        except Exception as exc:
            logger.warning("RAG answer failed: %s", exc)
            payload = None
        if isinstance(payload, dict):
            break
    if not isinstance(payload, dict):
        return _no_source(language, product, names)

    raw = str(payload.get("answer") or "").strip()
    if not raw or raw.upper().startswith("NO_SOURCE"):
        return _no_source(language, product, names)

    # Drop any sentence that promises an outcome; the insurer decides, not us.
    kept = " ".join(s for s in _SENTENCE.split(raw) if s.strip() and not _promises(s)).strip()

    cited: list[str] = []
    for match in _MARKER.finditer(kept):
        cited += [sid.strip() for sid in match.group(1).split(",")]
    if not cited:
        listed = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        cited = [str(sid) for sid in listed]
    cited = [sid for sid in dict.fromkeys(cited) if sid in numbered]
    if not kept or not cited:
        return _no_source(language, product, names)

    def to_labels(match: re.Match) -> str:
        sids = [sid.strip() for sid in match.group(1).split(",") if sid.strip() in numbered]
        return " ".join(dict.fromkeys(_citation(numbered[sid]).label for sid in sids))

    text_en = " ".join(_MARKER.sub(to_labels, kept).split())
    # [S1][S2] on the same page would read "[…, p.2] […, p.2]": say it once.
    text_en = _REPEATED_LABEL.sub(r"\1", text_en)
    citations = tuple(dict.fromkeys(_citation(numbered[sid]) for sid in cited))
    for citation in citations:
        if citation.label not in text_en:
            text_en = f"{text_en} {citation.label}"

    text, translated = _to_her_language(text_en, language)
    return Answer(
        status=ANSWERED,
        text=text,
        text_en=text_en,
        citations=citations,
        unverified=any(c.verified_by == UNVERIFIED for c in citations),
        handoff=None,
        language=language,
        translated=translated,
    )
