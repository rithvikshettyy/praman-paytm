"""MongoDB store (PRD-PAYTM C7): cases, documents, consents, events, drafts, the chat transcript
and the text of documents she sent.

Retention default: we keep the fields read from a document, not the document.
The original is written to disk only when the case has an unrevoked
``keep_original`` consent; otherwise it is never stored. The documents in
scope are medical bills and loan agreements, so this is a product decision,
not a nicety. The chat transcript and the document text are stored masked
(``redact``), and the document text only after the consent that lets her papers be read.

Callers keep one function per question (``get_case``, ``record_event``, ...) and get plain
dicts back; none of them sees MongoDB. ``connect()`` returns a ``Store`` holding the database.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app import config
from app.services.documents import Extraction
from app.services.redact import redact

CONSENT_SCOPES = (
    "read_documents",  # read any document she sends (OCR and extraction); asked before the first one
    "read_policy",
    "read_bill",
    "read_kfs",
    "store_fields",
    "contact_insurer",
    "keep_original",  # keep the uploaded file itself, not just the fields read from it
)

# The audit trail. Every number on the Distributor Console (N8) is counted
# from these rows; nothing is projected or estimated.
EVENT_KINDS = (
    "case_routed",  # detail: product, grievance_class, respondent, respondent_name, distributor_owned, ladder
    "readiness_checked",  # detail: ladder, outcome
    "claim_stopped",  # detail: rules (the blocks that stopped it), possible_on
    "deduction_explained",  # detail: rule, deduction (rupees or null while pending), pending
    "coverage_query_drafted",
    "escalation_drafted",
    "clock_started",  # detail: ladder, step, started_on, respond_by, verified_by
    "draft_approved",  # detail: draft_id, kind. Approved and ready to send; nothing is sent
    "papers_checked",  # detail: fix (problems an insurer would query), heads_up. No names or amounts
    "answer_given",  # detail: question, status (answered|not_found), source (her_document|sources), pages, answer_en. Redacted
    "answer_feedback",  # detail: answer_id, solved. "Did this solve it?"; the first answer to each question stands
    "agent_requested",  # detail: reason (not_solved|not_found), answer_id. She asked for a person after an answer
    "grievance_reported",  # detail: text (redacted), grievance_class, product. What she wrote when something went wrong
    "case_status",  # detail: status (resolved|pending). An agent's mark on the console; the latest one stands
    "journey_chosen",  # detail: journey (find|check|complain). Her pick from the WhatsApp menu; the latest one stands
)
CASE_STATUSES = ("pending", "resolved")

# Every collection that belongs to one case, deleted with it.
CASE_COLLECTIONS = ("documents", "consents", "events", "drafts", "messages", "doc_pages")

# What a case looked like as a row, so callers still get every key.
_CASE_DEFAULTS = {
    "channel_user": None, "language": None, "product": None, "respondent": None,
    "respondent_name": None, "distributor_owned": None, "is_example": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain(value: Any) -> Any:
    """What a JSON round trip keeps: dates and the like become text."""
    return json.loads(json.dumps(value, default=str))


# --- Connection --------------------------------------------------------------

_SETUP_LOCK = threading.Lock()
_client: MongoClient | None = None  # one pooled client per process; tests put a fake here
_indexed: set[tuple[int, str]] = set()


@dataclass
class Store:
    """An open store. ``close`` does nothing: the client is pooled for the process."""

    db: Any

    def close(self) -> None:
        pass


def _get_client() -> MongoClient:
    global _client
    with _SETUP_LOCK:
        if _client is None:
            _client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True)
        return _client


def _ensure_indexes(db) -> None:
    key = (id(_get_client()), db.name)
    with _SETUP_LOCK:
        if key in _indexed:
            return
        db.cases.create_index("channel_user", unique=True, sparse=True)  # absent, not null, for API cases
        db.events.create_index([("case_id", ASCENDING), ("kind", ASCENDING)])
        db.events.create_index("id", unique=True)
        db.documents.create_index("case_id")
        db.documents.create_index("id", unique=True)
        db.drafts.create_index("case_id")
        db.drafts.create_index("id", unique=True)
        db.consents.create_index([("case_id", ASCENDING), ("scope", ASCENDING)])
        db.messages.create_index([("case_id", ASCENDING), ("id", ASCENDING)])
        db.doc_pages.create_index("case_id")
        _indexed.add(key)


def connect(db_name: str | None = None) -> Store:
    """Open the store, creating the indexes on first use in this process."""
    db = _get_client()[db_name or config.MONGO_DB]
    _ensure_indexes(db)
    return Store(db)


def _next_id(conn: Store, name: str) -> int:
    """The next integer id for a collection: ids show up in URLs and in "Did this solve it?" taps."""
    row = conn.db.counters.find_one_and_update(
        {"_id": name}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return row["n"]


def _row(doc: dict | None) -> dict[str, Any] | None:
    return None if doc is None else {k: v for k, v in doc.items() if k != "_id"}


# --- Cases -------------------------------------------------------------------


def _case_out(doc: dict | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    return {**_CASE_DEFAULTS, **_row(doc), "id": doc["_id"]}


def get_case(conn: Store, case_id: str) -> dict[str, Any] | None:
    return _case_out(conn.db.cases.find_one({"_id": case_id}))


def ensure_case(conn: Store, case_id: str) -> dict[str, Any]:
    """The case with this id, opening it if it does not exist yet."""
    conn.db.cases.update_one({"_id": case_id}, {"$setOnInsert": {"created_at": _now()}}, upsert=True)
    return get_case(conn, case_id)


def find_case_for_user(conn: Store, channel_user: str) -> dict[str, Any] | None:
    return _case_out(conn.db.cases.find_one({"channel_user": channel_user}))


def delete_case(conn: Store, case_id: str, originals_dir: Path | None = None) -> dict[str, int] | None:
    """Delete everything held for a case: documents, kept originals, consents, events, drafts, the
    chat transcript, the document text, the case.

    Returns how many rows went from each collection, or None if there was no such
    case. Events go too, so the case disappears from every console number.
    """
    if get_case(conn, case_id) is None:
        return None
    deleted = {name: conn.db[name].delete_many({"case_id": case_id}).deleted_count for name in CASE_COLLECTIONS}
    conn.db.cases.delete_one({"_id": case_id})
    shutil.rmtree(Path(originals_dir or config.ORIGINALS_DIR) / _safe_name(case_id), ignore_errors=True)
    return deleted


def wipe_all(conn: Store, originals_dir: Path | None = None) -> int:
    """Delete every case and every row that belongs to one, and every kept original.
    For resetting a demo store only. Returns how many cases there were."""
    count = conn.db.cases.count_documents({})
    for name in (*CASE_COLLECTIONS, "cases"):
        conn.db[name].delete_many({})
    shutil.rmtree(Path(originals_dir or config.ORIGINALS_DIR), ignore_errors=True)
    return count


def case_for_user(conn: Store, channel_user: str) -> dict[str, Any]:
    """The case for a messaging user (one open case per sender), opening it on first contact."""
    found = find_case_for_user(conn, channel_user)
    if found:
        return found
    case_id = uuid.uuid4().hex
    try:
        conn.db.cases.insert_one({"_id": case_id, "channel_user": channel_user, "created_at": _now()})
    except DuplicateKeyError:  # two first messages at once: the other one won
        return find_case_for_user(conn, channel_user)
    return get_case(conn, case_id)


def set_language(conn: Store, case_id: str, language: str | None) -> None:
    conn.db.cases.update_one({"_id": case_id}, {"$set": {"language": language}})


def set_example(conn: Store, case_id: str) -> None:
    """Label a case as a seeded example, so every screen can say so."""
    conn.db.cases.update_one({"_id": case_id}, {"$set": {"is_example": 1}})


def set_route(
    conn: Store,
    case_id: str,
    product: str | None,
    respondent: str | None,
    respondent_name: str | None,
    distributor_owned: bool | None,
) -> None:
    """Record who owes this case an answer. All None clears a previous route."""
    conn.db.cases.update_one(
        {"_id": case_id},
        {"$set": {
            "product": product, "respondent": respondent, "respondent_name": respondent_name,
            "distributor_owned": None if distributor_owned is None else int(distributor_owned),
        }},
    )


# --- Events ------------------------------------------------------------------


def record_event(conn: Store, case_id: str, kind: str, detail: dict | None = None, at: str | None = None) -> int:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    event_id = _next_id(conn, "events")
    conn.db.events.insert_one(
        {"id": event_id, "case_id": case_id, "kind": kind, "detail": _plain(detail or {}), "at": at or _now()}
    )
    return event_id


def case_events(conn: Store, case_id: str, kind: str, limit: int | None = None) -> list[dict[str, Any]]:
    """A case's events of one kind, oldest first; with ``limit``, the latest few (still oldest first)."""
    cursor = conn.db.events.find({"case_id": case_id, "kind": kind}).sort("id", DESCENDING)
    if limit:
        cursor = cursor.limit(limit)
    return [_row(doc) for doc in reversed(list(cursor))]


def latest_event(conn: Store, case_id: str, kind: str) -> dict[str, Any] | None:
    return _row(conn.db.events.find_one({"case_id": case_id, "kind": kind}, sort=[("id", DESCENDING)]))


def get_event(conn: Store, case_id: str, event_id: int, kind: str) -> dict[str, Any] | None:
    """One event of a case by its id, or None if it is not this case's, or not this kind."""
    return _row(conn.db.events.find_one({"id": event_id, "case_id": case_id, "kind": kind}))


# The console's one row per case: its latest route, verdict and clock, each read from the latest
# event of that kind (never from a value an event did not write).
_LATEST_KINDS = ("case_routed", "readiness_checked", "clock_started", "case_status")


def console_rows(conn: Store) -> list[dict[str, Any]]:
    # ponytail: one pass over every event, fine at demo scale; an aggregation pipeline if cases pile up
    latest: dict[str, dict[str, dict]] = {}
    last_at: dict[str, str] = {}
    asked: set[str] = set()
    for event in conn.db.events.find({}).sort("id", ASCENDING):
        case = event["case_id"]
        if event["kind"] in _LATEST_KINDS:
            latest.setdefault(case, {})[event["kind"]] = event["detail"]
        elif event["kind"] == "agent_requested":
            asked.add(case)
        last_at[case] = max(last_at.get(case, ""), event["at"])
    with_documents = set(conn.db.documents.distinct("case_id"))
    rows = []
    for case in conn.db.cases.find({}):
        found = latest.get(case["_id"], {})
        route, verdict = found.get("case_routed", {}), found.get("readiness_checked", {})
        clock, status = found.get("clock_started", {}), found.get("case_status", {})
        rows.append({
            "case_id": case["_id"],
            "created_at": case["created_at"],
            "is_example": case.get("is_example") or 0,
            "product": route.get("product"),
            "grievance_class": route.get("grievance_class"),
            "respondent": route.get("respondent"),
            "respondent_name": route.get("respondent_name"),
            "distributor_owned": route.get("distributor_owned"),
            "verdict": verdict.get("outcome"),
            "clock_step": clock.get("step"),
            "clock_respond_by": clock.get("respond_by"),
            "clock_verified_by": clock.get("verified_by"),
            "last_event_at": last_at.get(case["_id"]),
            "has_documents": case["_id"] in with_documents,
            "agent_requested": case["_id"] in asked,
            "agent_status": status.get("status") or "pending",
        })
    return rows


# --- Consents ----------------------------------------------------------------


def record_consent(conn: Store, case_id: str, scope: str, granted: bool, at: str | None = None) -> None:
    """Append a grant or revocation. The latest row for a scope wins."""
    if scope not in CONSENT_SCOPES:
        raise ValueError(f"unknown consent scope {scope!r}")
    conn.db.consents.insert_one({
        "id": _next_id(conn, "consents"), "case_id": case_id, "scope": scope,
        "granted": int(bool(granted)), "at": at or _now(),
    })


def has_consent(conn: Store, case_id: str, scope: str) -> bool:
    """True only if the latest row for this scope is a grant. No row means no."""
    row = conn.db.consents.find_one({"case_id": case_id, "scope": scope}, sort=[("id", DESCENDING)])
    return bool(row and row["granted"])


# --- Documents ---------------------------------------------------------------


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "document"


def save_document(
    conn: Store,
    case_id: str,
    extraction: Extraction,
    *,
    original: bytes | None = None,
    filename: str | None = None,
    slot: str | None = None,
    originals_dir: Path | None = None,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Store what was read from a document; keep the file only with consent.

    ``confidence`` is the weakest confidence among the fields that were read,
    so one number says how much of the row still needs confirming.
    """
    fields = {name: {"value": f.value, "confidence": f.confidence} for name, f in extraction.fields.items()}
    read = [f.confidence for f in extraction.fields.values() if f.value is not None]
    keep = original is not None and has_consent(conn, case_id, "keep_original")

    doc_id = _next_id(conn, "documents")
    conn.db.documents.insert_one({
        "id": doc_id,
        "case_id": case_id,
        "doc_type": extraction.doc_type,
        "slot": slot,
        "received_at": received_at or _now(),
        "fields": _plain(fields),
        "confidence": min(read) if read else None,
        "retained": int(keep),
    })
    if keep:
        folder = Path(originals_dir or config.ORIGINALS_DIR) / _safe_name(case_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{doc_id}-{_safe_name(filename or 'document')}").write_bytes(original)
    return get_document(conn, doc_id)


def get_document(conn: Store, doc_id: int) -> dict[str, Any] | None:
    return _row(conn.db.documents.find_one({"id": doc_id}))


def case_documents(conn: Store, case_id: str, doc_types: tuple[str, ...]) -> list[dict[str, Any]]:
    """Documents of these types on the case, newest first, with fields decoded."""
    found = conn.db.documents.find({"case_id": case_id, "doc_type": {"$in": list(doc_types)}}).sort("id", DESCENDING)
    return [_row(doc) for doc in found]


def document_types(conn: Store, case_id: str) -> set[str]:
    """The kinds of document on a case, never their names or text."""
    return set(conn.db.documents.distinct("doc_type", {"case_id": case_id}))


def filled_slots(conn: Store, case_id: str) -> set[str]:
    return {slot for slot in conn.db.documents.distinct("slot", {"case_id": case_id}) if slot is not None}


def pending_document(conn: Store, case_id: str, doc_type: str) -> int | None:
    """The latest document of this type still waiting to be placed in a slot."""
    row = conn.db.documents.find_one({"case_id": case_id, "doc_type": doc_type, "slot": None}, sort=[("id", DESCENDING)])
    return row["id"] if row else None


def assign_slot(conn: Store, case_id: str, doc_id: int, slot: str) -> bool:
    """Place a document in a slot. False if no such document belongs to this case."""
    return conn.db.documents.update_one({"id": doc_id, "case_id": case_id}, {"$set": {"slot": slot}}).matched_count == 1


# --- Her words and her papers ------------------------------------------------


def record_message(
    conn: Store, case_id: str, role: str, text: str, language: str | None = None,
    attachments: tuple[str, ...] = (), citations: tuple[dict, ...] = (),
) -> None:
    """One turn of the chat, masked. ``role`` is user or praman; ``attachments`` are content types,
    never file names or bytes."""
    conn.db.messages.insert_one({
        "id": _next_id(conn, "messages"), "case_id": case_id, "role": role, "text": redact(text or ""),
        "language": language, "attachments": list(attachments), "citations": _plain(list(citations)), "at": _now(),
    })


def case_messages(conn: Store, case_id: str) -> list[dict[str, Any]]:
    return [_row(doc) for doc in conn.db.messages.find({"case_id": case_id}).sort("id", ASCENDING)]


def save_pages(conn: Store, case_id: str, filename: str, product: str | None, pages: list[tuple[int, str]]) -> None:
    """Keep the text of a document she sent, masked, so her questions still work after a restart.
    Call only once she has consented to her documents being read. A file sent again replaces its earlier copy."""
    conn.db.doc_pages.delete_many({"case_id": case_id, "filename": filename})
    conn.db.doc_pages.insert_one({
        "id": _next_id(conn, "doc_pages"), "case_id": case_id, "filename": filename, "product": product,
        "pages": [{"page": int(page), "text": redact(text)} for page, text in pages if text and text.strip()],
        "at": _now(),
    })


def case_pages(conn: Store, case_id: str) -> list[dict[str, Any]]:
    """Her saved documents, oldest first: filename, product and (page, text) pairs."""
    return [
        {"filename": doc["filename"], "product": doc["product"], "pages": [(p["page"], p["text"]) for p in doc["pages"]]}
        for doc in conn.db.doc_pages.find({"case_id": case_id}).sort("id", ASCENDING)
    ]


# --- Drafts ------------------------------------------------------------------


def save_draft(conn: Store, case_id: str, kind: str, addressee: str, text: str, unverified: bool) -> dict[str, Any]:
    draft_id = _next_id(conn, "drafts")
    conn.db.drafts.insert_one({
        "id": draft_id, "case_id": case_id, "kind": kind, "addressee": addressee, "text": text,
        "unverified": int(unverified), "status": "drafted", "created_at": _now(), "approved_at": None,
    })
    return get_draft(conn, case_id, draft_id)


def get_draft(conn: Store, case_id: str, draft_id: int) -> dict[str, Any] | None:
    out = _row(conn.db.drafts.find_one({"id": draft_id, "case_id": case_id}))
    if out is not None:
        out["unverified"] = bool(out["unverified"])
    return out


def case_drafts(conn: Store, case_id: str) -> list[dict[str, Any]]:
    found = conn.db.drafts.find({"case_id": case_id}).sort("id", DESCENDING)
    return [get_draft(conn, case_id, doc["id"]) for doc in found]


def approve_draft(conn: Store, case_id: str, draft_id: int) -> dict[str, Any] | None:
    """Mark a draft approved and ready to send. Nothing is sent anywhere."""
    draft = get_draft(conn, case_id, draft_id)
    if draft is None:
        return None
    conn.db.drafts.update_one(
        {"id": draft_id, "case_id": case_id},
        {"$set": {"status": "approved", "approved_at": draft["approved_at"] or _now()}},
    )
    return get_draft(conn, case_id, draft_id)
