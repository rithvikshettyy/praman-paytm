"""PRD-PAYTM N6: identifiers are masked before any text leaves the process."""

import pytest

from app.clients import sarvam
from app.services.redact import redact


@pytest.mark.parametrize(
    "text, expected",
    [
        # Aadhaar: 12 digits, first digit 2-9, often grouped 4-4-4
        ("Aadhaar 2345 6789 0123", "Aadhaar [AADHAAR]"),
        ("aadhaar no 234567890123.", "aadhaar no [AADHAAR]."),
        ("UID: 9876-5432-1098", "UID: [AADHAAR]"),
        # PAN: five letters, four digits, one letter
        ("PAN ABCPE1234F", "PAN [PAN]"),
        ("my pan is abcpe1234f", "my pan is [PAN]"),
        # bank account numbers: 9 to 18 digits, grouped or not
        ("A/c 123456789012345", "A/c [ACCOUNT]"),
        ("account 0012 3456 7890", "account [ACCOUNT]"),
        ("call 9876543210", "call [ACCOUNT]"),
        # policy numbers: after a policy label, or a slash/hyphen-structured reference
        ("Policy No: 12345678", "Policy No: [POLICY]"),
        ("policy number OG-24-1001-8401-00001234 was issued", "policy number [POLICY] was issued"),
        ("ref P/211100/01/2023/123456", "ref [POLICY]"),
        ("Policy #EXHP0001", "Policy #[POLICY]"),
    ],
)
def test_identifiers_are_masked(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Sum insured Rs 5,00,000 and room rent 8000 per day",
        "Bill total ₹1,32,000; pharmacy 18,000",
        "Admitted on 05/10/2026, discharged 08-10-2026",
        "Waiting period 24 months, PED 36 months",
        "Claim of 45000 denied for non-disclosure",
        "insurance/claim_denied platform/payment_failed",
        "Year 2026, pin 400001",
        "",
    ],
)
def test_ordinary_numbers_dates_and_text_are_left_alone(text):
    assert redact(text) == text


def test_several_identifiers_in_one_message():
    text = "PAN ABCPE1234F, Aadhaar 2345 6789 0123, policy no. HP/2023/0045617, a/c 50100012345678"
    assert redact(text) == "PAN [PAN], Aadhaar [AADHAAR], policy no. [POLICY], a/c [ACCOUNT]"


def test_redaction_is_idempotent():
    text = "PAN ABCPE1234F and Aadhaar 2345 6789 0123 and Policy No: 12345678"
    assert redact(redact(text)) == redact(text)


def test_an_aadhaar_starting_with_0_or_1_is_not_an_aadhaar():
    # Still a long number, so still masked, but not mislabelled.
    assert redact("1234 5678 9012") == "[ACCOUNT]"


# --- Applied where text leaves: the Sarvam client ----------------------------


def test_chat_messages_are_redacted_before_they_leave(monkeypatch):
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return type("R", (), {"choices": []})()

    monkeypatch.setattr(sarvam, "_chat_raw", fake)
    sarvam.chat([{"role": "user", "content": "My PAN is ABCPE1234F, claim delayed"}])
    assert seen["messages"][0]["content"] == "My PAN is [PAN], claim delayed"


def test_translate_input_is_redacted(monkeypatch):
    seen = {}

    def fake(method, **kwargs):
        seen.update(kwargs)
        return type("R", (), {"translated_text": "ok"})()

    monkeypatch.setattr(sarvam, "_text_call", fake)
    sarvam.translate("Aadhaar 2345 6789 0123", "mr-IN")
    assert seen["input"] == "Aadhaar [AADHAAR]"


def test_language_detection_input_is_redacted(monkeypatch):
    seen = {}

    def fake(method, **kwargs):
        seen.update(kwargs)
        return {"language_code": "mr-IN"}

    monkeypatch.setattr(sarvam, "_text_call", fake)
    sarvam.identify_language("माझा पॅन ABCPE1234F आहे")
    assert "ABCPE1234F" not in seen["input"]


def test_speech_text_is_redacted(monkeypatch):
    seen = {}

    def fake(attr, method, **kwargs):
        seen.update(kwargs)
        return {"audios": []}

    monkeypatch.setattr(sarvam, "_speech_call", fake)
    sarvam.text_to_speech("Policy No: 12345678", language="en-IN")
    assert seen["text"] == "Policy No: [POLICY]"
