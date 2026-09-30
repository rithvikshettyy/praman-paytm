"""Distributor Console reads (PRD-PAYTM N8).

Three blocks: the case list, the headline ("Of N cases, X needed <the
distributor>") and six counters. Every number is counted from event rows,
through the ``console_cases`` view or the events table directly. Nothing is
projected, priced or estimated: "what would this save?" is answered by
multiplying X by the distributor's own cost per ticket, which only they know.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app import config

FILTERS = {
    "needs_paytm": "distributor_owned = 1",
    "routed_away": "distributor_owned = 0",
}


def _owned(value: Any) -> bool | None:
    return None if value is None else bool(value)


def case_list(conn: sqlite3.Connection, filter: str | None = None) -> list[dict[str, Any]]:
    """One row per case, newest activity first. ``filter`` is needs_paytm | routed_away."""
    if filter is not None and filter not in FILTERS:
        raise ValueError(f"unknown filter {filter!r}; use one of {sorted(FILTERS)}")
    # A case nobody has done anything with (a browser that only opened the site) is not a case yet.
    where = "WHERE (last_event_at IS NOT NULL OR has_documents)"
    if filter:
        where += f" AND {FILTERS[filter]}"
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
    """The headline and the six counters, all from event rows."""
    routed = conn.execute(
        "SELECT COUNT(*) AS cases, "
        "COALESCE(SUM(distributor_owned = 1), 0) AS needed, "
        "COALESCE(SUM(distributor_owned = 0), 0) AS away "
        "FROM console_cases WHERE distributor_owned IS NOT NULL"
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
        },
    }
