import pytest

from app.clients import sarvam
from app.core import agent
from app.core.agent import Classification, classify, normalise


def test_prompt_names_every_intent_class_and_product():
    for value in agent.INTENTS + agent.GRIEVANCE_CLASSES + agent.PRODUCTS:
        assert value in agent.CLASSIFY_PROMPT


def test_c1_values_are_present():
    assert "pre_decision" in agent.INTENTS
    assert {"health_policy", "merchant_loan", "motor_policy"} == set(agent.PRODUCTS)
    for family in ("lending/", "insurance/", "platform/"):
        assert any(c.startswith(family) for c in agent.GRIEVANCE_CLASSES)
    assert {
        "lending/undisclosed_charge", "lending/kfs_mismatch", "lending/wrong_emi",
        "lending/disbursal_failed", "lending/recovery_conduct", "lending/foreclosure",
        "insurance/claim_denied", "insurance/claim_delayed", "insurance/mis_sold",
        "insurance/policy_mismatch", "platform/payment_failed", "platform/refund",
        "platform/app_issue", "rti/no_response", "other",
    } <= set(agent.GRIEVANCE_CLASSES)


def test_prompt_routes_double_debits_to_platform():
    assert "debited twice" in agent.CLASSIFY_PROMPT
    assert "platform/payment_failed" in agent.CLASSIFY_PROMPT


def test_normalise_keeps_valid_values():
    result = normalise(
        {"intent": "grievance", "grievance_class": "insurance/claim_delayed", "product": "health_policy"}
    )
    assert result == Classification("grievance", "insurance/claim_delayed", "health_policy")


def test_pre_decision_never_carries_a_grievance_class():
    # C1 acceptance: a loan offer is pre_decision + merchant_loan, never banking/service_deficiency.
    result = normalise(
        {"intent": "pre_decision", "grievance_class": "banking/service_deficiency", "product": "merchant_loan"}
    )
    assert result == Classification("pre_decision", None, "merchant_loan")


def test_motor_policy_is_accepted():
    assert normalise({"intent": "question", "product": "motor_policy"}).product == "motor_policy"


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"intent": "complaint"}, Classification(None, None, None)),
        ({"intent": "grievance", "grievance_class": "insurance/made_up"}, Classification("grievance", "other", None)),
        ({"intent": "question", "grievance_class": "insurance/made_up"}, Classification("question", None, None)),
        ({"intent": "grievance", "grievance_class": None}, Classification("grievance", None, None)),
        ({"intent": "question", "product": "home_loan"}, Classification("question", None, None)),
        ({"intent": "question", "product": "null"}, Classification("question", None, None)),
        ({"intent": " Grievance ", "grievance_class": "Platform/Refund"}, Classification("grievance", "platform/refund", None)),
    ],
)
def test_normalise_never_guesses(payload, expected):
    assert normalise(payload) == expected


def test_classify_sends_the_prompt_and_the_text(monkeypatch):
    seen = {}

    def fake_chat_json(messages, **kwargs):
        seen["messages"] = messages
        return {"intent": "grievance", "grievance_class": "platform/payment_failed", "product": "health_policy"}

    monkeypatch.setattr(sarvam, "chat_json", fake_chat_json)
    result = classify("Premium was debited twice")

    assert result == Classification("grievance", "platform/payment_failed", "health_policy")
    assert seen["messages"][0] == {"role": "system", "content": agent.CLASSIFY_PROMPT}
    assert "Premium was debited twice" in seen["messages"][-1]["content"]


@pytest.mark.parametrize("reply", [None, [], "not json"])
def test_classify_unreadable_reply_is_unknown(monkeypatch, reply):
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: reply)
    assert classify("anything") == Classification()


def test_classify_model_failure_is_unknown(monkeypatch):
    def fail(*args, **kwargs):
        raise sarvam.SarvamUnavailable("down")

    monkeypatch.setattr(sarvam, "chat_json", fail)
    assert classify("anything") == Classification()


def test_classify_empty_text_makes_no_call(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no call expected")

    monkeypatch.setattr(sarvam, "chat_json", boom)
    assert classify("   ") == Classification()
