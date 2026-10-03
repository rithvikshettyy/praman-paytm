"""Any insurance, not only health: bike, car, life, travel, home. One policy is enough to keep talking."""

import pytest
from fastapi.testclient import TestClient

from app import config, conversation
from app.clients import sarvam
from app.main import app
from app.rag import mine, retrieve
from app.rag.answer import Answer, Citation
from app.services import documents, i18n

BIKE = [(1, "Two Wheeler Package Policy. Insured Declared Value (IDV): Rs 1,45,000. Own damage and third party."),
        (8, "Exclusions: driving without a valid licence, under the influence of alcohol, wear and tear.")]
LIFE = [(1, "Term life insurance. Sum assured Rs 1,00,00,000. Life assured: Example Person. Nominee: Example Spouse."),
        (3, "Grace period 30 days. Death benefit is paid to the nominee. No maturity benefit.")]


# --- Which insurance is this? ----------------------------------------------------------


@pytest.mark.parametrize("text, product", [
    (" ".join(t for _, t in BIKE), "motor_policy"),
    (" ".join(t for _, t in LIFE), "life_policy"),
    ("Health insurance. Room rent 1% of sum insured. Pre-existing diseases after 36 months. Cashless.", "health_policy"),
    ("Travel insurance for your trip: baggage loss, flight delay, passport loss.", "travel_policy"),
    ("Householder home insurance: building and contents.", "home_policy"),
    ("Dear Sir, thank you for your letter.", None),  # nothing to go on: asked, not guessed
])
def test_the_kind_of_insurance_is_read_from_the_document(text, product):
    assert documents.detect_product(text) == product


def test_her_latest_clear_document_gives_the_product():
    mine._DOCS.clear()
    mine.add("c1", "bike.pdf", BIKE, "motor_policy")
    mine.add("c1", "letter.pdf", [(1, "Dear Sir")], None)
    assert mine.product("c1") == "motor_policy" and mine.product("c2") is None
    mine._DOCS.clear()


# --- Retrieval skips near-copies --------------------------------------------------------


def test_a_clause_repeated_on_every_page_does_not_crowd_out_the_answer(monkeypatch):
    monkeypatch.setattr(config, "RAG_EMBEDDINGS", "hashing")
    boiler = "Section {n}. The company will indemnify the insured against loss or damage to the vehicle by fire, theft and flood."
    pages = [(n, boiler.format(n=n)) for n in range(2, 9)] + [(9, "Exclusions: wear and tear and mechanical breakdown are not covered.")]
    mine._DOCS.clear()
    mine.add("c1", "bike.pdf", pages)
    found = mine.collection("c1")
    try:
        passages = retrieve.retrieve("what is not covered by the company for the vehicle", insurer=mine.YOUR_DOCUMENT,
                                     product=mine.PRODUCT, collection=found, k=3)
    finally:
        mine.drop(found)
        mine._DOCS.clear()
    assert 9 in [p.page for p in passages]
    assert len({p.text.split(".", 1)[1] for p in passages}) == len(passages)  # no two near-identical passages


# --- The conversation with any policy ------------------------------------------------------

client = TestClient(app)
SESSION = "any-insurance-0001"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(config, "DISTRIBUTOR_LEGAL_NAME", "Example Broking Pvt Ltd")
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: None)
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: text)
    monkeypatch.setattr(sarvam, "identify_language", lambda text: {})
    i18n._CACHE.clear()
    mine._DOCS.clear()
    yield
    mine._DOCS.clear()


def classify_as(monkeypatch, **payload):
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: payload)


def chat(text):
    return [m["text"] for m in client.post("/api/chat", json={"session_id": SESSION, "text": text, "language": "en-IN"}).json()["messages"]]


def send(pages, monkeypatch, text=""):
    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: pages)
    case_id = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    client.post("/api/consent", json={"case_id": case_id, "scope": "read_documents", "granted": True})
    return client.post("/api/chat/upload", files=[("files", ("policy.pdf", b"%PDF-1.4", "application/pdf"))],
                       data={"session_id": SESSION, "language": "en-IN", "text": text}).json(), case_id


def test_the_greeting_names_every_kind_of_insurance():
    [text] = chat("hi")
    assert all(kind in text for kind in ("health", "bike", "car", "life"))


def test_small_talk_after_a_policy_keeps_her_on_the_policy(monkeypatch):
    send(BIKE, monkeypatch)
    classify_as(monkeypatch, intent="smalltalk", grievance_class=None, product=None)
    assert chat("ok") == [conversation.FOLLOW_UP]


def test_a_question_with_no_policy_asks_for_one(monkeypatch):
    classify_as(monkeypatch, intent="question", grievance_class=None, product="motor_policy")
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: Answer(
        "no_source", "not found", "not found", (), False, {"respondent": "insurer"}, "en-IN", False))
    assert chat("what does my car insurance cover?") == [conversation.ASK_FOR_POLICY]


def test_a_claim_problem_uses_the_kind_of_policy_she_sent(monkeypatch):
    send(BIKE, monkeypatch)
    classify_as(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product=None)
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: pytest.fail("a grievance is routed, not looked up"))
    [text] = chat("my claim was rejected, what can I do?")
    assert text == "This one is for your insurer. First step: Claims desk."  # the motor ladder, from her bike policy


def test_a_life_claim_goes_up_the_insurer_ladder(monkeypatch):
    send(LIFE, monkeypatch)
    classify_as(monkeypatch, intent="grievance", grievance_class="insurance/claim_delayed", product="life_policy")
    assert chat("my life insurance claim is delayed") == ["This one is for your insurer. First step: Grievance cell."]


def test_a_bike_or_life_policy_never_fills_the_health_checklist(monkeypatch):
    for pages in (BIKE, LIFE):
        _, case_id = send(pages, monkeypatch)
        assert len(client.get(f"/api/checklist/{case_id}").json()["missing"]) == 6


def test_her_question_on_a_life_policy_is_answered_from_it(monkeypatch):
    def fake_ask(question, **kwargs):
        cite = Citation(mine.YOUR_DOCUMENT, "policy.pdf", 1, "", mine.VERIFIED_BY)
        text = f"The nominee is Example Spouse {cite.label}."
        return Answer("answered", text, text, (cite,), False, None, "en-IN", False)

    monkeypatch.setattr(conversation, "_ask", fake_ask)
    body, _ = send(LIFE, monkeypatch, text="who is the nominee?")
    classify_as(monkeypatch, intent="question", grievance_class=None, product="life_policy")
    assert body["messages"][0]["text"] == "The nominee is Example Spouse."


def test_text_with_no_claim_under_way_gets_help_not_the_health_checklist(monkeypatch):
    assert chat("hmm okay then") == [conversation.HELP]
    assert chat("what documents are missing?")[0].startswith("Still missing, 6 of 6")  # asked for: shown


def test_a_grievance_with_no_clear_class_tries_her_policy_first(monkeypatch):
    def fake_ask(question, **kwargs):
        cite = Citation(mine.YOUR_DOCUMENT, "policy.pdf", 8, "", mine.VERIFIED_BY)
        text = f"For theft, file a police FIR within 24 hours {cite.label}."
        return Answer("answered", text, text, (cite,), False, None, "en-IN", False)

    send(BIKE, monkeypatch)
    monkeypatch.setattr(conversation, "_ask", fake_ask)
    classify_as(monkeypatch, intent="grievance", grievance_class=None, product="motor_policy")
    assert chat("my bike was stolen, what now?") == ["For theft, file a police FIR within 24 hours."]
