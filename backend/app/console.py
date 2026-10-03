"""Distributor Console reads (PRD-PAYTM N8).

Three blocks: the case list, the headline ("Of N cases, X needed <the
distributor>") and the counters. Every number is counted from event rows,
through the ``console_cases`` view or the events table directly. Nothing is
projected, priced or estimated: "what would this save?" is answered by
multiplying X by the distributor's own cost per ticket, which only they know.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import config

# A case needed the distributor when the answer is the distributor's own, or when she asked for a
# person after an answer did not solve her question. Routed away: the insurer or lender owes it and
# she did not ask for a person.
NEEDS = "(distributor_owned = 1 OR agent_requested = 1)"
# An agent marks a case resolved on the console and it leaves the list. Nothing is deleted: the case
# and its events stay, so every number still counts it, and the Resolved tab brings it back.
OPEN = "agent_status != 'resolved'"
FILTERS = {
    "needs_paytm": f"{NEEDS} AND {OPEN}",
    "routed_away": f"(distributor_owned = 0 AND agent_requested = 0) AND {OPEN}",
    "resolved": "agent_status = 'resolved'",
}


def _owned(value: Any) -> bool | None:
    return None if value is None else bool(value)


def case_list(conn: sqlite3.Connection, filter: str | None = None) -> list[dict[str, Any]]:
    """One row per case, newest activity first. ``filter`` is needs_paytm | routed_away | resolved;
    without one, every case an agent has not marked resolved."""
    if filter is not None and filter not in FILTERS:
        raise ValueError(f"unknown filter {filter!r}; use one of {sorted(FILTERS)}")
    # A case nobody has done anything with (a browser that only opened the site) is not a case yet.
    where = "WHERE (last_event_at IS NOT NULL OR has_documents)"
    where += f" AND {FILTERS[filter] if filter else OPEN}"
    rows = conn.execute(
        f"SELECT * FROM console_cases {where} ORDER BY COALESCE(last_event_at, created_at) DESC, case_id"
    ).fetchall()
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
            "needs_distributor": bool(row["distributor_owned"] or row["agent_requested"]),
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


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0] or 0


def metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """The headline and the counters, all from event rows."""
    routed = conn.execute(
        "SELECT COUNT(*) AS cases, "
        f"COALESCE(SUM({NEEDS}), 0) AS needed, "
        "COALESCE(SUM(distributor_owned = 0 AND agent_requested = 0), 0) AS away "
        "FROM console_cases WHERE distributor_owned IS NOT NULL OR agent_requested = 1"
    ).fetchone()
    cases, needed = routed["cases"], routed["needed"]

    why = {
        rule: count
        for rule, count in conn.execute(
            "SELECT j.value, COUNT(DISTINCT e.case_id) FROM events e, json_each(e.detail, '$.rules') j "
            "WHERE e.kind = 'claim_stopped' GROUP BY j.value ORDER BY j.value"
        ).fetchall()
    }

    return {
        "headline": {
            "cases": cases,
            "needed_distributor": needed,
            "distributor": config.DISTRIBUTOR_SHORT_NAME,
            "text": f"Of {cases} {'case' if cases == 1 else 'cases'}, {needed} needed {config.DISTRIBUTOR_SHORT_NAME}.",
        },
        "counters": {
            # readiness checks run (each check counts)
            "readiness_checks_run": _count(conn, "SELECT COUNT(*) FROM events WHERE kind = 'readiness_checked'"),
            # claims stopped before filing (cases), and why (cases per blocking rule)
            "claims_stopped": _count(conn, "SELECT COUNT(DISTINCT case_id) FROM events WHERE kind = 'claim_stopped'"),
            "claims_stopped_why": why,
            # known deductions explained (each explanation counts)
            "known_deductions_explained": _count(conn, "SELECT COUNT(*) FROM events WHERE kind = 'deduction_explained'"),
            "coverage_queries_drafted": _count(conn, "SELECT COUNT(*) FROM events WHERE kind = 'coverage_query_drafted'"),
            "escalations_drafted": _count(conn, "SELECT COUNT(*) FROM events WHERE kind = 'escalation_drafted'"),
            # cases whose latest route sends them to someone other than the distributor
            "cases_routed_away": routed["away"],
            # answers given in the chat, and how many she confirmed solved her question ("Did this
            # solve it?"). Only a Yes counts: an answer nobody rated is not counted as solved.
            "answers_given": _count(conn, "SELECT COUNT(*) FROM events WHERE kind = 'answer_given' AND json_extract(detail, '$.status') = 'answered'"),
            "answers_confirmed_solved": _count(
                conn, "SELECT COUNT(DISTINCT json_extract(detail, '$.answer_id')) FROM events "
                      "WHERE kind = 'answer_feedback' AND json_extract(detail, '$.solved') = 1"),
            # cases an agent marked resolved (the latest mark on each case)
            "cases_marked_resolved": _count(conn, "SELECT COUNT(*) FROM console_cases WHERE agent_status = 'resolved'"),
            # cases where she asked for a person after an answer
            "asked_for_a_person": _count(conn, "SELECT COUNT(DISTINCT case_id) FROM events WHERE kind = 'agent_requested'"),
            # letters the delivery workflow confirmed it sent, and response windows that ended with no reply
            "letters_sent": _count(conn, "SELECT COUNT(*) FROM events WHERE kind = 'letter_delivered'"),
            "follow_ups_triggered": _count(
                conn, "SELECT COUNT(*) FROM events WHERE kind = 'clock_due' AND json_extract(detail, '$.action') != 'stop'"),
            # cases where the paper checks caught something an insurer would query, before filing
            "insurer_queries_caught": _count(
                conn,
                "SELECT COUNT(DISTINCT case_id) FROM events "
                "WHERE kind = 'papers_checked' AND json_array_length(detail, '$.fix') > 0",
            ),
        },
    }
