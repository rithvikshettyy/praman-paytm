"""n8n: the edge that delivers an approved letter and keeps its response clock.

Praman decides what goes to whom and which step comes next; n8n (a workflow per
insurer, built and edited outside this repo's deploys) decides how and when it
is delivered. Three calls cross the line:

* ``dispatch``: she approved a letter and agreed to contact the insurer; the
  letter goes to the n8n workflow, signed with a shared secret.
* ``delivered`` / ``failed``: the workflow reports back. Only ``delivered``
  moves a draft to sent and starts the clock on that step's own window.
* ``clock_due``: the workflow woke at the end of the window and asks what to do.
  The ladder answers (escalate to the next step, or ask a person); the workflow
  carries it out. A resolved case, a deleted case or revoked consent stops it.

n8n is another party, so the letter is redacted on the way out and only what
delivery needs is sent. Nothing here says a letter was filed: sent means n8n
reported it went out, nothing more.
"""

from __future__ import annotations

import hmac
import sqlite3
from datetime import date, timedelta
from typing import Callable

import httpx

from app import cases, config, store
from app.core import ladders, routing
from app.services.redact import redact

CONSENT = "contact_insurer"


class NotConfigured(RuntimeError):
    """No n8n workflow URL or shared secret is set, so nothing can be sent."""


class NotReady(ValueError):
    """The letter cannot be sent yet: not approved, or she has not agreed to contact the insurer."""


class DeliveryUnavailable(RuntimeError):
    """n8n did not accept the letter. It stays approved and can be sent again."""


def authentic(header: str | None) -> bool:
    """True only when a secret is set and the caller sent it. No secret set means every callback is refused."""
    return bool(config.N8N_SECRET) and hmac.compare_digest(header or "", config.N8N_SECRET)


def _route(conn: sqlite3.Connection, case_id: str) -> routing.Route | None:
    routed = (store.latest_event(conn, case_id, "case_routed") or {}).get("detail") or {}
    if not routed.get("respondent"):
        return None
    return routing.route(
        routed.get("product"), routed.get("grievance_class"),
        names={routed["respondent"]: routed.get("respondent_name")},
    )


def _post(url: str, payload: dict) -> None:
    try:
        response = httpx.post(
            url, json=payload, headers={"x-praman-secret": config.N8N_SECRET}, timeout=config.N8N_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DeliveryUnavailable("The delivery workflow did not accept the letter.") from exc


def dispatch(conn: sqlite3.Connection, case_id: str, draft_id: int, post: Callable[[str, dict], None] = _post) -> dict:
    """Hand an approved letter to the n8n workflow. Refused without approval and her consent."""
    if not config.N8N_DISPATCH_URL or not config.N8N_SECRET:
        raise NotConfigured("Sending is not set up on this server.")
    draft = store.get_draft(conn, case_id, draft_id)
    if draft is None:
        raise LookupError(f"no draft {draft_id} on case {case_id}")
    if draft["status"] != "approved":
        raise NotReady("Approve the letter first." if draft["status"] == "drafted" else "This letter was already sent.")
    if not store.has_consent(conn, case_id, CONSENT):
        raise NotReady("Agree to Praman contacting the insurer on your behalf before sending.")

    found = _route(conn, case_id)
    step = found.first_step if found else None
    days = ladders.load_steps()[step].days if step else None
    payload = {
        "case_id": case_id,
        "draft_id": draft_id,
        "kind": draft["kind"],
        "addressee": draft["addressee"],
        "letter": redact(draft["text"]),
        "step": step,
        "respond_within_days": days,
        "callback_base": config.PUBLIC_BASE_URL,
    }
    post(config.N8N_DISPATCH_URL, payload)
    store.set_draft_status(conn, case_id, draft_id, "sending")
    store.record_event(conn, case_id, "letter_dispatched", {"draft_id": draft_id, "kind": draft["kind"], "step": step})
    return {"draft_id": draft_id, "status": "sending", "message": "Handed to the delivery workflow"}


def _clock_view(event: dict | None) -> dict | None:
    if event is None:
        return None
    detail = event["detail"]
    return {"step": detail.get("step"), "respond_by": detail.get("respond_by"), "verified_by": detail.get("verified_by")}


def delivered(conn: sqlite3.Connection, case_id: str, draft_id: int, channel: str, today: date | None = None) -> dict:
    """n8n confirms the letter went out. Marks it sent and starts the clock on the step it was addressed to."""
    draft = store.get_draft(conn, case_id, draft_id)
    if draft is None:
        raise LookupError(f"no draft {draft_id} on case {case_id}")
    if draft["status"] == "sent":  # n8n retried the callback: same answer, no second event or clock
        return {"draft_id": draft_id, "status": "sent", "clock": _clock_view(store.latest_event(conn, case_id, "clock_started"))}

    sent = (store.latest_event(conn, case_id, "letter_dispatched") or {}).get("detail") or {}
    store.set_draft_status(conn, case_id, draft_id, "sent")
    store.record_event(conn, case_id, "letter_delivered", {"draft_id": draft_id, "channel": channel[:40]})
    try:
        cases.start_case_clock(conn, case_id, today or date.today(), sent.get("step"))
    except (LookupError, ValueError):  # no route or no known step: no clock, and none is claimed
        pass
    return {"draft_id": draft_id, "status": "sent", "clock": _clock_view(store.latest_event(conn, case_id, "clock_started"))}


def failed(conn: sqlite3.Connection, case_id: str, draft_id: int, reason: str) -> dict:
    """n8n gave up after its retries. The letter goes back to approved so she can send it again."""
    if store.get_draft(conn, case_id, draft_id) is None:
        raise LookupError(f"no draft {draft_id} on case {case_id}")
    store.set_draft_status(conn, case_id, draft_id, "approved")
    store.record_event(conn, case_id, "delivery_failed", {"draft_id": draft_id, "reason": redact(reason)[:200]})
    return {"draft_id": draft_id, "status": "approved"}


def _stop(reason: str) -> dict:
    return {"action": "stop", "reason": reason}


def clock_due(
    conn: sqlite3.Connection,
    case_id: str,
    notify: Callable[[dict, str], None] | None = None,
    today: date | None = None,
) -> dict:
    """The workflow woke at the end of the window: what happens next, by the case's own ladder.

    ``notify(case, text_in_english)`` tells her, when she has a messaging channel; a failure there
    never fails the answer. The ladder is read here, never in n8n, so the workflow only carries it out.
    """
    case = store.get_case(conn, case_id)
    if case is None:
        return _stop("case_deleted")
    status = (store.latest_event(conn, case_id, "case_status") or {}).get("detail", {}).get("status")
    if status == "resolved":
        return _stop("case_resolved")
    if not store.has_consent(conn, case_id, CONSENT):
        return _stop("consent_withdrawn")
    clock = store.latest_event(conn, case_id, "clock_started")
    if clock is None or not clock["detail"].get("respond_by"):
        return _stop("no_known_deadline")  # no window is known, so no deadline is claimed
    handled = store.latest_event(conn, case_id, "clock_due")
    if handled is not None and handled["id"] > clock["id"]:
        return _stop("already_handled")
    respond_by = date.fromisoformat(clock["detail"]["respond_by"])
    if (today or date.today() + timedelta(days=config.N8N_DEMO_DAYS_AHEAD)) <= respond_by:
        return {"action": "wait", "respond_by": respond_by.isoformat()}

    found = _route(conn, case_id)
    windows = ladders.load_steps()
    step = clock["detail"]["step"]
    following = None
    if found is not None and step in found.steps:
        later = found.steps[found.steps.index(step) + 1:]
        following = later[0] if later else None
    action = "escalate" if following else "ask_agent"
    who = (found.respondent_name if found else None) or "The insurer"
    label = windows[following].label if following else None
    text = (
        f"{who} has not replied by {respond_by.strftime('%d %b %Y')}. "
        + (f"The next step is: {label}. Open your case on Praman to draft that letter."
           if following else "Praman is asking a person to help with the next step.")
    )
    store.record_event(conn, case_id, "clock_due", {"step": step, "action": action, "next_step": following})
    if notify is not None:
        try:
            notify(case, text)
        except Exception:  # she can still see it on her case page; the answer to n8n stands
            pass
    return {
        "action": action,
        "case_id": case_id,
        "respondent_name": found.respondent_name if found else None,
        "step": step,
        "next_step": following,
        "next_label": label,
        "message": text,
        "brief_path": f"/api/case/{case_id}/brief",
    }
