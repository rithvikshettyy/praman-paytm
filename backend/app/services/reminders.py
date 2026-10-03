"""Premium reminders: she asks to be reminded, confirms the date, and n8n emails her before it.

Praman decides which dates and whether a reminder may still go out; n8n only waits and sends. The
date is hers to confirm (a date read from her policy is offered, never assumed), and the message
says the premium *may be* due: Praman cannot see payments, so it never says one is overdue or paid.

What leaves the process, only with her ``premium_reminders`` consent: her email address, the due date,
the dates to remind on, the case id. No policy number, no document text, no phone number.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Callable

from app import config, store
from app.services import n8n

CONSENT = "premium_reminders"

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
            ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
            ("nov", "november"), ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_NUMERIC = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s,\-]+([a-z]{3,9})\.?[\s,\-]+(\d{4})\b", re.I)
_MONTH_DAY = re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(text: str) -> date | None:
    """The first calendar date in ``text`` (14/03/2027, 14-03-2027, 2027-03-14, 14 March 2027, March 14, 2027).
    Numbers are day first, as written in India. None when there is no date, or it is not a real one."""
    text = text or ""
    if match := _ISO.search(text):
        return _make(int(match[1]), int(match[2]), int(match[3]))
    if match := _NUMERIC.search(text):
        return _make(int(match[3]), int(match[2]), int(match[1]))
    if match := _DAY_MONTH.search(text):
        month = _MONTHS.get(match[2].lower())
        if month:
            return _make(int(match[3]), month, int(match[1]))
    if match := _MONTH_DAY.search(text):
        month = _MONTHS.get(match[1].lower())
        if month:
            return _make(int(match[3]), month, int(match[2]))
    return None


def plan(due: date, today: date, offsets: tuple[int, ...] | None = None) -> list[date]:
    """The days to remind on: each offset before the due date that is still ahead of ``today``
    (a reminder is never set for a day that has passed), earliest first, none twice."""
    offsets = config.REMINDER_DAYS_BEFORE if offsets is None else offsets
    days = {due - timedelta(days=offset) for offset in offsets}
    return sorted(day for day in days if day >= today)


def _log(due: date, remind_on: list[date]) -> dict:
    return {"due_date": due.isoformat(), "remind_on": [day.isoformat() for day in remind_on]}


def is_set_up() -> bool:
    return bool(config.N8N_REMINDER_URL and config.N8N_SECRET and config.PUBLIC_BASE_URL and config.REMINDER_EMAIL)


def masked_email(address: str | None = None) -> str:
    """r***@gmail.com: enough for her to recognise it, not enough to read it out."""
    address = address if address is not None else config.REMINDER_EMAIL
    name, _, domain = address.partition("@")
    return f"{name[:1]}***@{domain}" if domain else "your email"


def schedule(
    conn: sqlite3.Connection,
    case_id: str,
    due: date,
    today: date | None = None,
    post: Callable[[str, dict], None] = n8n._post,
) -> dict:
    """Hand the reminder dates to the n8n workflow. Needs her consent; a new one replaces any earlier."""
    if not is_set_up():
        raise n8n.NotConfigured("Reminders are not set up on this server.")
    if not store.has_consent(conn, case_id, CONSENT):
        raise n8n.NotReady("Agree to Praman emailing you reminders first.")
    remind_on = plan(due, today or date.today())
    if not remind_on:
        raise n8n.NotReady("That date is too close or has passed, so there is nothing to remind you about.")
    reminder_id = uuid.uuid4().hex[:12]
    post(config.N8N_REMINDER_URL, {
        "case_id": case_id,
        "reminder_id": reminder_id,
        "email": config.REMINDER_EMAIL,
        "due_date": due.isoformat(),
        "remind_on": [day.isoformat() for day in remind_on],
        "callback_base": config.PUBLIC_BASE_URL,
    })
    store.record_event(conn, case_id, "reminder_scheduled", {"reminder_id": reminder_id, **_log(due, remind_on)})
    return {"reminder_id": reminder_id, **_log(due, remind_on)}


def current(conn: sqlite3.Connection, case_id: str) -> dict | None:
    """The reminder still in force: the latest one set, unless she cancelled it afterwards."""
    scheduled = store.latest_event(conn, case_id, "reminder_scheduled")
    if scheduled is None:
        return None
    cancelled = store.latest_event(conn, case_id, "reminder_cancelled")
    if cancelled is not None and cancelled["id"] > scheduled["id"]:
        return None
    return scheduled["detail"]


def cancel(conn: sqlite3.Connection, case_id: str) -> bool:
    """She asked to stop. True when a reminder was in force. Waiting ones are refused when they wake."""
    if current(conn, case_id) is None:
        return False
    store.record_event(conn, case_id, "reminder_cancelled", {"reason": "asked"})
    return True


def message(due: date, on: date) -> tuple[str, str]:
    """Subject and body. 'May be due': Praman cannot see whether she has paid."""
    days = (due - on).days
    when = "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"
    return (
        f"Reminder: your insurance premium may be due {when}",
        f"Reminder from Praman: your insurance premium may be due on {due.strftime('%d %b %Y')} ({when}).\n\n"
        "Please check the amount and date with your insurer, and pay on time so your cover does not lapse. "
        "Praman cannot see or make payments, so ignore this if you have already paid.\n\n"
        "To stop these reminders, reply \"stop reminders\" in your Praman chat.",
    )


def due(conn: sqlite3.Connection, case_id: str, reminder_id: str, on: str) -> dict:
    """The workflow woke for one reminder date. It goes out only if the case, her consent and this
    reminder are all still in force, and that date was not already sent."""
    if store.get_case(conn, case_id) is None:
        return {"send": False, "reason": "case_deleted"}
    if not store.has_consent(conn, case_id, CONSENT):
        return {"send": False, "reason": "consent_withdrawn"}
    scheduled = current(conn, case_id)
    if scheduled is None:
        return {"send": False, "reason": "cancelled"}
    if scheduled.get("reminder_id") != reminder_id:
        return {"send": False, "reason": "replaced"}
    try:
        day = datetime.strptime(on, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"send": False, "reason": "bad_date"}
    if on not in scheduled.get("remind_on", []):
        return {"send": False, "reason": "not_planned"}
    if any(e["detail"].get("reminder_id") == reminder_id and e["detail"].get("on") == on
           for e in store.case_events(conn, case_id, "reminder_sent")):
        return {"send": False, "reason": "already_sent"}
    subject, body = message(date.fromisoformat(scheduled["due_date"]), day)
    store.record_event(conn, case_id, "reminder_sent", {"reminder_id": reminder_id, "on": on})
    return {"send": True, "subject": subject, "message": body}
