"""Respondent router (PRD-PAYTM N5): who owes her an answer.

One pure function over the N5 table. The customer paid the distributor, so
she asks the distributor, even when the insurer or the lender owns the
answer. Routing each case straight to the party that owes the answer, with
that party's own first step and clock, is what keeps the distributor out of
the relay. ``distributor_owned`` marks the cases that genuinely are the
distributor's: platform problems and mis-selling.

Pure like the engine (tested): no I/O, no model, no clock. Legal names come
in as ``names``; response windows come in as ``windows`` (loaded from
data/ladders/escalation_steps.yaml, each with its own verified_by).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from app.core import ladder_engine as le

INSURER = "insurer"
DISTRIBUTOR = "distributor"
LENDER = "lender"

_LENDING = frozenset(
    {
        "lending/undisclosed_charge",
        "lending/kfs_mismatch",
        "lending/wrong_emi",
        "lending/disbursal_failed",
        "lending/recovery_conduct",
        "lending/foreclosure",
    }
)
_CLAIMS = frozenset({"insurance/claim_denied", "insurance/claim_delayed"})
# Every kind of insurance (agent.INSURANCE_PRODUCTS); the router stays free of the classifier.
_INSURANCE = frozenset({"health_policy", "motor_policy", "life_policy", "travel_policy", "home_policy", "other_insurance"})


@dataclass(frozen=True)
class Row:
    """One row of the N5 table."""

    situation: str
    classes: frozenset  # grievance classes; None is a coverage question with no grievance
    products: frozenset | None  # None: any product, including unknown
    respondent: str
    distributor_owned: bool
    ladder: str
    steps: tuple[str, ...]  # first step, then, then


TABLE: tuple[Row, ...] = (
    Row("Claim denied or delayed", _CLAIMS, _INSURANCE - {"motor_policy"},
        INSURER, False, "insurance_claim",
        ("insurer_grievance_cell", "irdai_grievance", "insurance_ombudsman")),
    Row("Coverage question on her own policy", frozenset({None}), _INSURANCE,
        INSURER, False, "coverage_question",
        ("answered_in_chat", "coverage_query")),
    Row("Mis-sold or bundled policy", frozenset({"insurance/mis_sold"}), None,
        DISTRIBUTOR, True, "mis_selling",
        ("distributor_grievance", "insurer_grievance_cell", "irdai_grievance")),
    Row("Premium debited twice, refund, mandate failure", frozenset({"platform/payment_failed", "platform/refund"}), None,
        DISTRIBUTOR, True, "platform_support",
        ("distributor_support_ticket",)),
    Row("App or platform issue", frozenset({"platform/app_issue"}), None,
        DISTRIBUTOR, True, "platform_support",
        ("distributor_support_ticket",)),
    Row("Loan servicing, charges, conduct", _LENDING, None,
        LENDER, False, "lending_servicing",
        ("lender_grievance", "lender_nodal_officer", "rbi_ombudsman")),
    Row("Motor claim", _CLAIMS, frozenset({"motor_policy"}),
        INSURER, False, "motor_claim",
        ("insurer_claims_desk", "insurer_grievance_cell", "irdai_grievance")),
)


@dataclass(frozen=True)
class Route:
    situation: str
    respondent: str  # insurer | distributor | lender
    respondent_name: str | None  # the legal entity a draft is addressed to; None until known
    distributor_owned: bool
    ladder: str
    steps: tuple[str, ...]

    @property
    def first_step(self) -> str:
        return self.steps[0]


def matching_rows(product: str | None, grievance_class: str | None) -> list[Row]:
    return [
        row
        for row in TABLE
        if grievance_class in row.classes and (row.products is None or product in row.products)
    ]


def route(
    product: str | None,
    grievance_class: str | None,
    *,
    names: Mapping[str, str] | None = None,
) -> Route | None:
    """(product, grievance_class) -> who owes the answer, their name, and their ladder.

    ``names`` maps a respondent kind to its legal name. Returns None for a
    situation the N5 table does not cover, or one it cannot place without
    knowing the product: those are asked, not guessed.
    """
    rows = matching_rows(product, grievance_class)
    if len(rows) != 1:
        return None
    row = rows[0]
    return Route(
        situation=row.situation,
        respondent=row.respondent,
        respondent_name=(names or {}).get(row.respondent) or None,
        distributor_owned=row.distributor_owned,
        ladder=row.ladder,
        steps=row.steps,
    )


# --- Clocks ------------------------------------------------------------------


@dataclass(frozen=True)
class StepWindow:
    """How long a step on a ladder has to answer. ``days`` is None when no window is known."""

    step: str
    label: str
    days: int | None
    verified_by: str
    source: str | None = None


@dataclass(frozen=True)
class Clock:
    ladder: str
    step: str
    started_on: date
    respond_by: date | None  # None: no known window, so no deadline is claimed
    verified_by: str


def start_clock(
    route: Route, started_on: date, windows: Mapping[str, StepWindow], step: str | None = None
) -> Clock:
    """Start the clock for a step on this respondent's own ladder (the first step by default)."""
    step = step or route.first_step
    if step not in route.steps:
        raise ValueError(f"{step!r} is not on the {route.ladder} ladder")
    window = windows[step]
    respond_by = le.add_days(started_on, window.days) if window.days is not None else None
    return Clock(route.ladder, step, started_on, respond_by, window.verified_by)
