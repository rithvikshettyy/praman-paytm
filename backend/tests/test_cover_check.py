""""Will this be covered?" with her policy and bill both sent in the chat: the engine answers, not a model."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import config, conversation
from app.clients import sarvam
from app.main import app
from app.rag import mine
from app.rag.answer import Answer
from app.services import documents, i18n
from app.services.documents import Extraction, Field

client = TestClient(app)
SESSION = "cover-check-0001"
POLICY_PAGES = [(1, "Health insurance policy schedule. Sum insured Rs 5,00,000. Waiting period 24 months. Cashless.")]
NOT_FOUND = Answer("no_source", "", "", (), False, None, "en-IN", False)
BILL_PAGES = [(1, "Example Hospital. Final bill. Bill No 12. Patient name: Example Patient. Grand total 1,84,500.")]


def sure(value):
    return Field(value, 0.95)


POLICY = {"insurer": sure("Example General Insurance Company Limited"), "policy_start_date": sure(date(2015, 9, 18)),
          "period_end_date": sure(date(2016, 9, 17)), "insured_names": sure(["Example Patient"]),
          "sum_insured": sure(500000.0)}
BILL = {"patient_name": sure("Example Patient"), "admission_date": sure(date(2024, 2, 26)),
        "discharge_date": sure(date(2024, 3, 8)), "bill_total": sure(184500.0)}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: None)
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: text)
    monkeypatch.setattr(sarvam, "identify_language", lambda text: {})
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: pytest.fail("the engine answers, not RAG"))
    monkeypatch.setattr(documents, "extract", lambda data, name, doc_type, **k: Extraction(
        doc_type, False, "doc_ai", {"policy": POLICY, "bill": BILL}[doc_type]))
    i18n._CACHE.clear()
    mine._DOCS.clear()
    yield
    mine._DOCS.clear()


def send(pages, monkeypatch, name):
    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: pages)
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: NOT_FOUND)  # no summary: not under test
    case_id = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    client.post("/api/consent", json={"case_id": case_id, "scope": "read_documents", "granted": True})
    client.post("/api/chat/upload", files=[("files", (name, b"%PDF-1.4", "application/pdf"))],
                data={"session_id": SESSION, "language": "en-IN"})
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: pytest.fail("the engine answers, not RAG"))


def chat(text):
    return [m["text"] for m in client.post("/api/chat", json={"session_id": SESSION, "text": text, "language": "en-IN"}).json()["messages"]]


def test_a_bill_after_the_policy_ended_is_all_hers(monkeypatch):
    send(POLICY_PAGES, monkeypatch, "policy.pdf")
    send(BILL_PAGES, monkeypatch, "bill.pdf")
    [text] = chat("will my insurance cover it?")  # her words on WhatsApp
    assert "Bill total: ₹1,84,500" in text
    assert conversation.COVER_NOTHING.format(amount="₹1,84,500") in text
    assert "after the policy period ended on 17 Sep 2016" in text  # admission 26 Feb 2024
    assert "Documents still missing: 4 of 6." in text  # policy and bill fill two of the six slots
    assert text.endswith(conversation.COVER_CAVEAT)


def test_a_covered_bill_is_split_between_insurer_and_her(monkeypatch):
    policy = {**POLICY, "period_end_date": sure(date(2026, 9, 17)), "co_pay_percent": sure(10.0),
              "sum_insured": sure(150000.0)}
    bill = {**BILL, "line_items": sure([{"description": "Surgeon charges", "amount": 180000.0},
                                        {"description": "Gloves and masks", "amount": 4500.0}])}
    monkeypatch.setattr(documents, "extract", lambda data, name, doc_type, **k: Extraction(
        doc_type, False, "doc_ai", {"policy": policy, "bill": bill}[doc_type]))
    send(POLICY_PAGES, monkeypatch, "policy.pdf")
    send(BILL_PAGES, monkeypatch, "bill.pdf")
    [text] = chat("how much will I have to pay?")
    assert ("Bill total: ₹1,84,500\n"
            "- Items insurers usually do not pay: ₹4,500\n"
            "- Your co-payment (10%): ₹18,000\n"
            "- Above your sum insured: ₹12,000\n"
            "Insurer pays (estimate): ₹1,50,000\n"
            "You pay (estimate): ₹34,500") in text
    assert conversation.COVER_IF in text  # the waiting period and exclusions are still unknown
    assert "What still decides it:" in text


def test_a_bill_without_a_policy_asks_for_the_policy(monkeypatch):
    send(BILL_PAGES, monkeypatch, "bill.pdf")
    assert chat("will this bill be covered?") == [conversation.COVER_NEEDS_POLICY]


def test_what_the_policy_covers_in_general_still_reads_the_policy(monkeypatch):
    send(POLICY_PAGES, monkeypatch, "policy.pdf")
    send(BILL_PAGES, monkeypatch, "bill.pdf")
    monkeypatch.setattr(conversation, "_cover", lambda *a: pytest.fail("not a question about this bill"))
    monkeypatch.setattr(conversation, "_ask_hers", lambda *a, **k: (conversation.Message("From your policy."),))
    assert chat("does my policy cover maternity?") == ["From your policy."]
