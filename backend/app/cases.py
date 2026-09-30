"""Cases: the claim document checklist (PRD-PAYTM N3).

A claim needs a known set of documents. She sends photos one at a time; each
is placed in a slot and ticks it off. A photo is placed by what she typed
with it, or by keywords in its OCR text. When neither is clear it waits,
unplaced, and she picks the slot from a numbered list: slots are never
guessed. The checklist becomes ``documents_collected`` /
``documents_required`` for the engine's ``documents_incomplete`` rule.

Respondent routing (N5): ``route_case`` runs the pure router in
app/core/routing.py with the legal names read from her documents, and
records the result on the case.

Every step that the Distributor Console counts writes an event row here:
routing, readiness checks (and what stopped a claim), deductions explained,
clocks started.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import typing
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any, Callable

import yaml

from app import config, store
from app.clients import sarvam
from app.core import ladder_engine as le
from app.core import ladders, routing
from app.services import documents
from app.services.documents import Extraction

logger = logging.getLogger(__name__)

CLAIM_DOC = "claim_doc"  # documents.doc_type for checklist photos
UNVERIFIED = ladders.UNVERIFIED


class UnknownSlot(ValueError):
    """A slot name that is not on the checklist."""


@dataclass(frozen=True)
class Slot:
    id: str
    label: str
    keywords: tuple[str, ...]
    caption: tuple[str, ...]


@dataclass(frozen=True)
class Checklist:
    id: str
    verified_by: str
    slots: tuple[Slot, ...]

    @property
    def slot_ids(self) -> tuple[str, ...]:
        return tuple(slot.id for slot in self.slots)

    def label(self, slot_id: str) -> str:
        return next(slot.label for slot in self.slots if slot.id == slot_id)

    def by_number(self, number: int) -> str | None:
        """The slot a numbered-list answer names, counting from 1."""
        return self.slots[number - 1].id if 1 <= number <= len(self.slots) else None

    def require(self, slot_id: str) -> str:
        if slot_id not in self.slot_ids:
            raise UnknownSlot(f"{slot_id!r} is not on the {self.id} checklist")
        return slot_id


@lru_cache(maxsize=None)
def load_checklist(name: str = "insurance_health_claim") -> Checklist:
    raw = yaml.safe_load((config.CHECKLISTS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    verified_by = raw.get("verified_by")
    if not isinstance(verified_by, str) or not verified_by.strip():
        raise ValueError(f"checklist {name}: verified_by is required (UNVERIFIED until checked)")
    slots = []
    for item in raw.get("slots") or []:
        words = {key: tuple(str(w).strip().lower() for w in item.get(key) or ()) for key in ("keywords", "caption")}
        if not item.get("id") or not item.get("label") or not all(words.values()):
            raise ValueError(f"checklist {name}: every slot needs id, label, keywords and caption")
        slots.append(Slot(item["id"], item["label"], words["keywords"], words["caption"]))
    if not slots or len({s.id for s in slots}) != len(slots):
        raise ValueError(f"checklist {name}: slots must be present and unique")
    return Checklist(raw.get("id") or name, verified_by.strip(), tuple(slots))


# --- Placing a photo in a slot -----------------------------------------------


@lru_cache(maxsize=None)
def _pattern(word: str) -> re.Pattern:
    # Latin words match whole words (plurals allowed); other scripts, whose
    # vowel signs break \b, match anywhere.
    if word.isascii():
        return re.compile(rf"\b{re.escape(word)}(?:s|es)?\b")
    return re.compile(re.escape(word))


def _hits(words: tuple[str, ...], text: str) -> int:
    return sum(1 for word in words if _pattern(word).search(text))


def classify_slot(checklist: Checklist, *, text: str | None = None, caption: str | None = None) -> str | None:
    """The slot a photo belongs to, or None when it is not clear.

    A caption that names exactly one slot decides it: she said so. Otherwise
    the OCR text must hit at least two keywords of one slot, and more than
    any other slot.
    """
    if caption:
        named = [slot.id for slot in checklist.slots if _hits(slot.caption, caption.lower())]
        if len(named) == 1:
            return named[0]
    if text:
        lowered = text.lower()
        scores = sorted(((_hits(slot.keywords, lowered), slot.id) for slot in checklist.slots), reverse=True)
        (best, slot_id), (second, _) = scores[0], scores[1]
        if best >= 2 and best > second:
            return slot_id
    return None


@dataclass(frozen=True)
class Attached:
    document_id: int
    slot: str | None  # None while she has not picked one
    needs_choice: bool


def attach(
    conn: sqlite3.Connection,
    case_id: str,
    data: bytes,
    filename: str,
    *,
    mime_type: str | None = None,
    slot: str | None = None,
    caption: str | None = None,
    checklist: Checklist | None = None,
    read_text: Callable[..., str] | None = None,
) -> Attached:
    """Store a photo against the case and place it in a slot if that is clear.

    Only the slot is recorded. The photo itself is kept only with the case's
    ``keep_original`` consent (see store.save_document). It is read (OCR)
    only with the case's ``read_documents`` consent, and the text is used to
    place it and then dropped. Without consent it waits for her to pick.
    """
    checklist = checklist or load_checklist()
    mime = documents.validate_upload(data, filename, mime_type)
    if slot is not None:
        checklist.require(slot)
    else:
        slot = classify_slot(checklist, caption=caption)
        may_read = store.has_consent(conn, case_id, "read_documents")
        if slot is None and may_read and not config.USE_DOC_FIXTURES:
            slot = classify_slot(checklist, text=_ocr(read_text, data, filename, mime))

    row = store.save_document(
        conn,
        case_id,
        Extraction(doc_type=CLAIM_DOC, detected=slot is not None, source="checklist"),
        original=data,
        filename=filename,
        slot=slot,
    )
    return Attached(row["id"], slot, needs_choice=slot is None)


def _ocr(read_text, data: bytes, filename: str, mime: str) -> str | None:
    """OCR for placing a photo. Any failure means 'not clear', and she is asked."""
    try:
        return (read_text or documents.read_text)(data, filename, mime_type=mime)
    except (documents.ExtractionFailed, documents.UploadRejected, sarvam.SarvamUnavailable, sarvam.SarvamBadRequest) as exc:
        logger.info("Could not read a checklist photo, asking her instead: %s", exc)
        return None


def choose_slot(
    conn: sqlite3.Connection, case_id: str, document_id: int, slot: str, checklist: Checklist | None = None
) -> None:
    """Place a waiting photo in the slot she picked."""
    (checklist or load_checklist()).require(slot)
    if not store.assign_slot(conn, case_id, document_id, slot):
        raise LookupError(f"document {document_id} is not on case {case_id}")


# --- Checklist state ---------------------------------------------------------


@dataclass(frozen=True)
class ChecklistState:
    case_id: str
    filled: tuple[str, ...]
    missing: tuple[str, ...]
    pending_document_id: int | None  # a photo waiting for her to pick its slot

    @property
    def collected(self) -> int:
        return len(self.filled)

    @property
    def required(self) -> int:
        return len(self.filled) + len(self.missing)


def checklist_state(conn: sqlite3.Connection, case_id: str, checklist: Checklist | None = None) -> ChecklistState:
    checklist = checklist or load_checklist()
    have = store.filled_slots(conn, case_id)
    return ChecklistState(
        case_id=case_id,
        filled=tuple(s for s in checklist.slot_ids if s in have),
        missing=tuple(s for s in checklist.slot_ids if s not in have),
        pending_document_id=store.pending_document(conn, case_id, CLAIM_DOC),
    )


def checklist_facts(state: ChecklistState) -> dict[str, int]:
    """What the checklist tells the engine (feeds documents_incomplete)."""
    return {"documents_collected": state.collected, "documents_required": state.required}


def checklist_view(state: ChecklistState, checklist: Checklist | None = None) -> dict:
    """The checklist as the API returns it."""
    checklist = checklist or load_checklist()
    return {
        "case_id": state.case_id,
        "slots": [
            {"number": n, "slot": slot.id, "label": slot.label, "filled": slot.id in state.filled}
            for n, slot in enumerate(checklist.slots, start=1)
        ],
        "missing": list(state.missing),
        "pending_document_id": state.pending_document_id,
        **checklist_facts(state),
        "verified_by": checklist.verified_by,
        "unverified": checklist.verified_by == UNVERIFIED,
    }


def options(checklist: Checklist | None = None) -> list[dict]:
    checklist = checklist or load_checklist()
    return [{"number": n, "slot": slot.id, "label": slot.label} for n, slot in enumerate(checklist.slots, start=1)]


# --- Messages (English; the channel translates and speaks them) --------------


def status_message(state: ChecklistState, checklist: Checklist | None = None) -> str:
    checklist = checklist or load_checklist()
    if not state.missing:
        return f"All {state.required} documents are in. Nothing is missing."
    names = ", ".join(checklist.label(slot) for slot in state.missing)
    return f"Still missing, {len(state.missing)} of {state.required}: {names}."


def options_message(checklist: Checklist | None = None) -> str:
    checklist = checklist or load_checklist()
    lines = [f"{n}. {slot.label}" for n, slot in enumerate(checklist.slots, start=1)]
    return "Which document is this? Reply with its number.\n" + "\n".join(lines)


# --- Respondent routing (N5) -------------------------------------------------

# Where each respondent's legal name is printed: (kind, document types, field).
_NAME_FIELDS = (
    (routing.INSURER, ("policy", "letter"), "insurer"),
    (routing.LENDER, ("kfs",), "lender_name"),
)


def respondent_names(conn: sqlite3.Connection, case_id: str) -> dict[str, str]:
    """Legal names known for this case: read confidently from her documents, or configured."""
    names: dict[str, str] = {}
    for kind, doc_types, field_name in _NAME_FIELDS:
        for doc in store.case_documents(conn, case_id, doc_types):
            read = doc["fields"].get(field_name) or {}
            if read.get("value") and (read.get("confidence") or 0) >= config.CONFIDENCE_GATE:
                names[kind] = read["value"]
                break
    if config.DISTRIBUTOR_LEGAL_NAME:
        names[routing.DISTRIBUTOR] = config.DISTRIBUTOR_LEGAL_NAME
    return names


def route_case(
    conn: sqlite3.Connection, case_id: str, product: str | None, grievance_class: str | None
) -> routing.Route | None:
    """Work out who owes this case an answer, record it on the case, and log it."""
    found = routing.route(product, grievance_class, names=respondent_names(conn, case_id))
    store.set_route(
        conn,
        case_id,
        product,
        found.respondent if found else None,
        found.respondent_name if found else None,
        found.distributor_owned if found else None,
    )
    store.record_event(conn, case_id, "case_routed", {
        "product": product,
        "grievance_class": grievance_class,
        "respondent": found.respondent if found else None,
        "respondent_name": found.respondent_name if found else None,
        "distributor_owned": found.distributor_owned if found else None,
        "ladder": found.ladder if found else None,
    })
    return found


def start_case_clock(
    conn: sqlite3.Connection, case_id: str, started_on: date, step: str | None = None
) -> routing.Clock:
    """Start the clock on this case's own respondent ladder, and log it."""
    routed = store.latest_event(conn, case_id, "case_routed")
    detail = routed["detail"] if routed else {}
    if not detail.get("respondent"):
        raise LookupError(f"case {case_id} has no respondent yet, so no clock can start")
    found = routing.route(
        detail.get("product"), detail.get("grievance_class"),
        names={detail["respondent"]: detail.get("respondent_name")},
    )
    clock = routing.start_clock(found, started_on, ladders.load_steps(), step)
    store.record_event(conn, case_id, "clock_started", {
        "ladder": clock.ladder,
        "step": clock.step,
        "started_on": clock.started_on.isoformat(),
        "respond_by": clock.respond_by.isoformat() if clock.respond_by else None,
        "verified_by": clock.verified_by,
    })
    return clock


# --- Readiness (N1 over a case) ----------------------------------------------

_FACT_TYPES = typing.get_type_hints(le.Facts)


def facts_from_json(raw: dict[str, Any]) -> le.Facts:
    """A fact sheet from JSON. Dates arrive as YYYY-MM-DD. Unknown facts are refused."""
    if not isinstance(raw, dict):
        raise ValueError("facts must be an object")
    values = {}
    for name, value in raw.items():
        if name not in le.FACT_NAMES:
            raise ValueError(f"unknown fact {name!r}")
        if isinstance(value, str) and "date" in str(_FACT_TYPES[name]):
            value = date.fromisoformat(value)
        values[name] = value
    return le.Facts(**values)


def facts_to_json(facts: le.Facts) -> dict[str, Any]:
    """The known facts as JSON: dates as YYYY-MM-DD, unknown facts left out."""
    out = {}
    for name in le.FACT_NAMES:
        value = getattr(facts, name)
        if value is not None:
            out[name] = value.isoformat() if isinstance(value, date) else value
    return dict(sorted(out.items()))


def case_detail(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    """Everything the case page shows, read from the case and its latest events."""
    case = store.get_case(conn, case_id)
    if case is None:
        return None
    routed = (store.latest_event(conn, case_id, "case_routed") or {}).get("detail") or {}
    checked = (store.latest_event(conn, case_id, "readiness_checked") or {}).get("detail") or {}
    clocked = store.latest_event(conn, case_id, "clock_started")

    route_view = None
    if routed.get("respondent"):
        found = routing.route(
            routed.get("product"), routed.get("grievance_class"),
            names={routed["respondent"]: routed.get("respondent_name")},
        )
        if found is not None:
            steps = ladders.load_steps()
            route_view = {
                "situation": found.situation,
                "ladder": found.ladder,
                "steps": [
                    {"step": step, "label": steps[step].label, "respond_within_days": steps[step].days,
                     "verified_by": steps[step].verified_by}
                    for step in found.steps
                ],
            }
    state = checklist_state(conn, case_id)
    return {
        "case_id": case_id,
        "example": bool(case.get("is_example")),
        "language": case.get("language") or config.DEFAULT_LANGUAGE,
        "product": routed.get("product"),
        "grievance_class": routed.get("grievance_class"),
        "respondent": routed.get("respondent"),
        "respondent_name": routed.get("respondent_name"),
        "distributor_owned": routed.get("distributor_owned"),
        "route": route_view,
        "clock": clocked["detail"] if clocked else None,
        "verdict": checked.get("outcome"),
        "checklist": checklist_facts(state),
        "drafts": store.case_drafts(conn, case_id),
    }


def check_readiness(
    conn: sqlite3.Connection, case_id: str, facts: le.Facts, ladder_name: str = "insurance_health_claim"
) -> le.Verdict:
    """Run the readiness ladder for a case and log what it found."""
    ladder = ladders.load(ladder_name)
    verdict = le.evaluate(facts, ladder.rules)
    store.record_event(
        conn, case_id, "readiness_checked", {"ladder": ladder.id, "outcome": verdict.outcome, "facts": facts_to_json(facts)}
    )
    if verdict.outcome == le.DO_NOT_FILE_YET:
        store.record_event(conn, case_id, "claim_stopped", {
            "rules": [hit.rule_id for hit in verdict.blocks],
            "possible_on": verdict.possible_on.isoformat() if verdict.possible_on else None,
        })
    for hit in verdict.deductions:
        store.record_event(conn, case_id, "deduction_explained", {
            "rule": hit.rule_id,
            "deduction": hit.values.get("deduction"),
            "pending": hit.values.get("pending"),
        })
    return verdict
