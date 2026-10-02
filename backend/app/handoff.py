"""The agent's brief: one screen that lets a support agent pick up a case without asking her again.

Built from what is already recorded for the case: its route, readiness check, documents received,
what she wrote and asked, what Praman answered, and her language. Always in English, so an agent
who does not read Marathi can still act; her own words are shown beside their English.

Nothing here is invented: a section with nothing recorded says so. Nothing identifying her goes
in: no phone number, name, session id, file names or document text. Her words and the answers
were masked when they were recorded (app/services/redact.py) and go when she deletes the case.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from app import cases, config, store
from app.core import ladders
from app.services import i18n

PRODUCT_LABEL = {
    "health_policy": "Health insurance",
    "motor_policy": "Motor insurance (bike, car)",
    "life_policy": "Life insurance",
    "travel_policy": "Travel insurance",
    "home_policy": "Home insurance",
    "other_insurance": "Other insurance",
    "merchant_loan": "Merchant loan",
}
OUTCOME_LABEL = {
    "file": "Ready to file",
    "do_not_file_yet": "Do not file yet",
    "file_with_known_deduction": "File, with a known deduction",
    "facts_pending": "Waiting for her answers",
    "no_verdict": "No readiness check",
}
LANGUAGE_NAME = config.SUPPORTED_LANGUAGES

# fact -> (label, kind). Only facts an agent can use; no identifiers.
FACTS: dict[str, tuple[str, str]] = {
    "sum_insured": ("Sum insured", "money"),
    "room_cap_per_day": ("Room rent limit per day", "money"),
    "room_quoted_per_day": ("Room charged per day", "money"),
    "bill_deductible_heads": ("Room, nursing, doctor and surgery charges", "money"),
    "bill_exempt_heads": ("Medicines, tests and implants", "money"),
    "policy_start_on": ("Policy started", "date"),
    "treatment_on": ("Admission date", "date"),
    "months_held": ("Months the policy was held", "plain"),
    "months_continuous_cover": ("Months of continuous cover", "plain"),
    "wait_months": ("Waiting period for this treatment (months)", "plain"),
    "ped_wait_months": ("Pre-existing disease waiting period (months)", "plain"),
    "denial_reason": ("Reason the insurer gave", "choice"),
    "sanctioned_amount": ("Loan amount", "money"),
    "processing_fee": ("Processing fee", "money"),
    "instalment_amount": ("Instalment", "money"),
    "instalment_count": ("Number of instalments", "plain"),
    "total_repayable": ("Total to repay", "money"),
}
MAX_TURNS = 8  # the latest questions she asked


def _day(iso: str) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    return f"{d.day} {d.strftime('%b %Y')}"


def _fact_value(kind: str, value: Any) -> str:
    if kind == "money":
        return ladders.inr(value)
    if kind == "date":
        return _day(value)
    if kind == "choice":
        return str(value).replace("_", " ")
    return str(value)


def _english(text: str, language: str) -> str | None:
    """Her words in English, when they are not already: None if they are, or it could not be done."""
    if not text or language == config.DEFAULT_LANGUAGE or text.isascii():
        return None
    out = i18n.translate(text, config.DEFAULT_LANGUAGE, source_language=language, colloquial=True)
    return None if out == text else out


def _humanise(value: str | None) -> str | None:
    return value.split("/")[-1].replace("_", " ").capitalize() if value else None


def brief(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    """The agent's brief for a case, or None if there is no such case."""
    detail = cases.case_detail(conn, case_id)
    if detail is None:
        return None
    language = detail["language"]
    route = detail["route"] or {}
    steps = route.get("steps") or []

    # Why this case is in front of an agent.
    asked = store.case_events(conn, case_id, "agent_requested", limit=1)
    if asked:
        how = asked[0]["detail"].get("reason")
        why = {
            "reason": "asked_for_person",
            "text": "She asked for a person: Praman's answer did not solve her question." if how == "not_solved"
            else "She asked for a person: Praman could not find the answer in her documents or the sources.",
        }
    elif detail["distributor_owned"]:
        why = {"reason": "distributor_owned", "text": f"This one is the distributor's to answer ({route.get('situation', 'platform issue')})."}
    elif detail["respondent"]:
        owner = detail["respondent_name"] or f"the {detail['respondent']}"
        why = {"reason": "routed_away", "text": f"Routed to {owner}. Nothing is needed from the distributor unless she asks for a person."}
    else:
        why = {"reason": "not_routed", "text": "No problem has been routed yet."}

    # What she wrote when something went wrong, and what she asked.
    wrote = [
        {"text": e["detail"].get("text", ""), "text_en": _english(e["detail"].get("text", ""), language)}
        for e in store.case_events(conn, case_id, "grievance_reported", limit=3)
    ]
    solved = {
        e["detail"].get("answer_id"): bool(e["detail"].get("solved"))
        for e in store.case_events(conn, case_id, "answer_feedback")
    }
    conversation = []
    answers = store.case_events(conn, case_id, "answer_given", limit=MAX_TURNS)
    asked_about = next((e["detail"]["product"] for e in reversed(answers) if e["detail"].get("product")), None)
    for e in answers:
        d = e["detail"]
        conversation.append({
            "at": e["at"],
            "question": d.get("question", ""),
            "question_en": _english(d.get("question", ""), language),
            "answered": d.get("status") == "answered",
            "source": "her own document" if d.get("source") == "her_document" else "policy wordings and rules",
            "pages": d.get("pages") or [],
            "answer_en": d.get("answer_en") or "",
            "solved": solved.get(e["id"]),
        })

    # Facts from the latest readiness check, and what the paper checks caught.
    checked = (store.latest_event(conn, case_id, "readiness_checked") or {}).get("detail") or {}
    facts = [
        {"label": FACTS[name][0], "value": _fact_value(FACTS[name][1], value)}
        for name, value in (checked.get("facts") or {}).items()
        if name in FACTS and value is not None
    ]
    deduction = (store.latest_event(conn, case_id, "deduction_explained") or {}).get("detail") or {}
    papers = (store.latest_event(conn, case_id, "papers_checked") or {}).get("detail") or {}

    # Documents received: kinds and checklist slots only, never names or text.
    checklist = cases.load_checklist()
    state = cases.checklist_state(conn, case_id, checklist)
    kinds = sorted({row["doc_type"] for row in conn.execute("SELECT doc_type FROM documents WHERE case_id = ?", (case_id,))} - {cases.CLAIM_DOC})
    documents = {
        # a policy sent in the chat is read for her answers and not kept, so only this note remains of it
        "shared_in_chat": any(e["detail"].get("source") == "her_document" for e in answers),
        "claim_documents": [checklist.label(slot) for slot in state.filled],
        "claim_documents_missing": [checklist.label(slot) for slot in state.missing] if state.filled else [],
        "read": kinds,
    }

    drafts = [
        {"kind": d["kind"].replace("_", " "), "addressee": d["addressee"],
         "status": "approved and ready to send" if d["status"] == "approved" else "drafted, not approved"}
        for d in detail["drafts"]
    ]
    first = steps[0] if steps else None
    if why["reason"] == "routed_away":
        next_step = "None needed now. If she writes again, the case is already routed to the party that owes the answer."
    else:
        next_step = (
            f"Reply in {LANGUAGE_NAME.get(language, language)}. "
            + (f"First step on the ladder: {first['label']}"
               + (f", they have {first['respond_within_days']} days to answer." if first.get("respond_within_days") else ".")
               if first else "")
        ).strip()

    result: dict[str, Any] = {
        "case_id": case_id,
        "example": detail["example"],
        "language": {"code": language, "name": LANGUAGE_NAME.get(language, language)},
        "product": PRODUCT_LABEL.get(detail["product"] or asked_about or ""),
        "problem": _humanise(detail["grievance_class"]) or ("Question about her policy" if answers and not detail["route"] else None),
        "situation": route.get("situation"),
        "respondent": detail["respondent_name"] or detail["respondent"],
        "why_here": why,
        "she_wrote": wrote,
        "conversation": conversation,
        "readiness": {
            "outcome": OUTCOME_LABEL.get(detail["verdict"] or "", None),
            "deduction": ladders.inr(deduction["deduction"]) if deduction.get("deduction") is not None else None,
            "paper_fixes": papers.get("fix") or [],
        },
        "facts": facts,
        "documents": documents,
        "drafts": drafts,
        "next_step": next_step,
    }
    result["text"] = as_text(result)
    return result


def as_text(b: dict[str, Any]) -> str:
    """The brief as plain text an agent can paste into a ticket."""
    lines = [f"CASE BRIEF{' (example case)' if b['example'] else ''}"]
    lines.append(f"Language: {b['language']['name']}")
    lines.append(f"Why you are seeing this: {b['why_here']['text']}")
    problem = " / ".join(x for x in (b["product"], b["problem"] or b["situation"]) if x)
    lines.append(f"Problem: {problem or 'not stated yet'}")
    if b["respondent"]:
        lines.append(f"Owed by: {b['respondent']}")
    for item in b["she_wrote"]:
        lines.append(f'She wrote: "{item["text"]}"' + (f' (in English: "{item["text_en"]}")' if item["text_en"] else ""))
    if b["conversation"]:
        lines.append("What she asked and what Praman answered:")
        for turn in b["conversation"]:
            q = turn["question"] + (f' (in English: "{turn["question_en"]}")' if turn["question_en"] else "")
            verdict = {True: "she said it solved it", False: "she said it did not solve it", None: "no reply from her"}[turn["solved"]]
            if turn["answered"]:
                pages = f" (from {turn['source']}, page{'s' if len(turn['pages']) > 1 else ''} {', '.join(map(str, turn['pages']))})"
                lines.append(f'- Q: {q}\n  A: {turn["answer_en"]}{pages}; {verdict}')
            else:
                lines.append(f"- Q: {q}\n  A: Praman could not find this in {turn['source']}; {verdict}")
    r = b["readiness"]
    if r["outcome"]:
        lines.append(f"Claim readiness: {r['outcome']}" + (f", deduction {r['deduction']}" if r["deduction"] else ""))
    if r["paper_fixes"]:
        lines.append(f"Paper problems an insurer would query: {', '.join(x.replace('_', ' ') for x in r['paper_fixes'])}")
    for fact in b["facts"]:
        lines.append(f"{fact['label']}: {fact['value']}")
    d = b["documents"]
    if d["shared_in_chat"]:
        lines.append("She shared a policy in the chat (read for her answers; the file is not kept).")
    if d["claim_documents"]:
        lines.append("Claim documents received: " + ", ".join(d["claim_documents"]))
    if d["claim_documents_missing"]:
        lines.append("Claim documents missing: " + ", ".join(d["claim_documents_missing"]))
    if d["read"]:
        lines.append("Documents read: " + ", ".join(d["read"]))
    for draft in b["drafts"]:
        lines.append(f"Letter ({draft['kind']}) to {draft['addressee']}: {draft['status']}")
    lines.append(f"Suggested next step: {b['next_step']}")
    return "\n".join(lines)
