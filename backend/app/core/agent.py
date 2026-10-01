"""Classifier: what is she asking about, and is anything wrong yet?

This is where the model is called. Its answer is normalised against fixed
lists before anything downstream sees it, so an invented class or product
becomes "unknown", never a guess. The rule engine never calls a model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.clients import sarvam

logger = logging.getLogger(__name__)

INTENTS = ("grievance", "question", "pre_decision", "smalltalk")

# Only the banking class PRD-PAYTM names is listed; the rest of banking/* comes
# with the RBI ladder.
GRIEVANCE_CLASSES = (
    "banking/service_deficiency",
    "lending/undisclosed_charge",
    "lending/kfs_mismatch",
    "lending/wrong_emi",
    "lending/disbursal_failed",
    "lending/recovery_conduct",
    "lending/foreclosure",
    "insurance/claim_denied",
    "insurance/claim_delayed",
    "insurance/mis_sold",
    "insurance/policy_mismatch",
    "platform/payment_failed",
    "platform/refund",
    "platform/app_issue",
    "rti/no_response",
    "other",
)

# motor_policy is reserved: the classifier may emit it before a motor ruleset exists.
# Every kind of insurance a customer may hold, plus the loan the PRD names.
INSURANCE_PRODUCTS = ("health_policy", "motor_policy", "life_policy", "travel_policy", "home_policy", "other_insurance")
PRODUCTS = INSURANCE_PRODUCTS + ("merchant_loan",)

_CLASS_NOTES = {
    "banking/service_deficiency": "a bank failed to provide a service it owed",
    "lending/undisclosed_charge": "a loan charge that was never disclosed",
    "lending/kfs_mismatch": "loan terms differ from the key fact statement",
    "lending/wrong_emi": "the instalment amount is wrong",
    "lending/disbursal_failed": "the loan was sanctioned but the money did not arrive",
    "lending/recovery_conduct": "harassment or misconduct by recovery agents",
    "lending/foreclosure": "a problem closing a loan early",
    "insurance/claim_denied": "the insurer rejected a claim",
    "insurance/claim_delayed": "the insurer has not decided a claim in time",
    "insurance/mis_sold": "a policy was sold with false promises, or added without consent",
    "insurance/policy_mismatch": "the policy document differs from what was promised",
    "platform/payment_failed": "a payment on the app failed or was debited twice",
    "platform/refund": "a refund from the app has not arrived",
    "platform/app_issue": "the app itself is not working",
    "rti/no_response": "a Right to Information request got no reply",
    "other": "a grievance that fits none of the above",
}

CLASSIFY_PROMPT = f"""You classify one message from a person in India about insurance, a loan or a payment app.
The message may be in any Indian language, or mixed with English. Classify its meaning.

Return JSON only:
{{"intent": "<intent>", "grievance_class": "<class or null>", "product": "<product or null>"}}

intent, one of:
- grievance: something has already gone wrong and she wants it fixed.
- question: she wants to understand something; nothing has gone wrong.
- pre_decision: she is about to act and nothing has gone wrong yet - about to accept a loan offer,
  sign a loan agreement, buy a policy, or be admitted to hospital and wondering if the claim will be paid.
- smalltalk: a greeting, thanks, or chat that asks nothing about insurance, a loan or a payment.

grievance_class, one of (null unless something has gone wrong):
{chr(10).join(f"- {name}: {_CLASS_NOTES[name]}" for name in GRIEVANCE_CLASSES)}

product, one of, or null if it is not clear:
- health_policy: health or medical insurance, including hospitalisation and top-up cover
- motor_policy: vehicle insurance: bike, scooter, car or commercial vehicle
- life_policy: life insurance: term, endowment, money-back, ULIP or pension plan
- travel_policy: travel insurance
- home_policy: home, property, shop or contents insurance
- other_insurance: any other insurance (personal accident, gadget, crop, pet, cyber and so on)
- merchant_loan: a loan to a shop or small business

Rules:
- Write every value exactly as listed above, in English, whatever language the message is in.
- A premium or instalment debited twice, a failed mandate or autopay, or a missing refund is
  platform/payment_failed or platform/refund, even when the money was for a policy or a loan.
- Something that happened to her (an accident, a theft, an illness, a death in the family) where she asks
  what to do or whether she is covered is a question, not a grievance. A grievance is when a company (the
  insurer, the lender, the app) did something wrong or has not done what it should.
- pre_decision and smalltalk always have grievance_class null. A loan offer she is weighing is not a banking complaint.
- If you are unsure, use null. Never guess."""

_REPLY_IN_ENGLISH = "\n\n---\nReply with the JSON object only. Keys and values in English, exactly as listed."


@dataclass(frozen=True)
class Classification:
    intent: str | None = None
    grievance_class: str | None = None
    product: str | None = None


def _pick(value, allowed: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned if cleaned in allowed else None


def normalise(payload: dict) -> Classification:
    """Hold a model reply to the fixed lists. Anything off-list becomes unknown."""
    intent = _pick(payload.get("intent"), INTENTS)
    product = _pick(payload.get("product"), PRODUCTS)

    raw_class = payload.get("grievance_class")
    grievance_class = _pick(raw_class, GRIEVANCE_CLASSES)
    if grievance_class is None and intent == "grievance" and isinstance(raw_class, str) and raw_class.strip():
        # A grievance the model named but we do not recognise is still a grievance.
        grievance_class = "other"
    if intent in ("pre_decision", "smalltalk"):
        # Nothing has gone wrong yet, so there is no grievance to classify.
        grievance_class = None

    return Classification(intent, grievance_class, product)


def classify(text: str) -> Classification:
    """Classify one message. Returns all-unknown on an empty message or any model failure."""
    if not text or not text.strip():
        return Classification()
    try:
        payload = sarvam.chat_json(
            [
                {"role": "system", "content": CLASSIFY_PROMPT},
                # The English line after her message stops the model answering in her language.
                {"role": "user", "content": f"Message:\n{text[:4000]}{_REPLY_IN_ENGLISH}"},
            ],
            temperature=0.0,
            max_tokens=600,  # reasoning shares this budget; 200 sometimes came back empty
            default=None,
        )
    except Exception as exc:
        logger.warning("Classification failed: %s", exc)
        return Classification()
    if not isinstance(payload, dict):
        return Classification()
    return normalise(payload)
