"""Complaints she asked a person to take on, for the console's Complaints tab.

A complaint is registered when the chat could not settle it, or she asked to talk to someone. It is an
event (``complaint_registered``) with a short summary and how to reach her; the console reads events
only. An agent marks it Resolved or Pending (``complaint_status``, the latest mark stands). Nothing is
sent to the insurer: registering means Praman's own team can see it.

Her words are masked (``redact``) and the policy number is kept as its last four characters only.
How to reach her is shown on this tab and nowhere else (not in the brief), and goes with the case when
she says "delete everything".
"""

from __future__ import annotations

import re
from typing import Any

from app import store
from app.services.redact import redact
from app.store import Store

CONSENT = "register_complaint"
STATUSES = ("pending", "resolved")
MAX_TEXT = 500

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{9})(?!\d)")
_POLICY_AFTER_WORD = re.compile(r"\bpolicy\s*(?:no\.?|number|num|#)?\s*(?:is|:)?\s*([A-Za-z0-9][A-Za-z0-9/\-]{5,24})", re.I)
_LONG_NUMBER = re.compile(r"(?<![\w-])(?=[A-Za-z0-9/\-]{6,25}(?![\w-]))(?=[A-Za-z0-9/\-]*\d{5})[A-Za-z0-9/\-]+")


def parse_contact(text: str) -> str | None:
    """An email or an Indian mobile number in what she typed, normalised; None if there is neither."""
    if match := _EMAIL.search(text or ""):
        return match.group(0).lower()
    if match := _PHONE.search(text or ""):
        return "+91" + match.group(1)
    return None


def mask_contact(contact: str | None) -> str:
    """r***@gmail.com or +91******3210: enough for her to recognise it."""
    if not contact:
        return "no contact given"
    if "@" in contact:
        name, _, domain = contact.partition("@")
        return f"{name[:1]}***@{domain}"
    return contact[:3] + "*" * max(len(contact) - 7, 0) + contact[-4:]


def policy_last4(text: str) -> str | None:
    """The last four characters of a policy number she typed, or None. The rest is never kept."""
    found = _POLICY_AFTER_WORD.search(text or "")
    token = found.group(1) if found else None
    if token is None:
        found = _LONG_NUMBER.search(text or "")
        token = found.group(0) if found else None
    return token[-4:] if token else None


def register(
    conn: Store,
    case_id: str,
    text: str,
    contact: str | None,
    policy_last4_: str | None = None,
    product: str | None = None,
    grievance_class: str | None = None,
) -> int:
    """Record the complaint and return its number. Needs her ``register_complaint`` consent."""
    if not store.has_consent(conn, case_id, CONSENT):
        raise PermissionError("She has not agreed to register a complaint.")
    return store.record_event(conn, case_id, "complaint_registered", {
        "text": redact(text)[:MAX_TEXT],
        "contact": contact,
        "policy_last4": policy_last4_,
        "product": product,
        "grievance_class": grievance_class,
        "channel": "web" if ((store.get_case(conn, case_id) or {}).get("channel_user") or "").startswith("web:") else "other",
    })


def open_complaint(conn: Store, case_id: str) -> dict | None:
    """The complaint on this case that no agent has resolved, if she already registered one."""
    for event in reversed(store.case_events(conn, case_id, "complaint_registered")):
        if _status_of(conn, event["id"]) == "pending":
            return {"id": event["id"], **event["detail"]}
    return None


def _status_of(conn: Store, complaint_id: int) -> str:
    marks = [
        e for e in conn.db.events.find({"kind": "complaint_status", "detail.complaint_id": complaint_id}).sort("id", 1)
    ]
    return marks[-1]["detail"].get("status", "pending") if marks else "pending"


def listing(conn: Store, status: str | None = "pending") -> list[dict[str, Any]]:
    """Registered complaints, newest first. ``status`` is pending, resolved, or None for all."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; use one of {list(STATUSES)} or none")
    marks: dict[int, str] = {}
    for mark in conn.db.events.find({"kind": "complaint_status"}).sort("id", 1):
        marks[mark["detail"].get("complaint_id")] = mark["detail"].get("status", "pending")
    out = []
    for event in conn.db.events.find({"kind": "complaint_registered"}).sort("id", -1):
        state = marks.get(event["id"], "pending")
        if status is not None and state != status:
            continue
        case = store.get_case(conn, event["case_id"]) or {}
        detail = event["detail"]
        out.append({
            "id": event["id"],
            "case_id": event["case_id"],
            "status": state,
            "registered_at": event["at"],
            "text": detail.get("text", ""),
            "contact": detail.get("contact"),
            "policy_last4": detail.get("policy_last4"),
            "product": detail.get("product") or case.get("product"),
            "grievance_class": detail.get("grievance_class"),
            "respondent_name": case.get("respondent_name"),
            "example": bool(case.get("is_example")),
        })
    return out


def set_status(conn: Store, complaint_id: int, status: str) -> bool:
    """An agent marks a complaint Resolved or Pending. False when there is no such complaint."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES)}")
    event = conn.db.events.find_one({"id": complaint_id, "kind": "complaint_registered"})
    if event is None:
        return False
    store.record_event(conn, event["case_id"], "complaint_status", {"complaint_id": complaint_id, "status": status})
    return True


def counts(conn: Store) -> dict[str, int]:
    """For the console's counters: how many were registered, and how many still wait."""
    everything = listing(conn, None)
    return {
        "complaints_registered": len(everything),
        "complaints_pending": sum(item["status"] == "pending" for item in everything),
    }
