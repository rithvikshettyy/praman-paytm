"""Readiness for the web: the verdict plus what she still needs to tell us.

Presentation only. The verdict comes from the engine, the money from its
calculations, the fields from the confidence gate. This module puts them in
the shape the site shows, and turns missing facts into plain questions so
nothing is ever assumed.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from functools import lru_cache
from typing import Any

import yaml

from app import cases, config, store
from app.store import Store
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


# --- Paper checks: catch the insurer's query before she files ----------------


@lru_cache(maxsize=1)
def paper_words() -> dict[str, Any]:
    """Labels and messages for the paper checks, and the non-payable list (data/claim_checks.yaml)."""
    return yaml.safe_load((config.DATA_DIR / "claim_checks.yaml").read_text(encoding="utf-8"))


def papers_from(policy: dict[str, Any] | None, bill: dict[str, Any] | None) -> le.Papers:
    """The check inputs from each document's trusted values (documents.trusted_values)."""
    policy, bill = policy or {}, bill or {}
    lines = bill.get("line_items")
    return le.Papers(
        insured_names=tuple(policy["insured_names"]) if policy.get("insured_names") else None,
        policy_start_on=policy.get("policy_start_date"),
        policy_end_on=policy.get("period_end_date"),
        patient_name=bill.get("patient_name"),
        admission_on=bill.get("admission_date"),
        discharge_on=bill.get("discharge_date"),
        bill_total=bill.get("bill_total"),
        bill_lines=tuple((line["description"], line.get("amount")) for line in lines) if lines else None,
    )


def _shown(name: str, value: Any) -> str:
    if isinstance(value, date):
        return f"{value.day} {value.strftime('%b %Y')}"
    if name == "insured":
        return ", ".join(value)
    if name == "items":
        return ", ".join(f"{text} ({ladders.inr(amount)})" if amount is not None else text for text, amount in value)
    if isinstance(value, (int, float)):
        return ladders.inr(value)
    return str(value)


def papers_view(result: le.PapersCheck) -> dict[str, Any]:
    """Findings in her words, plus which checks ran clean and which could not run (and why)."""
    words = paper_words()
    checks, problems = words["checks"], words["problems"]
    list_unverified = words["non_payable"]["verified_by"] == cases.UNVERIFIED
    findings = []
    for finding in result.findings:
        problem = problems[finding.problem]
        message = " ".join(problem["message"].split())
        findings.append({
            "check": finding.check,
            "problem": finding.problem,
            "severity": problem["severity"],
            "message": message.format(**{k: _shown(k, v) for k, v in finding.values.items()}),
            "unverified": finding.check == "non_payable_items" and list_unverified,
        })
    return {
        "findings": findings,
        "passed": [{"check": c, "label": checks[c]["label"]} for c in result.passed],
        "skipped": [{"check": c, "label": checks[c]["label"], "needs": checks[c]["needs"]} for c in result.skipped],
        "to_fix": sum(f["severity"] == "fix" for f in findings),
    }


def check_papers(
    conn: Store | None, case_id: str | None, policy: dict[str, Any] | None, bill: dict[str, Any] | None
) -> dict[str, Any]:
    """Run the paper checks on her trusted values; with a case, log what was caught for the console."""
    result = le.check_papers(papers_from(policy, bill), tuple(paper_words()["non_payable"]["items"]))
    view = papers_view(result)
    if conn is not None and case_id and (result.findings or result.passed):
        store.record_event(conn, case_id, "papers_checked", {
            "fix": [f["problem"] for f in view["findings"] if f["severity"] == "fix"],
            "heads_up": [f["problem"] for f in view["findings"] if f["severity"] != "fix"],
        })
    return view


# --- One verdict from her policy and bill -------------------------------------


def assess(conn: Store, case_id: str, fields: dict[str, dict[str, documents.Field]]) -> dict[str, Any]:
    """Verdict, open questions and paper checks from each document's fields ({"policy": ..., "bill": ...}).
    Only values that clear the confidence gate reach the engine or the checks."""
    facts, trusted = {}, {}
    for doc_type, read in fields.items():
        facts.update(documents.review(doc_type, read).facts)
        trusted[doc_type] = documents.trusted_values(read)
    state = cases.checklist_state(conn, case_id)
    facts["documents_required"] = state.required
    if state.collected:
        facts["documents_collected"] = state.collected
    fact_sheet = le.Facts(**facts)
    verdict = cases.check_readiness(conn, case_id, fact_sheet)
    papers = check_papers(conn, case_id, trusted.get("policy"), trusted.get("bill"))
    return {"papers": papers, **view(verdict, fact_sheet, ladders.load("insurance_health_claim"))}


def _merged(conn: Store, case_id: str, doc_type: str) -> dict[str, documents.Field] | None:
    """Every document of this type on the case as one: per field the newest value read; a bill
    sent page by page has its lines joined (a page sent twice counts once)."""
    merged: dict[str, documents.Field] = {}
    lines: list[dict] = []
    line_confidence = 1.0
    for row in store.case_documents(conn, case_id, (doc_type,)):  # newest first
        for name, read in documents.stored_fields(doc_type, row["fields"]).items():
            if name == "line_items":
                for line in read.value or ():
                    if line not in lines:
                        lines.append(line)
                        line_confidence = min(line_confidence, read.confidence)
            elif read.value is not None and name not in merged:
                merged[name] = read
    if lines:
        merged["line_items"] = documents.Field(lines, line_confidence)
    return merged or None


def from_case(conn: Store, case_id: str) -> dict[str, Any] | None:
    """The assessment of the policy and bill she sent in the chat; None until both are read."""
    policy, bill = _merged(conn, case_id, "policy"), _merged(conn, case_id, "bill")
    if policy is None or bill is None:
        return None
    return assess(conn, case_id, {"policy": policy, "bill": bill})
