"""The conversation, shared by every channel: WhatsApp and the web chat.

A channel turns what she sent into ``respond(conn, user, text, attachments)``
and delivers the ``Reply``: English messages, unless ``localized`` says the
text is already in her language (RAG answers are translated with their
amounts and citations protected).

Order of play for one message:
  1. "delete everything"          -> delete the case, say so
  2. a consent answer, if one is awaited (photos wait, unread, until YES)
  3. photos                       -> checklist slots (N3)
  4. a number, if a photo waits   -> the slot she picked
  5. other text                   -> classify (C1): question -> RAG answer with citations;
                                     grievance -> N5 route, named; pre_decision -> ask for the document
  6. anything else                -> the checklist status
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from app import cases, config, store
from app.clients import sarvam
from app.core import agent, ladders
from app.services import i18n

logger = logging.getLogger(__name__)

UNVERIFIED_BADGE = "⚠ Not yet verified."

CONSENT_PROMPT = (
    "Before I read your documents, I need your yes. To read a photo, I send it to our "
    "document-reading service, Sarvam. I keep only the details your claim needs, not the photo, "
    "unless you ask me to keep it. Account, Aadhaar, PAN and policy numbers are hidden in any text "
    "I send out. You can say \"delete everything\" at any time. Reply YES to agree, or NO."
)
CONSENT_DECLINED = "Okay. I will not read your documents. Reply YES any time if you change your mind."
DELETED = "Everything is deleted: your case, its documents and every record of them held by this service."
NEEDS_DETAIL = "Tell me which policy or loan this is about, and what happened, so I can send it to the right place."
PRE_DECISION = "Before you decide, send me a photo of the policy or the loan offer and I will check it for you."

_YES = {"yes", "y", "ok", "okay", "agree", "i agree", "haan", "han", "ha", "ho", "hoy",
        "हो", "होय", "हाँ", "हां", "ठीक", "ठीक आहे", "ठीक है", "चालेल"}
_NO = {"no", "n", "nahi", "nahin", "nako", "नाही", "नहीं", "नको", "मत"}
_DELETE = {"delete everything", "delete all", "delete my data", "सगळं हटवा", "सगळे हटवा", "सर्व हटवा",
           "सब हटाओ", "सब हटा दो", "सब डिलीट करो", "सब मिटा दो"}


@dataclass(frozen=True)
class Attachment:
    """Something she sent. ``fetch`` downloads it; nothing is fetched before consent."""

    content_type: str
    fetch: Callable[[], bytes]
    caption: str | None = None


@dataclass(frozen=True)
class Message:
    text: str
    unverified: bool = False  # relies on a value still marked UNVERIFIED: show the badge
    citations: tuple[dict, ...] = ()
    localized: bool = False  # already in her language; do not translate again


@dataclass(frozen=True)
class Reply:
    case_id: str | None
    language: str
    messages: tuple[Message, ...]


# Attachments that arrived before she answered the consent question, by user.
# In memory only: a restart means she sends them again.
_AWAITING_CONSENT: dict[str, list[Attachment]] = {}
_AWAITING_LOCK = threading.Lock()


def _normalised(text: str) -> str:
    """Lower case, punctuation dropped, spaces collapsed. Keeps Devanagari vowel signs."""
    kept = "".join(ch if unicodedata.category(ch)[0] in "LMN" or ch.isspace() else " " for ch in text or "")
    return " ".join(kept.lower().split())


def _number(text: str) -> int | None:
    """A numbered-list answer. Accepts any script's digits (६ as well as 6)."""
    cleaned = text.strip().rstrip(".)")
    return int(cleaned) if cleaned.isdecimal() else None


def _language(conn: sqlite3.Connection, case: dict, text: str, chosen: str | None) -> str:
    """Her language: the one she picked, or re-detected from any message of two or more words."""
    if chosen in config.SUPPORTED_LANGUAGES:
        if chosen != case.get("language"):
            store.set_language(conn, case["id"], chosen)
        return chosen
    language = case.get("language") or config.DEFAULT_LANGUAGE
    words = [w for w in (text or "").split() if any(ch.isalpha() for ch in w)]
    if len(words) < 2:
        return language
    detected = (sarvam.identify_language(text) or {}).get("language_code")
    if detected in config.SUPPORTED_LANGUAGES and detected != case.get("language"):
        store.set_language(conn, case["id"], detected)
        return detected
    return language


# --- Text she typed or said ------------------------------------------------------


def _ask(question: str, **kwargs):
    """Answer a coverage question from sources (RAG). Imported late: the index is heavy."""
    from app.rag import answer as rag_answer

    return rag_answer.answer(question, intent="question", **kwargs)


def _policy_context(conn: sqlite3.Connection, case: dict, context: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """Her insurer (as the corpus spells it) and product: from the page she is on, or her case."""
    from app.rag.retrieve import resolve_insurer

    context = context or {}
    product = context.get("product") or case.get("product") or None
    insurer = context.get("insurer") or cases.respondent_names(conn, case["id"]).get("insurer")
    return resolve_insurer(insurer), product


def _answer_text(conn: sqlite3.Connection, case: dict, text: str, language: str, context) -> tuple[Message, ...] | None:
    found = agent.classify(text)
    if found.intent == "question":
        insurer, product = _policy_context(conn, case, context)
        result = _ask(
            text, language=language, insurer=insurer, product=product,
            names=cases.respondent_names(conn, case["id"]),
        )
        citations = tuple({"label": c.label, **asdict(c)} for c in result.citations)
        return (Message(result.text, unverified=result.unverified, citations=citations, localized=True),)
    if found.intent == "grievance" and found.grievance_class:
        route = cases.route_case(conn, case["id"], found.product or case.get("product"), found.grievance_class)
        if route is None:
            return (Message(NEEDS_DETAIL),)
        step = ladders.load_steps()[route.first_step]
        who = route.respondent_name or f"the {route.respondent}"
        return (Message(f"This one is for {who}. First step: {step.label}.", unverified=step.verified_by == cases.UNVERIFIED),)
    if found.intent == "pre_decision":
        return (Message(PRE_DECISION),)
    return None


# --- One message in, one reply out -----------------------------------------------


def respond(
    conn: sqlite3.Connection,
    user: str,
    text: str = "",
    attachments: tuple[Attachment, ...] = (),
    *,
    language: str | None = None,
    context: Mapping[str, Any] | None = None,
    checklist: cases.Checklist | None = None,
) -> Reply:
    """Handle one message from ``user`` (a channel-scoped id such as whatsapp:+91… or web:<session>)."""
    checklist = checklist or cases.load_checklist()
    text = (text or "").strip()
    said = _normalised(text)

    # "delete everything": works at any point, and leaves nothing behind.
    if said in _DELETE:
        existing = store.find_case_for_user(conn, user)
        chosen = language if language in config.SUPPORTED_LANGUAGES else None
        reply_language = chosen or (existing or {}).get("language") or config.DEFAULT_LANGUAGE
        if existing:
            store.delete_case(conn, existing["id"])
        with _AWAITING_LOCK:
            _AWAITING_CONSENT.pop(user, None)
        return Reply(None, reply_language, (Message(DELETED),))

    case = store.case_for_user(conn, user)
    case_id = case["id"]
    reply_language = _language(conn, case, text, language) if (text or language) else (case.get("language") or config.DEFAULT_LANGUAGE)
    photos = [a for a in attachments if a.content_type in config.ALLOWED_MIME_TYPES]

    # Consent before the first document is read (N6).
    with _AWAITING_LOCK:
        waiting = bool(_AWAITING_CONSENT.get(user))
    if waiting and not photos:
        if said in _YES:
            store.record_consent(conn, case_id, "read_documents", True)
            store.record_consent(conn, case_id, "store_fields", True)
            with _AWAITING_LOCK:
                photos = _AWAITING_CONSENT.pop(user, [])
        elif said in _NO:
            store.record_consent(conn, case_id, "read_documents", False)
            with _AWAITING_LOCK:
                _AWAITING_CONSENT.pop(user, None)
            return Reply(case_id, reply_language, (Message(CONSENT_DECLINED),))
        else:
            return Reply(case_id, reply_language, (Message(CONSENT_PROMPT),))
    elif photos and not store.has_consent(conn, case_id, "read_documents"):
        with _AWAITING_LOCK:
            _AWAITING_CONSENT.setdefault(user, []).extend(photos)
        return Reply(case_id, reply_language, (Message(CONSENT_PROMPT),))

    messages: list[Message] = []
    pending = store.pending_document(conn, case_id, cases.CLAIM_DOC)
    number = _number(text) if text and not photos else None

    if photos:
        for index, attachment in enumerate(photos):
            try:
                data = attachment.fetch()
                extension = attachment.content_type.split("/")[-1]
                cases.attach(
                    conn, case_id, data, f"upload-{index}.{extension}",
                    mime_type=attachment.content_type, caption=attachment.caption, checklist=checklist,
                )
            except Exception as exc:  # a download or file problem: ask her to resend, never crash
                logger.info("Could not take an attachment: %s", exc)
                messages.append(Message("I could not open that file. Please send it again as a photo or a PDF."))
    elif number is not None and pending is not None:
        slot = checklist.by_number(number)
        if slot is not None:
            cases.choose_slot(conn, case_id, pending, slot, checklist)
    elif text and pending is None:
        answered = _answer_text(conn, case, text, reply_language, context)
        if answered:
            return Reply(case_id, reply_language, answered)

    state = cases.checklist_state(conn, case_id, checklist)
    if state.pending_document_id is not None:
        messages.append(Message(cases.options_message(checklist)))
    else:
        # The required-document list itself is UNVERIFIED, so the status carries the badge.
        messages.append(Message(cases.status_message(state, checklist), unverified=checklist.verified_by == cases.UNVERIFIED))
    return Reply(case_id, reply_language, tuple(messages))


def render(message: Message, language: str) -> str:
    """The message text in her language (the badge is the channel's to show)."""
    return message.text if message.localized else i18n.translate(message.text, language)
