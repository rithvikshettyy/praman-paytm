"""Seed the demo's example cases: exactly the three in PRD-PAYTM Part 9.

    python scripts/seed_demo.py        (from backend/; safe to re-run: it replaces them)

  demo-admission     her father is being admitted; the policy and bill fixtures put the room
                     above the cap -> file with a known deduction
  demo-moratorium    a claim denied for non-disclosure on a policy held six years -> the
                     moratorium ground, and a draft to the insurer by name, waiting for approval
  demo-double-debit  a premium debited twice -> the distributor owes the answer

Every case is labelled an example and shows as one on every screen. The numbers are the
labelled fixtures and example answers below; nothing is a statistic.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cases, readiness, store  # noqa: E402
from app.core.ladder_engine import Facts  # noqa: E402
from app.services import documents, drafts  # noqa: E402
from app.services.documents import Extraction  # noqa: E402

ADMISSION, MORATORIUM, DOUBLE_DEBIT = "demo-admission", "demo-moratorium", "demo-double-debit"
EXAMPLES = (ADMISSION, MORATORIUM, DOUBLE_DEBIT)

# What she would answer on the readiness page, where the documents are silent.
EXAMPLE_ANSWERS = {"wait_months": 24, "exclusion_listed": False, "policy_in_force": True}


def _open(conn, case_id: str, language: str) -> None:
    store.ensure_case(conn, case_id)
    store.set_example(conn, case_id)
    store.set_language(conn, case_id, language)


def _complete_checklist(conn, case_id: str) -> None:
    """Every checklist slot filled with an example entry (no file behind it)."""
    for slot in cases.load_checklist().slot_ids:
        store.save_document(conn, case_id, Extraction(cases.CLAIM_DOC, False, "example"), slot=slot)


def _read(conn, case_id: str, doc_type: str) -> dict:
    """Store a fixture document on the case and return the facts that clear the confidence gate."""
    extraction = documents.fixture_extraction(doc_type)
    store.save_document(conn, case_id, extraction)
    return documents.review(doc_type, extraction.fields).facts


def seed(conn) -> tuple[str, ...]:
    for case_id in EXAMPLES:
        store.delete_case(conn, case_id)

    # 1. Health admission: policy + bill fixtures, room above the cap.
    _open(conn, ADMISSION, "mr-IN")
    facts = {**_read(conn, ADMISSION, "policy"), **_read(conn, ADMISSION, "bill")}
    readiness.check_papers(conn, ADMISSION, *(  # the example bill misspells the patient's surname
        documents.trusted_values(documents.fixture_extraction(doc_type).fields) for doc_type in ("policy", "bill")
    ))
    _complete_checklist(conn, ADMISSION)
    facts.update(cases.checklist_facts(cases.checklist_state(conn, ADMISSION)), **EXAMPLE_ANSWERS)
    cases.route_case(conn, ADMISSION, "health_policy", None)  # a coverage question: the insurer's
    cases.check_readiness(conn, ADMISSION, Facts(**facts))

    # 2. Claim denied for non-disclosure on a policy held six years.
    _open(conn, MORATORIUM, "mr-IN")
    _read(conn, MORATORIUM, "policy")  # the insurer's legal name comes from here
    _complete_checklist(conn, MORATORIUM)
    cases.route_case(conn, MORATORIUM, "health_policy", "insurance/claim_denied")
    cases.check_readiness(conn, MORATORIUM, Facts(
        denial_reason="non_disclosure", months_continuous_cover=72, months_held=72,
        room_cap_per_day=5000, room_quoted_per_day=5000, ped_wait_months=36,
        **cases.checklist_facts(cases.checklist_state(conn, MORATORIUM)), **EXAMPLE_ANSWERS,
    ))
    drafts.compose(conn, MORATORIUM, "en-IN")  # left as drafted: she approves it live

    # 3. Premium debited twice.
    _open(conn, DOUBLE_DEBIT, "mr-IN")
    cases.route_case(conn, DOUBLE_DEBIT, "health_policy", "platform/payment_failed")
    return EXAMPLES


def main() -> int:
    conn = store.connect()
    try:
        for case_id in seed(conn):
            print(f"seeded example case {case_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
