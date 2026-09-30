"""Endpoints the demo web site needs. They reuse the WhatsApp conversation pipeline."""

import base64

import pytest
from fastapi.testclient import TestClient

from app import config, conversation, store
from app.clients import sarvam
from app.main import app
from app.rag.answer import Answer, Citation
from app.services import i18n, voice
from scripts import seed_demo

client = TestClient(app)
SESSION = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "DISTRIBUTOR_LEGAL_NAME", "Example Broking Pvt Ltd")
    monkeypatch.setattr(config, "DISTRIBUTOR_SHORT_NAME", "Paytm")
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: None)
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: f"[{target}] {text}")
    monkeypatch.setattr(sarvam, "identify_language", lambda text: {})
    monkeypatch.setattr(
        sarvam, "text_to_speech", lambda text, **k: {"audios": [base64.b64encode(b"ID3-mp3").decode()]}
    )
    i18n._CACHE.clear()
    voice._MEDIA.clear()
    conversation._AWAITING_CONSENT.clear()
    yield
    i18n._CACHE.clear()


def classify_as(monkeypatch, **payload):
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: payload)


def chat(text, **extra):
    return client.post("/api/chat", json={"session_id": SESSION, "text": text, "language": "en-IN", **extra})


# --- Session ---------------------------------------------------------------------


def test_a_browser_session_keeps_one_case():
    first = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    again = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    assert first == again


@pytest.mark.parametrize("session_id", ["", "../../etc", "x" * 200, None])
def test_a_bad_session_id_is_refused(session_id):
    assert client.post("/api/session", json={"session_id": session_id}).status_code == 400


# --- Chat: the same pipeline as WhatsApp -----------------------------------------


def test_unclassified_text_gets_the_checklist_status_with_its_badge():
    body = chat("hello").json()
    [message] = body["messages"]
    assert message["text"].startswith("Still missing, 6 of 6")
    assert message["unverified"] is True
    assert body["case_id"]


def test_a_grievance_is_routed_to_whoever_owes_the_answer(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="platform/payment_failed", product="health_policy")
    [message] = chat("premium was debited twice").json()["messages"]
    assert message["text"] == "This one is for Example Broking Pvt Ltd. First step: Customer support."
    metrics = client.get("/api/metrics").json()
    assert metrics["headline"]["needed_distributor"] == 1


def test_a_question_is_answered_from_sources_with_citations(monkeypatch):
    classify_as(monkeypatch, intent="question", grievance_class=None, product="health_policy")
    seen = {}

    def fake_ask(question, **kwargs):
        seen.update(kwargs)
        cite = Citation("Example General Insurance", "policy_wording", 1, "", "UNVERIFIED")
        text = f"Room rent is limited to 1% of the sum insured per day {cite.label}."
        return Answer("answered", text, text, (cite,), True, None, "en-IN", False)

    monkeypatch.setattr(conversation, "_ask", fake_ask)
    body = chat("what is the room rent limit?", insurer="Example General Insurance Company Ltd",
                product="health_policy").json()
    [message] = body["messages"]

    assert seen["insurer"] == "Example General Insurance"  # resolved to the corpus spelling
    assert seen["product"] == "health_policy"
    assert message["unverified"] is True
    assert message["citations"] == [{
        "label": "[Example General Insurance, policy_wording, p.1]", "insurer": "Example General Insurance",
        "doc_type": "policy_wording", "page": 1, "source_url": "", "verified_by": "UNVERIFIED",
    }]


def test_replies_come_in_the_chosen_language():
    body = client.post("/api/chat", json={"session_id": SESSION, "text": "hello", "language": "mr-IN"}).json()
    assert body["messages"][0]["text"].startswith("[mr-IN] Still missing")
    assert body["language"] == "mr-IN"


def test_chat_can_speak_its_reply():
    [message] = chat("hello", speak=True).json()["messages"]
    assert message["audio_url"].startswith("/media/") and message["audio_url"].endswith(".mp3")
    assert client.get(message["audio_url"]).content == b"ID3-mp3"


def test_delete_everything_works_in_the_web_chat_too():
    case_id = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    [message] = chat("delete everything").json()["messages"]
    assert message["text"] == conversation.DELETED
    conn = store.connect()
    try:
        assert store.get_case(conn, case_id) is None
    finally:
        conn.close()


def test_chat_needs_text():
    assert chat("   ").status_code == 400


# --- Voice -----------------------------------------------------------------------


def test_voice_is_transcribed_then_answered_and_spoken(monkeypatch):
    monkeypatch.setattr(sarvam, "speech_to_text", lambda data, filename, **k: {"transcript": "what is missing"})
    response = client.post(
        "/api/voice",
        data={"session_id": SESSION, "language": "en-IN"},
        files={"audio": ("note.webm", b"webm-bytes", "audio/webm")},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["transcript"] == "what is missing"
    assert body["messages"][0]["text"].startswith("Still missing")
    assert body["messages"][0]["audio_url"].endswith(".mp3")


def test_voice_that_cannot_be_transcribed_says_so(monkeypatch):
    monkeypatch.setattr(sarvam, "speech_to_text", lambda data, filename, **k: {"transcript": ""})
    response = client.post("/api/voice", data={"session_id": SESSION},
                           files={"audio": ("note.webm", b"x", "audio/webm")})
    assert response.status_code == 422


def test_voice_needs_audio():
    assert client.post("/api/voice", data={"session_id": SESSION}).status_code == 422


# --- My policies (demo fixture) ----------------------------------------------------


def test_the_demo_policy_card():
    [policy] = client.get("/api/policies").json()["policies"]
    assert policy["example"].startswith("Example case")
    assert policy["insurer"] == "Example General Insurance Company Ltd"
    assert policy["product"] == "health_policy"
    assert policy["sum_insured"] == 500000
    assert policy["room_cap_percent"] == 1
    assert policy["room_cap_per_day"] == 5000  # 1% of the sum insured, worked out by the engine
    assert policy["waiting_periods"] == {"pre_existing_months": 36, "specified_disease_months": 24}


# --- Readiness from documents ------------------------------------------------------


def demo_readiness():
    case_id = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    return client.post("/api/readiness/documents", data={"case_id": case_id, "demo": "true"}).json(), case_id


def test_demo_files_give_a_verdict_and_ask_what_the_documents_do_not_say():
    body, _ = demo_readiness()
    assert body["documents"]["policy"]["source"] == "fixture"
    assert body["documents"]["policy"]["example"].startswith("Example case")
    assert body["outcome"] == "facts_pending"
    required = {q["fact"] for q in body["questions"] if q["required"]}
    assert {"wait_months", "exclusion_listed", "policy_in_force", "documents_collected"} <= required
    heads = body["documents"]["bill"]["heads"]
    assert heads["deductible_total"] == 95000 and heads["exempt_total"] == 37000
    assert heads["unmapped"] == []


def test_answering_the_questions_gives_a_known_deduction():
    body, case_id = demo_readiness()
    facts = {**body["facts"], "wait_months": 24, "exclusion_listed": False, "policy_in_force": True,
             "documents_collected": 6, "documents_required": 6}
    result = client.post("/api/readiness", json={"case_id": case_id, "facts": facts}).json()

    assert result["outcome"] == "file_with_known_deduction"
    assert result["breakdown"] == {
        "room_cap_per_day": 5000.0, "room_quoted_per_day": 8000.0, "ratio": 0.625,
        "deductible_heads": 95000.0, "exempt_heads": 37000.0, "deduction": 35625,
        "payable_estimate": 96375, "pending": False,
    }
    assert any("About ₹35,625 will be cut" in m["text"] for m in result["messages"])
    assert [q for q in result["questions"] if q["required"]] == []


def test_real_files_are_not_read_without_consent():
    case_id = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    response = client.post("/api/readiness/documents", data={"case_id": case_id},
                           files={"policy": ("policy.png", PNG, "image/png")})
    assert response.status_code == 403


def test_readiness_documents_needs_files_or_the_demo():
    case_id = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    assert client.post("/api/readiness/documents", data={"case_id": case_id}).status_code == 400


# --- Case detail, drafts and approval ----------------------------------------------


@pytest.fixture
def seeded():
    conn = store.connect()
    try:
        seed_demo.seed(conn)
    finally:
        conn.close()


def test_case_detail_names_the_respondent_and_the_clock_to_come(seeded):
    body = client.get("/api/case/demo-moratorium").json()
    assert body["example"] is True
    assert body["respondent_name"] == "Example General Insurance Company Ltd"
    assert body["route"]["steps"][0] == {
        "step": "insurer_grievance_cell", "label": "Grievance cell", "respond_within_days": 14,
        "verified_by": "UNVERIFIED",
    }
    assert body["verdict"] == "file"
    assert body["clock"] is None  # nothing is sent, so no clock has started


def test_a_draft_is_addressed_by_name_and_approval_is_approved_and_ready_to_send(seeded):
    draft = client.post("/api/case/demo-moratorium/draft", json={"language": "en-IN"}).json()["draft"]
    assert draft["text"].startswith("To: Grievance cell, Example General Insurance Company Ltd")
    assert "non-disclosure" in draft["text"]
    assert draft["status"] == "drafted"
    assert draft["unverified"] is True
    assert client.get("/api/metrics").json()["counters"]["escalations_drafted"] == 2  # the seeded draft and this one

    approved = client.post(f"/api/case/demo-moratorium/draft/{draft['id']}/approve").json()
    assert approved["message"] == "Approved and ready to send"
    assert approved["draft"]["status"] == "approved"
    for text in (draft["text"], approved["message"]):
        assert "filed" not in text.lower() and "submitted" not in text.lower()


def test_a_draft_is_read_back_in_her_language(seeded):
    draft = client.post("/api/case/demo-moratorium/draft", json={"language": "mr-IN"}).json()["draft"]
    assert draft["readback"].startswith("[mr-IN]")


def test_a_draft_without_a_legal_name_is_refused(monkeypatch):
    monkeypatch.setattr(config, "DISTRIBUTOR_LEGAL_NAME", "")
    classify_as(monkeypatch, intent="grievance", grievance_class="platform/refund", product="health_policy")
    case_id = chat("my refund has not come").json()["case_id"]
    assert client.post(f"/api/case/{case_id}/draft", json={}).status_code == 409


def test_unknown_cases_are_404():
    assert client.get("/api/case/nobody").status_code == 404
    assert client.post("/api/case/nobody/draft", json={}).status_code == 404
    assert client.post("/api/case/nobody/draft/1/approve").status_code == 404


def test_delete_removes_drafts_too(seeded):
    client.post("/api/case/demo-moratorium/draft", json={})
    deleted = client.delete("/api/case/demo-moratorium").json()["deleted"]
    assert deleted["drafts"] == 2  # the seeded draft and this one


# --- Demo seed ----------------------------------------------------------------------


def test_seed_is_idempotent_and_every_case_is_labelled_an_example(seeded):
    conn = store.connect()
    try:
        seed_demo.seed(conn)
    finally:
        conn.close()
    rows = client.get("/api/console/cases").json()["cases"]
    assert len(rows) == 3 and all(r["example"] for r in rows)
    headline = client.get("/api/metrics").json()["headline"]
    assert headline["text"] == "Of 3 cases, 1 needed Paytm."
    assert headline["distributor"] == "Paytm"


# --- CORS -------------------------------------------------------------------------


def preflight(origin):
    return client.options("/api/metrics", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})


def test_cors_allows_the_frontend_dev_origin():
    assert preflight("http://localhost:3000").headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_refuses_other_origins():
    assert "access-control-allow-origin" not in preflight("https://evil.example").headers
