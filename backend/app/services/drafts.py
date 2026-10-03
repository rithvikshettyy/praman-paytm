"""Drafts: letters to whoever owes her an answer.

Every draft is addressed to the respondent's legal name and the step on their
own ladder; with no name it is refused and the name asked for, never "the
company". The body comes from the rules the latest readiness check found
(grounds first, then blocks), each with its first-person ``letter`` line from
the ladder YAML. She reads it back in her language and approves it. Approval
means approved and ready to send: nothing is sent anywhere, and nothing here
ever calls it filed.
"""

from __future__ import annotations

from typing import Mapping

from app import cases, config, store
from app.store import Store
from app.core import ladder_engine as le
from app.core import ladders, routing
from app.core.routing import Route, StepWindow
from app.services import i18n

APPROVED_MESSAGE = "Approved and ready to send"
ESCALATION = "escalation"
COVERAGE_QUERY = "coverage_query"

# How an escalation opens, by the respondent ladder it goes up.
_OPENINGS = {
    "insurance_claim": "I am writing about the decision on my claim.",
    "motor_claim": "I am writing about the decision on my motor claim.",
    "mis_selling": "I am writing about how this policy was sold to me.",
    "platform_support": "I am writing about a payment problem on my account.",
    "lending_servicing": "I am writing about my loan.",
}


class RespondentUnknown(ValueError):
    """There is no one to address yet: no route, or no legal name."""


def addressee(route: Route | None, steps: Mapping[str, StepWindow], step: str | None = None) -> str:
    """The 'To:' line of a draft, e.g. 'Grievance cell, Example General Insurance Company Ltd'."""
    if route is None:
        raise RespondentUnknown("This case has no respondent yet, so there is no one to write to.")
    if not route.respondent_name:
        raise RespondentUnknown(
            f"The {route.respondent}'s legal name is not known yet. Ask for it, or read it from her "
            "policy, letter or key fact statement, before drafting."
        )
    step = step or route.first_step
    if step not in route.steps:
        raise ValueError(f"{step!r} is not on the {route.ladder} ladder")
    return f"{steps[step].label}, {route.respondent_name}"


def compose(conn: Store, case_id: str, language: str | None = None) -> dict:
    """Draft the next letter for a case, save it as drafted, and log it."""
    case = store.get_case(conn, case_id)
    if case is None:
        raise LookupError(f"no case {case_id}")
    routed = (store.latest_event(conn, case_id, "case_routed") or {}).get("detail") or {}
    found = None
    if routed.get("respondent"):
        found = routing.route(
            routed.get("product"), routed.get("grievance_class"),
            names={routed["respondent"]: routed.get("respondent_name")},
        )
    to = addressee(found, ladders.load_steps())

    checked = (store.latest_event(conn, case_id, "readiness_checked") or {}).get("detail") or {}
    lines, unverified = [], False
    verdict = None
    if checked.get("facts") and checked.get("ladder"):
        ladder = ladders.load(checked["ladder"])
        verdict = le.evaluate(cases.facts_from_json(checked["facts"]), ladder.rules)
        lines, unverified = ladders.letter_lines(verdict, ladder)

    if verdict is not None and verdict.grounds:
        kind = ESCALATION
    elif (verdict is not None and verdict.blocks) or found.ladder == "coverage_question":
        kind = COVERAGE_QUERY
    else:
        kind = ESCALATION

    if kind == ESCALATION:
        subject, opening, close = (
            "Request to review a decision on my case",
            _OPENINGS.get(found.ladder, "I am writing about a problem with my case."),
            "Please review this and reply to me in writing.",
        )
    else:
        subject, opening, close = (
            "Coverage query about my policy",
            "I am writing to ask how my policy applies to my treatment.",
            "Please reply to me in writing.",
        )
    body = [f"To: {to}", f"Subject: {subject}", "", "Dear Sir or Madam,", "", opening]
    if lines:
        body += [""] + [f"- {line}" for line in lines]
    body += ["", close, "", "Yours faithfully,", "[Your name]", "[Policy or claim number]"]
    text = "\n".join(body)

    draft = store.save_draft(conn, case_id, kind, to, text, unverified)
    store.record_event(conn, case_id, f"{kind}_drafted", {"draft_id": draft["id"], "kind": kind})
    reader = language if language in config.SUPPORTED_LANGUAGES else case.get("language") or config.DEFAULT_LANGUAGE
    return {**draft, "readback": i18n.translate(text, reader), "readback_language": reader}


def approve(conn: Store, case_id: str, draft_id: int) -> dict:
    """She approved the draft: it is approved and ready to send. Nothing is sent."""
    draft = store.approve_draft(conn, case_id, draft_id)
    if draft is None:
        raise LookupError(f"no draft {draft_id} on case {case_id}")
    store.record_event(conn, case_id, "draft_approved", {"draft_id": draft_id, "kind": draft["kind"]})
    return {"draft": draft, "message": APPROVED_MESSAGE}
