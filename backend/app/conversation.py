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
  5. other text                   -> a greeting or thanks gets a fixed reply; otherwise classify (C1):
                                     question -> RAG answer with citations; grievance -> N5 route, named;
                                     pre_decision -> ask for the document; smalltalk -> the greeting
  6. anything else                -> the checklist status

The web chat and WhatsApp also read what she sends (``explain_documents``): each document is
read in full, summarised with citations, and held in memory (rag/mine.py) so her
questions are answered from it first. Without it, photos fill the checklist.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from app import cases, config, readiness, store
from app.store import Store
from app.clients import sarvam
from app.core import agent, ladders, routing
from app.core import ladder_engine as le
from app.services import documents, i18n
from app.services.redact import redact

logger = logging.getLogger(__name__)

UNVERIFIED_BADGE = "⚠ Not yet verified."

CONSENT_PROMPT = (
    "Before I read your documents, I need your yes. To read a photo, I send it to our "
    "document-reading service, Sarvam. I keep only the details your claim needs, not the photo, "
    "unless you ask me to keep it. In the web chat I hold a document's text in memory, never saved, so I can "
    "answer questions about it. Account, Aadhaar, PAN and policy numbers are hidden in any text "
    "I send out. You can say \"delete everything\" at any time. Reply YES to agree, or NO."
)
CONSENT_DECLINED = "Okay. I will not read your documents. Reply YES any time if you change your mind."
DELETED = "Everything is deleted: your case, its documents and every record of them held by this service."
NEEDS_DETAIL = "Tell me which policy or loan this is about, and what happened, so I can send it to the right place."
PRE_DECISION = "Before you decide, send me a photo of the policy or the loan offer and I will check it for you."
GREETING = (
    "Hello, I am Praman. Send me any insurance policy (health, bike, car, life, travel or home) and ask me "
    "anything about it, in your language."
)
FOLLOW_UP = (
    "Ask me anything about the policy you sent: what it covers and what it does not, its dates and amounts, "
    "or what to do for a claim. You can also send another document."
)
ASK_FOR_POLICY = (
    "I answer only from your own policy, so I will not guess. Send a photo or PDF of the policy (health, bike, "
    "car, life, travel or any other) and ask again, or I can write the question to your insurer for you."
)
FEEDBACK_SOLVED = "Glad that helped. Ask me anything else about your policy, or send another document."
FEEDBACK_PERSON = (
    "I have passed your question and my answer to the support team with a short summary, so you will not "
    "need to explain it again. Nothing has been sent to your insurer."
)
MAX_QUESTION, MAX_ANSWER = 300, 500  # what is kept of her words and of the answer, for the agent's brief
HELP = (
    "I did not follow that. Ask me a question about your insurance, tell me what went wrong with a claim, "
    "or send a photo or PDF of your policy."
)
THANKS = "You are welcome. Ask me anything else about your policy or claim, or send the next document."
COULD_NOT_READ = "I could not read {name}. Please send a clearer photo or the PDF."
READ_IT = "Ask me anything about this document."
READ_THEM = "Ask me anything about these documents."
NOT_IN_HERS = (
    "I could not find this in the document you sent, or in the policy wordings and rules I have, so I will "
    "not guess. Try asking it another way, or send the page that covers it."
)
# Asked of her own document when she sends it; answered from its opening pages, with citations.
SUMMARY_QUESTION = (
    "What is this document? Say what kind of document it is, who issued it, who or what it covers, "
    "its dates, and its main amounts. At most three sentences."
)

WELCOME = (
    "Hello, I am Praman. I read your insurance papers and answer from them and from the rules, in your "
    "language. I never guess: if I cannot find it, I say so. Say \"delete everything\" at any time and "
    "I erase your case, or \"reset\" to erase it and start again."
)
MENU_PROMPT = "What would you like to do?"
MENU_BUTTONS = (("journey:find", "Find a policy"), ("journey:check", "Check my policy"),
                ("journey:complain", "Complaint"))
OPEN_FIND = (
    "Which insurance are you looking for? Or send the policy or quote you are considering, and send two or "
    "more to compare them."
)
FIND_KIND_BUTTONS = (("kind:health", "Health"), ("kind:life", "Life"), ("kind:motor", "Motor"))
OPEN_CHECK = "Send a photo or PDF of your policy, then ask me anything about it."
OPEN_COMPLAIN = "Tell me what went wrong, in your own words. If you have the policy or the insurer's letter, send it too."
JOURNEY_OPENINGS = {"find": OPEN_FIND, "check": OPEN_CHECK, "complain": OPEN_COMPLAIN}
WHAT_TO_LOOK_FOR = (
    "I cannot rank insurers, but these decide how a policy treats you at claim time: the sum insured; the "
    "room-rent limit; the co-payment; the waiting period for illnesses you already have; what is excluded; "
    "which hospitals are in the insurer's network; and how claims are paid (cashless or reimbursement). "
    "Send me the policies you are considering and I will read these out of each one."
)
BUY_CHECK_QUESTION = (
    "Before buying this policy, what should I know? Give the sum insured, the premium, the room-rent limit, "
    "the co-payment, the waiting periods and the main exclusions, whichever are in the document. "
    "At most five sentences."
)
# "Will this be covered?" with her policy and bill both read: the engine's verdict, not a model's guess.
COVER_INTRO = "I checked your bill against your policy."
COVER_BILLED = "Bill total: {amount}"
COVER_PARTS = (  # (BillSplit field, label): what comes off before the insurer pays; shown when not zero
    ("not_payable", "Items insurers usually do not pay: {amount}"),
    ("room_deduction", "Cut because the room costs more than the policy allows: {amount}"),
    ("co_pay", "Your co-payment ({percent}%): {amount}"),
    ("over_sum_insured", "Above your sum insured: {amount}"),
)
COVER_INSURER = "Insurer pays (estimate): {amount}"
COVER_YOU = "You pay (estimate): {amount}"
COVER_NOTHING = "This policy pays nothing for this bill, so you pay all of it: {amount}."
COVER_IF = "This assumes your policy covers this treatment."
COVER_NO_TOTAL = (
    "I could not read the bill's total clearly, so I cannot split it between you and the insurer. "
    "Send a clear photo of the page with the grand total."
)
COVER_HEADLINES = {
    le.FILE: "Nothing I check stops you filing this claim.",
    le.FILE_WITH_KNOWN_DEDUCTION: "You can file this claim, with the cut above.",
    le.DO_NOT_FILE_YET: "Do not file this claim yet:",
    le.FACTS_PENDING: "I cannot yet say for sure whether it is covered.",
}
COVER_ASK = "What still decides it: {questions} Look for these in your policy, or ask me about each one."
COVER_CAVEAT = "This is what your papers show, not an approval. The insurer decides the claim."
COVER_NEEDS_POLICY = "To tell you whether this bill is covered, I need your policy too. Send a photo or PDF of it."
NEED_NAME = "I do not yet know your insurer's full legal name, so I cannot address a letter to the right place."
DRAFT_NOT_SENT = "This is a draft. Nothing has been sent."
LETTER_BUTTON = ("letter", "Write the letter")

_YES = {"yes", "y", "ok", "okay", "agree", "i agree", "haan", "han", "ha", "ho", "hoy",
        "हो", "होय", "हाँ", "हां", "ठीक", "ठीक आहे", "ठीक है", "चालेल"}
_NO = {"no", "n", "nahi", "nahin", "nako", "नाही", "नहीं", "नको", "मत"}
_DELETE = {"delete everything", "delete all", "delete my data", "सगळं हटवा", "सगळे हटवा", "सर्व हटवा",
           "सब हटाओ", "सब हटा दो", "सब डिलीट करो", "सब मिटा दो"}
# "Start again": the same erasing as "delete everything"; the WhatsApp channel then restarts onboarding.
_RESET = {"reset", "reset everything", "reset chat", "restart", "start over", "रीसेट", "रिसेट",
          "रीसेट करो", "फिर से शुरू करो", "पुन्हा सुरू करा"}
# She is asking whether this bill will be paid, or how much: a cover word with a word pointing at the
# bill, or a cover word and nothing but everyday words ("will my insurance cover it?", "how much do I
# pay?"). "Does it cover maternity?" names something else, so it stays with the policy wording.
_COVER_WORDS = {"cover", "covered", "covers", "coverage", "payable", "pay", "paid", "reimburse", "reimbursed",
                "claimable", "कवर", "कव्हर", "क्लेम", "भरना", "देना", "मिलेगा"}
_BILL_WORDS = {"this", "these", "bill", "bills", "hospital", "expense", "expenses", "treatment", "admission",
               "यह", "ये", "इसका", "इसे", "हे", "बिल", "खर्च", "अस्पताल", "हॉस्पिटल", "रुग्णालय"}
_EVERYDAY_WORDS = {
    "will", "would", "can", "could", "does", "do", "did", "is", "are", "was", "be", "get", "have", "has", "need",
    "my", "me", "i", "we", "our", "the", "a", "an", "it", "that", "of", "to", "for", "by", "under", "in", "from",
    "and", "or", "how", "much", "what", "which", "part", "amount", "money", "all", "full", "whole", "entire",
    "insurance", "policy", "insurer", "company", "claim", "rest", "remaining", "left", "own", "pocket",
    "क्या", "मेरा", "मेरी", "मेरे", "बीमा", "पॉलिसी", "कितना", "कितने", "करेगा", "करेगी", "होगा", "होगी", "है",
    "को", "का", "की", "के", "में", "से", "पैसा", "पैसे", "मुझे", "हमें",
}
# She is asking about the claim documents: the checklist answers that, whatever else is under way.
_CHECKLIST_WORDS = {"missing", "document", "documents", "checklist", "status", "pending", "कागद", "कागदपत्र",
                    "कागदपत्रे", "दस्तावेज", "दस्तावेज़", "बाकी", "राहिले"}
# Answered without a model call, so a greeting works even when Sarvam is down.
_GREETINGS = {"hi", "hii", "hello", "helo", "hey", "hola", "namaste", "namaskar", "namaskaar", "good morning",
              "good afternoon", "good evening", "hi praman", "hello praman", "नमस्कार", "नमस्ते", "हाय", "हॅलो",
              "हेलो", "हलो", "राम राम", "जय महाराष्ट्र", "सुप्रभात", "शुभ प्रभात"}
_THANKS = {"thanks", "thank you", "thank you so much", "thanks a lot", "thx", "ty", "dhanyavad", "dhanyawad",
           "shukriya", "धन्यवाद", "शुक्रिया", "आभार", "थँक्यू", "थैंक यू"}


@dataclass(frozen=True)
class Attachment:
    """Something she sent. ``fetch`` downloads it; nothing is fetched before consent."""

    content_type: str
    fetch: Callable[[], bytes]
    caption: str | None = None
    filename: str | None = None


@dataclass(frozen=True)
class Message:
    text: str
    unverified: bool = False  # relies on a value still marked UNVERIFIED: show the badge
    citations: tuple[dict, ...] = ()
    localized: bool = False  # already in her language; do not translate again
    # (answer_id, kind): the chat asks "Did this solve it?" under this message. kind is "answer"
    # (Yes / No, I need help) or "not_found" (offer to get a person).
    feedback: tuple[int, str] | None = None
    # (id, title) reply buttons a channel may show; the id comes back as her next message.
    buttons: tuple[tuple[str, str], ...] = ()


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


def is_delete(text: str) -> bool:
    """She said "delete everything" or "reset": either erases her case."""
    return _normalised(text) in _DELETE | _RESET


def is_reset(text: str) -> bool:
    """She said "reset": erase everything, then start again."""
    return _normalised(text) in _RESET


def _number(text: str) -> int | None:
    """A numbered-list answer. Accepts any script's digits (६ as well as 6)."""
    cleaned = text.strip().rstrip(".)")
    return int(cleaned) if cleaned.isdecimal() else None


def _language(conn: Store, case: dict, text: str, chosen: str | None) -> str:
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


def _policy_context(conn: Store, case: dict, context: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """Her insurer (as the corpus spells it) and product: from the page she is on, or her case."""
    from app.rag.retrieve import resolve_insurer

    context = context or {}
    product = context.get("product") or case.get("product") or None
    insurer = context.get("insurer") or cases.respondent_names(conn, case["id"]).get("insurer")
    return resolve_insurer(insurer), product


def _answer_text(
    conn: Store, case: dict, text: str, language: str, context, journey: str | None = None
) -> tuple[Message, ...] | None:
    said = _normalised(text)
    if said in _THANKS:
        return (Message(THANKS),)
    if said in _GREETINGS:
        return (Message(GREETING),)
    words = set(said.split())
    about_this_bill = words & _BILL_WORDS or words <= _COVER_WORDS | _EVERYDAY_WORDS
    if journey != "complain" and words & _COVER_WORDS and about_this_bill:
        verdict = _cover(conn, case["id"])
        if verdict:
            return verdict
    found = agent.classify(text)
    if journey == "complain" and found.intent != "smalltalk" and not (found.intent == "grievance" and found.grievance_class):
        return (Message(NEEDS_DETAIL),)  # a complaint needs the problem and the product, not a coverage answer
    if found.intent == "smalltalk":
        return (Message(FOLLOW_UP if _has_hers(case["id"]) else GREETING),)
    unplaced_grievance = found.intent == "grievance" and not found.grievance_class
    if found.intent in (None, "question", "pre_decision") or unplaced_grievance:
        from_hers = _ask_hers(conn, case["id"], text, language)  # her own policy may already say what to do
        if from_hers:
            return from_hers
    if found.intent == "question":
        insurer, product = _policy_context(conn, case, context)
        result = _ask(
            text, language=language, insurer=insurer, product=product,
            names=cases.respondent_names(conn, case["id"]),
        )
        answer_id = _log_answer(conn, case["id"], text, result, "sources")
        if result.status != "answered":
            offer = (answer_id, "not_found")  # she can ask for a person
            if _has_hers(case["id"]):
                if result.handoff is None:  # say we looked in her documents, not "which policy is this?"
                    return (Message(NOT_IN_HERS, feedback=offer),)
            else:
                return (Message(ASK_FOR_POLICY, feedback=offer),)  # nothing of hers to answer from yet: ask for it
            return (_cited(result, offer),)
        return (_cited(result, (answer_id, "answer")),)
    if found.intent == "grievance" and found.grievance_class:
        product = found.product or case.get("product") or _product_of_hers(case["id"])
        store.record_event(conn, case["id"], "grievance_reported", {
            "text": redact(text)[:MAX_QUESTION], "grievance_class": found.grievance_class, "product": product,
        })
        route = cases.route_case(conn, case["id"], product, found.grievance_class)
        if route is None:
            return (Message(NEEDS_DETAIL),)
        step = ladders.load_steps()[route.first_step]
        who = route.respondent_name or f"your {route.respondent}"
        said_route = f"This one is for {who}. First step: {step.label}."
        unverified = step.verified_by == cases.UNVERIFIED
        if journey != "complain":
            return (Message(said_route, unverified=unverified),)
        if route.respondent_name:
            return (Message(said_route, unverified=unverified, buttons=(LETTER_BUTTON,)),)
        return (Message(f"{said_route} {NEED_NAME}", unverified=unverified),)
    if found.intent == "pre_decision":
        return (Message(PRE_DECISION),)
    return None


def _cover(conn: Store, case_id: str) -> tuple[Message, ...] | None:
    """Her bill checked against her policy by the engine (verdict, deduction, paper checks), in plain
    words. None when she has sent neither, so the question goes to her documents as before."""
    found = readiness.from_case(conn, case_id)
    if found is None:
        kinds = store.document_types(conn, case_id)
        if "bill" in kinds:
            return (Message(COVER_NEEDS_POLICY),)
        return None  # no bill yet: her policy wording answers what it covers
    sections = [COVER_INTRO, _split_lines(found["split"])]
    if found["split"] and found["split"]["conditional"]:
        sections.append(COVER_IF)
    reasons = [m["text"] for m in found["messages"]] + [f["message"] for f in found["papers"]["findings"]]
    sections.append("\n".join([COVER_HEADLINES.get(found["outcome"], COVER_HEADLINES[le.FACTS_PENDING])] + reasons))
    asked = [q["question"] for q in found["questions"] if q["required"]]
    if asked:
        sections.append(COVER_ASK.format(questions=" ".join(asked)))
    sections.append(COVER_CAVEAT)
    unverified = found["unverified"] or any(f["unverified"] for f in found["papers"]["findings"])
    return (Message("\n\n".join(sections), unverified=unverified),)


def _split_lines(split: dict | None) -> str:
    """The bill split as lines: total, what comes off, then the insurer's share and hers."""
    if split is None:
        return COVER_NO_TOTAL
    lines = [COVER_BILLED.format(amount=ladders.inr(split["billed"]))]
    if not split["covered"]:
        return "\n".join(lines + [COVER_NOTHING.format(amount=ladders.inr(split["you_pay"]))])
    lines += [
        "- " + label.format(amount=ladders.inr(split[part]), percent=f"{split['co_pay_percent']:g}" if split["co_pay_percent"] else "")
        for part, label in COVER_PARTS if split[part]
    ]
    lines += [COVER_INSURER.format(amount=ladders.inr(split["insurer_pays"])),
              COVER_YOU.format(amount=ladders.inr(split["you_pay"]))]
    return "\n".join(lines)


def _letter(conn: Store, case: dict, language: str) -> tuple[Message, ...]:
    """The draft letter, read back in her language. Nothing is sent."""
    from app.services import drafts

    try:
        draft = drafts.compose(conn, case["id"], language)
    except (drafts.RespondentUnknown, LookupError):
        return (Message(NEED_NAME),)
    return (Message(draft["readback"], localized=True), Message(DRAFT_NOT_SENT))


def _web_options(conn: Store, case_id: str, text: str, language: str) -> tuple[Message, ...] | None:
    """Policies found online: a kind button, or her needs for the kind she picked (health if none)."""
    from app.services import policy_search

    if text.startswith("kind:"):
        kind, needs = text.removeprefix("kind:"), None
        if kind not in policy_search.KINDS:
            return None
        store.record_event(conn, case_id, "find_kind", {"kind": kind})
    else:
        picked = store.latest_event(conn, case_id, "find_kind")
        kind, needs = (picked["detail"].get("kind") if picked else "health"), text
    found = policy_search.suggest(kind, needs, language)
    return (Message(found, unverified=True, localized=True),) if found else None


def _find_text(conn: Store, case: dict, text: str, language: str) -> tuple[Message, ...] | None:
    """Buying journey, text only: her documents first; else the regulation corpus (no insurer or
    product, so no insurer's wording is used); else what to look for."""
    if _has_hers(case["id"]):
        return _ask_hers(conn, case["id"], text, language)
    options = None if _normalised(text) in _GREETINGS | _THANKS else _web_options(conn, case["id"], text, language)
    if options:
        return options
    result = _ask(text, language=language)
    answer_id = _log_answer(conn, case["id"], text, result, "sources")
    if result.status == "answered":
        return (_cited(result, (answer_id, "answer")),)
    return (Message(WHAT_TO_LOOK_FOR, feedback=(answer_id, "not_found")),)


def _text_reply(conn, case: dict, text: str, language: str, context, journey: str | None):
    """Text she typed, by the journey she chose; no journey is the web chat's own flow."""
    if journey == "complain" and _normalised(text) == "letter":
        return _letter(conn, case, language)
    if journey == "find":
        found = _find_text(conn, case, text, language)
        if found:
            return found
    return _answer_text(conn, case, text, language, context, journey)


# --- Her own documents (web chat) ---------------------------------------------------


def _cited(result, feedback: tuple[int, str] | None = None) -> Message:
    citations = tuple({"label": c.label, **asdict(c)} for c in result.citations)
    return Message(result.text, unverified=result.unverified, citations=citations, localized=True, feedback=feedback)


def _log_answer(conn: Store, case_id: str, question: str, result, source: str) -> int:
    """Record that Praman answered (or could not), for the agent's brief and the console's counters.
    Her words and the answer are masked first; they are deleted with the case."""
    answered = result is not None and result.status == "answered"
    return store.record_event(conn, case_id, "answer_given", {
        "question": redact(question)[:MAX_QUESTION],
        "status": "answered" if answered else "not_found",
        "source": source,
        "product": _product_of_hers(case_id),  # the kind of policy she sent, if it was clear
        "pages": sorted({c.page for c in result.citations}) if answered else [],
        "answer_en": redact(without_citations(result.text_en))[:MAX_ANSWER] if answered else "",
    })


def _ask_hers(conn: Store, case_id: str, text: str, language: str) -> tuple[Message, ...] | None:
    """Answer from the documents she sent in this chat, if any; None when they do not say."""
    from app.rag import mine

    if not mine.has(case_id):
        return None
    # The closest passages first; then the opening pages, which is where a broad question
    # ("what do I need to know?") finds the schedule: dates, amounts, cover.
    for first_chunks in (None, config.RAG_TOP_K):
        found = mine.collection(case_id, first_chunks=first_chunks)
        try:
            result = _ask(text, language=language, insurer=mine.YOUR_DOCUMENT, product=mine.PRODUCT,
                          collection=found, k=mine.TOP_K)
        finally:
            mine.drop(found)
        if result.status == "answered":
            return (_cited(result, (_log_answer(conn, case_id, text, result, "her_document"), "answer")),)
    return None


def _has_hers(case_id: str) -> bool:
    from app.rag import mine

    return mine.has(case_id)


def _product_of_hers(case_id: str) -> str | None:
    from app.rag import mine

    return mine.product(case_id)


def _restore_documents(conn: Store, case_id: str) -> None:
    """After a restart the documents she sent are gone from memory: load them back from the store."""
    from app.rag import mine

    if mine.has(case_id):
        return
    for doc in store.case_pages(conn, case_id):
        mine.add(case_id, doc["filename"], doc["pages"], doc["product"])


def forget_documents(case_id: str) -> None:
    """Drop the documents she sent in the chat from memory (the saved text goes with the case)."""
    from app.rag import mine

    mine.forget(case_id)


def _read_fields(conn, case_id: str, attachment: Attachment, filename: str, data: bytes, pages, product) -> None:
    """Read the fields of a health policy or hospital bill she sent, so "will this be covered?" can be
    checked by the engine; and of an insurer's letter while the insurer's legal name is unknown, so a
    letter can be addressed. Every value still has to clear the confidence gate before it is used."""
    doc_type = documents.detect_doc_type(pages[0][1])
    if doc_type in ("policy", "bill"):
        if product not in (None, "health_policy"):
            return  # the claim check is for health; a bike or life policy is only explained
    elif doc_type != "letter" or routing.INSURER in cases.respondent_names(conn, case_id):
        return
    try:
        extraction = documents.extract(data, filename, doc_type, mime_type=attachment.content_type)
        store.save_document(conn, case_id, extraction, original=data, filename=filename)
    except Exception as exc:  # a failed read leaves the name unknown, never a crash
        logger.info("Could not read the fields of a chat document: %s", exc)


def _explain(
    conn, case: dict, photos: list[Attachment], text: str, language: str, checklist, context, journey: str | None = None
) -> list[Message]:
    """Read each document and hold its text for her questions.

    With a message that asks something, that is answered (from these documents first);
    otherwise, or when the message only says what the document is ("bill"), each one is summarised.
    """
    from app.rag import mine

    case_id = case["id"]
    buying = journey == "find"  # a policy she is only considering is not her claim: it fills no checklist
    messages: list[Message] = []
    read: list[str] = []
    for index, attachment in enumerate(photos):
        filename = attachment.filename or f"upload-{index}.{attachment.content_type.split('/')[-1]}"
        try:
            data = attachment.fetch()
            pages = documents.read_pages(data, filename, mime_type=attachment.content_type)
        except Exception as exc:  # unreadable or a Doc AI failure: ask for a better copy, never crash
            logger.info("Could not read a chat document: %s", exc)
            pages = []
        if not pages:
            messages.append(Message(COULD_NOT_READ.format(name=filename)))
            continue
        product = documents.detect_product(" ".join(text for _, text in pages[:3]))
        read.append(mine.add(case_id, filename, pages, product))
        store.save_pages(conn, case_id, filename, product, pages)  # consent was checked before this runs
        if not buying:
            _read_fields(conn, case_id, attachment, filename, data, pages, product)

        # A health claim document still ticks its checklist slot, quietly; a bike, life or other
        # policy never fills the health claim checklist.
        slot = cases.classify_slot(checklist, caption=attachment.caption) or cases.classify_slot(checklist, text=pages[0][1])
        if slot is not None and product in (None, "health_policy") and not buying:
            cases.attach(conn, case_id, data, filename, mime_type=attachment.content_type, slot=slot, checklist=checklist)
    if not read:
        return messages

    said = _normalised(text)
    asks = said and said not in _GREETINGS | _THANKS and cases.classify_slot(checklist, caption=text) is None
    answered = _text_reply(conn, case, text, language, context, journey) if asks else None
    offer = Message(READ_IT if len(read) == 1 else READ_THEM)
    if answered:
        return messages + list(answered) + [offer]

    for name in read:
        found = mine.collection(case_id, only=name, first_chunks=config.RAG_TOP_K)
        try:
            summary = _ask(BUY_CHECK_QUESTION if buying else SUMMARY_QUESTION, language=language, question_language="en-IN",
                           insurer=mine.YOUR_DOCUMENT, product=mine.PRODUCT, collection=found)
        finally:
            mine.drop(found)
        if summary.status == "answered":
            messages.append(_cited(summary))
    return messages + [offer]


# --- "Did this solve it?" --------------------------------------------------------------


class UnknownAnswer(LookupError):
    """The answer is not one this person was given."""


def give_feedback(
    conn: Store, user: str, answer_id: int, solved: bool, language: str | None = None
) -> tuple[Reply, bool]:
    """Her Yes or No to "Did this solve it?": the reply, and what was recorded. A Yes is counted on
    the console; a No also asks for a person, who then sees a brief. The first answer to each
    question stands, so a repeated tap counts once."""
    case = store.find_case_for_user(conn, user)
    given = store.get_event(conn, case["id"], answer_id, "answer_given") if case else None
    row = given["detail"] if given else None
    if case is None or row is None:
        raise UnknownAnswer(f"no answer {answer_id} for this person")
    earlier = next(
        (e["detail"] for e in store.case_events(conn, case["id"], "answer_feedback") if e["detail"].get("answer_id") == answer_id),
        None,
    )
    if earlier is not None:
        solved = bool(earlier["solved"])
    else:
        store.record_event(conn, case["id"], "answer_feedback", {"answer_id": answer_id, "solved": solved})
        if not solved:
            store.record_event(conn, case["id"], "agent_requested", {
                "reason": "not_solved" if row.get("status") == "answered" else "not_found", "answer_id": answer_id,
            })
    chosen = language if language in config.SUPPORTED_LANGUAGES else None
    reply = Reply(case["id"], chosen or case.get("language") or config.DEFAULT_LANGUAGE,
                  (Message(FEEDBACK_SOLVED if solved else FEEDBACK_PERSON),))
    _log_turn(conn, reply, "Yes, it solved my question." if solved else "No, I need help.")
    return reply, solved


# --- One message in, one reply out -----------------------------------------------


def respond(conn: Store, user: str, text: str = "", attachments: tuple[Attachment, ...] = (), **kwargs) -> Reply:
    """Handle one message from ``user`` (a channel-scoped id such as whatsapp:+91… or web:<session>),
    and keep the turn in the transcript, masked. Nothing is kept for "delete everything"."""
    reply = _respond(conn, user, text, attachments, **kwargs)
    if reply.case_id is not None:
        _log_turn(conn, reply, text, attachments)
    return reply


def _log_turn(conn: Store, reply: Reply, text: str, attachments: tuple[Attachment, ...] = ()) -> None:
    if (text or "").strip() or attachments:
        store.record_message(conn, reply.case_id, "user", text, reply.language, tuple(a.content_type for a in attachments))
    for message in reply.messages:
        store.record_message(conn, reply.case_id, "praman", message.text, reply.language, citations=message.citations)


def _respond(
    conn: Store,
    user: str,
    text: str = "",
    attachments: tuple[Attachment, ...] = (),
    *,
    language: str | None = None,
    context: Mapping[str, Any] | None = None,
    checklist: cases.Checklist | None = None,
    explain_documents: bool = False,
    journey: str | None = None,
) -> Reply:
    """Handle one message from ``user`` (a channel-scoped id such as whatsapp:+91… or web:<session>)."""
    checklist = checklist or cases.load_checklist()
    text = (text or "").strip()
    said = _normalised(text)

    # "delete everything": works at any point, and leaves nothing behind.
    if said in _DELETE or said in _RESET:
        existing = store.find_case_for_user(conn, user)
        chosen = language if language in config.SUPPORTED_LANGUAGES else None
        reply_language = chosen or (existing or {}).get("language") or config.DEFAULT_LANGUAGE
        if existing:
            store.delete_case(conn, existing["id"])
            forget_documents(existing["id"])
        with _AWAITING_LOCK:
            _AWAITING_CONSENT.pop(user, None)
        return Reply(None, reply_language, (Message(DELETED),))

    case = store.case_for_user(conn, user)
    case_id = case["id"]
    _restore_documents(conn, case_id)
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

    if photos and explain_documents:
        return Reply(case_id, reply_language, tuple(_explain(conn, case, photos, text, reply_language, checklist, context, journey)))
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
        answered = _text_reply(conn, case, text, reply_language, context, journey)
        if answered:
            return Reply(case_id, reply_language, answered)

    state = cases.checklist_state(conn, case_id, checklist)
    if state.pending_document_id is not None:
        messages.append(Message(cases.options_message(checklist)))
    elif not state.collected and not messages and not (set(said.split()) & _CHECKLIST_WORDS):
        # No health claim under way: the document checklist would be beside the point.
        messages.append(Message(HELP))
    else:
        # The required-document list itself is UNVERIFIED, so the status carries the badge.
        messages.append(Message(cases.status_message(state, checklist), unverified=checklist.verified_by == cases.UNVERIFIED))
    return Reply(case_id, reply_language, tuple(messages))


_CITATION_LABEL = re.compile(r"\s*\[[^\[\]]+, [^\[\]]+, p\.\d+\]")
_SPACE_BEFORE_STOP = re.compile(r"\s+([.,;:!?।])")


def without_citations(text: str) -> str:
    """The answer without its inline [source, document, p.N] labels: for the screen, which shows
    the citations as chips, and for anything spoken aloud."""
    return _SPACE_BEFORE_STOP.sub(r"\1", _CITATION_LABEL.sub("", text)).strip()


def render(message: Message, language: str) -> str:
    """The message text in her language (the badge is the channel's to show)."""
    return message.text if message.localized else i18n.translate(message.text, language)
