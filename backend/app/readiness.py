"""Readiness for the web: the verdict plus what she still needs to tell us.

Presentation only. The verdict comes from the engine, the money from its
calculations, the fields from the confidence gate. This module puts them in
the shape the site shows, and turns missing facts into plain questions so
nothing is ever assumed.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from app import cases
from app.core import ladder_engine as le
from app.core import ladders
from app.services import documents

# fact -> (question, input kind). Input kinds: yes_no | number | date | choice.
QUESTIONS: dict[str, tuple[str, str]] = {
    "months_held": ("How many months have you held this policy?", "number"),
    "wait_months": ("What is the waiting period for this treatment, in months? Enter 0 if there is none.", "number"),
    "exclusion_listed": ("Is this treatment on the policy's list of exclusions?", "yes_no"),
    "policy_in_force": ("Was the policy active (premium paid) on the admission date?", "yes_no"),
    "room_cap_per_day": ("What is the policy's room rent limit per day, in rupees?", "number"),
    "room_quoted_per_day": ("What does the hospital charge per day for the room, in rupees?", "number"),
    "documents_collected": ("How many of the claim documents do you have?", "number"),
    "documents_required": ("How many documents does the insurer ask for?", "number"),
    "ped_wait_months": ("What is the waiting period for pre-existing diseases, in months?", "number"),
    "months_continuous_cover": ("How many months have you been covered without a break?", "number"),
    "denial_reason": ("If a claim was already rejected, what reason did the insurer give?", "choice"),
    "bill_deductible_heads": ("Total of room, nursing, doctor and surgery charges on the bill, in rupees?", "number"),
    "bill_exempt_heads": ("Total of medicines, consumables, implants and tests on the bill, in rupees?", "number"),
    "policy_start_on": ("When did the policy start?", "date"),
    "treatment_on": ("When is (or was) the admission?", "date"),
}
DENIAL_REASONS = ("non_disclosure", "waiting_period", "exclusion", "documents", "other")


def questions(facts: le.Facts, verdict: le.Verdict, ladder: ladders.Ladder) -> list[dict[str, Any]]:
    """Missing facts as questions. Required when a rule that gates filing needs them; optional
    when only a ground (help if a claim was rejected) or an amount needs them."""
    known = le.derive(facts)
    needed: dict[str, bool] = {}
    for rule in ladder.rules:
        if any(getattr(known, name) is not None and getattr(known, name) != value for name, value in rule.when):
            continue  # a known condition rules it out
        for name in rule.facts:
            if getattr(known, name) is None:
                needed[name] = needed.get(name, False) or rule.effect != le.GROUND
    for hit in (*verdict.blocks, *verdict.deductions, *verdict.grounds):
        if hit.values.get("pending"):
            calculation = ladder.rule(hit.rule_id).calculation
            for name in le.CALCULATIONS[calculation].reads:
                if getattr(known, name) is None:
                    needed.setdefault(name, False)
    out = []
    for name, required in needed.items():
        question, kind = QUESTIONS.get(name, (name.replace("_", " ").capitalize() + "?", "number"))
        item = {"fact": name, "question": question, "input": kind, "required": required}
        if kind == "choice":
            item["options"] = list(DENIAL_REASONS)
        out.append(item)
    return out


def breakdown(verdict: le.Verdict, facts: le.Facts) -> dict[str, Any] | None:
    """The room-cap deduction, head by head, exactly as the engine computed it."""
    known = le.derive(facts)
    for hit in verdict.deductions:
        values = hit.values
        if "deduction" not in values:
            continue
        return {
            "room_cap_per_day": values["limit"],
            "room_quoted_per_day": values["value"],
            "ratio": values["ratio"],
            "deductible_heads": known.bill_deductible_heads,
            "exempt_heads": known.bill_exempt_heads,
            "deduction": values["deduction"],
            "payable_estimate": values["payable_estimate"],
            "pending": values["pending"],
        }
    return None


def view(verdict: le.Verdict, facts: le.Facts, ladder: ladders.Ladder) -> dict[str, Any]:
    messages = ladders.explain(verdict, ladder)
    return {
        **dataclasses.asdict(verdict),
        "messages": messages,
        "unverified": any(m["unverified"] for m in messages),
        "questions": questions(facts, verdict, ladder),
        "breakdown": breakdown(verdict, facts),
        "facts": cases.facts_to_json(facts),
    }


def document_summary(doc_type: str, extraction: documents.Extraction, review: documents.Review) -> dict[str, Any]:
    """What was read from one document, with confidences and what still needs asking."""
    summary = {
        "doc_type": doc_type,
        "source": extraction.source,
        "example": extraction.example,
        "fields": {
            name: {"value": f.value.isoformat() if hasattr(f.value, "isoformat") else f.value, "confidence": f.confidence}
            for name, f in extraction.fields.items()
        },
        "to_confirm": list(review.to_confirm),
        "missing": list(review.missing),
    }
    if doc_type == "bill":
        grouped = documents.group_bill_lines(extraction.fields["line_items"].value or [])
        totals = le.bill_head_totals(
            [(head, line["amount"]) for head in ("deductible", "exempt") for line in grouped[head]]
        )
        summary["heads"] = {
            "deductible": grouped["deductible"],
            "exempt": grouped["exempt"],
            "unmapped": grouped["unmapped"],
            "deductible_total": totals["deductible"],
            "exempt_total": totals["exempt"],
        }
    return summary
