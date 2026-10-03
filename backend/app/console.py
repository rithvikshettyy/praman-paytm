"""Distributor Console reads (PRD-PAYTM N8).

Three blocks: the case list, the headline ("Of N cases, X needed <the
distributor>") and the counters. Every number is counted from event rows,
through the ``console_cases`` view or the events table directly. Nothing is
projected, priced or estimated: "what would this save?" is answered by
multiplying X by the distributor's own cost per ticket, which only they know.
"""

from __future__ import annotations

from typing import Any

from app import config, store
from app.store import Store

# A case needed the distributor when the answer is the distributor's own, or when she asked for a
# person after an answer did not solve her question. Routed away: the insurer or lender owes it and
# she did not ask for a person.
def _needs(row: dict) -> bool:
    return bool(row["distributor_owned"] or row["agent_requested"])


def _routed_away(row: dict) -> bool:
    return row["distributor_owned"] is not None and not row["distributor_owned"] and not row["agent_requested"]


# An agent marks a case resolved on the console and it leaves the list. Nothing is deleted: the case
# and its events stay, so every number still counts it, and the Resolved tab brings it back.
def _open(row: dict) -> bool:
    return row["agent_status"] != "resolved"


FILTERS = {
    "needs_paytm": lambda row: _needs(row) and _open(row),
    "routed_away": lambda row: _routed_away(row) and _open(row),
    "resolved": lambda row: row["agent_status"] == "resolved",
}


def _owned(value: Any) -> bool | None:
    return None if value is None else bool(value)


def case_list(conn: Store, filter: str | None = None) -> list[dict[str, Any]]:
    """One row per case, newest activity first. ``filter`` is needs_paytm | routed_away | resolved;
    without one, every case an agent has not marked resolved."""
    if filter is not None and filter not in FILTERS:
        raise ValueError(f"unknown filter {filter!r}; use one of {sorted(FILTERS)}")
    keep = FILTERS[filter] if filter else _open
    # A case nobody has done anything with (a browser that only opened the site) is not a case yet.
    rows = [
        row for row in store.console_rows(conn)
        if (row["last_event_at"] is not None or row["has_documents"]) and keep(row)
    ]
    rows.sort(key=lambda row: row["case_id"])
    rows.sort(key=lambda row: row["last_event_at"] or row["created_at"], reverse=True)
    return [
        {
            "case_id": row["case_id"],
            "example": bool(row["is_example"]),
            "product": row["product"],
            "grievance_class": row["grievance_class"],
            "respondent": row["respondent"],
            "respondent_name": row["respondent_name"],
            "distributor_owned": _owned(row["distributor_owned"]),
            "asked_for_person": bool(row["agent_requested"]),
            "needs_distributor": _needs(row),
            "status": row["agent_status"],
            "verdict": row["verdict"],
            "clock": {
                "step": row["clock_step"],
                "respond_by": row["clock_respond_by"],
                "verified_by": row["clock_verified_by"],
            }
            if row["clock_step"]
            else None,
            "last_event_at": row["last_event_at"],
        }
        for row in rows
    ]


def _events(conn: Store, kind: str) -> list[dict]:
    return list(conn.db.events.find({"kind": kind}))


def metrics(conn: Store) -> dict[str, Any]:
    """The headline and the counters, all from event rows."""
    rows = store.console_rows(conn)
    counted = [row for row in rows if row["distributor_owned"] is not None or row["agent_requested"]]
    cases = len(counted)
    needed = sum(_needs(row) for row in counted)
    away = sum(_routed_away(row) for row in counted)

    why: dict[str, set] = {}
    for event in _events(conn, "claim_stopped"):
        for rule in event["detail"].get("rules") or []:
            why.setdefault(rule, set()).add(event["case_id"])

    def count(kind: str) -> int:
        return conn.db.events.count_documents({"kind": kind})

    def cases_with(kind: str, keep=lambda detail: True) -> int:
        return len({e["case_id"] for e in _events(conn, kind) if keep(e["detail"])})

    return {
        "headline": {
            "cases": cases,
            "needed_distributor": needed,
            "distributor": config.DISTRIBUTOR_SHORT_NAME,
            "text": f"Of {cases} {'case' if cases == 1 else 'cases'}, {needed} needed {config.DISTRIBUTOR_SHORT_NAME}.",
        },
        "counters": {
            # readiness checks run (each check counts)
            "readiness_checks_run": count("readiness_checked"),
            # claims stopped before filing (cases), and why (cases per blocking rule)
            "claims_stopped": cases_with("claim_stopped"),
            "claims_stopped_why": {rule: len(found) for rule, found in sorted(why.items())},
            # known deductions explained (each explanation counts)
            "known_deductions_explained": count("deduction_explained"),
            "coverage_queries_drafted": count("coverage_query_drafted"),
            "escalations_drafted": count("escalation_drafted"),
            # cases whose latest route sends them to someone other than the distributor
            "cases_routed_away": away,
            # answers given in the chat, and how many she confirmed solved her question ("Did this
            # solve it?"). Only a Yes counts: an answer nobody rated is not counted as solved.
            "answers_given": sum(e["detail"].get("status") == "answered" for e in _events(conn, "answer_given")),
            "answers_confirmed_solved": len({
                e["detail"].get("answer_id") for e in _events(conn, "answer_feedback") if e["detail"].get("solved") is True
            }),
            # cases an agent marked resolved (the latest mark on each case)
            "cases_marked_resolved": sum(row["agent_status"] == "resolved" for row in rows),
            # cases where she asked for a person after an answer
            "asked_for_a_person": cases_with("agent_requested"),
            # cases where the paper checks caught something an insurer would query, before filing
            "insurer_queries_caught": cases_with("papers_checked", lambda detail: bool(detail.get("fix"))),
        },
    }
