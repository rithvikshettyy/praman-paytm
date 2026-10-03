"""Endpoints the demo web site needs. They reuse the WhatsApp conversation pipeline."""

import base64

import pytest
from fastapi.testclient import TestClient

from app import config, conversation, store
from app.clients import sarvam
from app.main import app
from app.rag.answer import Answer, Citation
from app.services import documents, i18n, voice
from scripts import seed_demo

client = TestClient(app)
SESSION = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
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
    body = chat("status").json()
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


def test_the_screen_and_the_voice_get_the_answer_without_inline_source_labels(monkeypatch):
    classify_as(monkeypatch, intent="question", grievance_class=None, product="health_policy")
    cite = Citation("Your document", "hunter 26-27.pdf", 1, "", "Her own document")
    text = f"The IDV is Rs 1,45,000 {cite.label}. It ends on 02/10/2027 {cite.label}."
    answer = Answer("answered", text, text, (cite,), False, None, "en-IN", False)
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: answer)
    spoken = []
    monkeypatch.setattr(voice, "speak", lambda t, lang: spoken.append(t) or "/media/x.mp3")
    [message] = chat("what is my IDV?", speak=True).json()["messages"]
    assert message["text"] == "The IDV is Rs 1,45,000. It ends on 02/10/2027."
    assert spoken == [message["text"]]
    assert message["citations"][0]["page"] == 1  # the source is still there, as a chip


def test_replies_come_in_the_chosen_language():
    body = client.post("/api/chat", json={"session_id": SESSION, "text": "status", "language": "mr-IN"}).json()
    assert body["messages"][0]["text"].startswith("[mr-IN] Still missing")
    assert body["language"] == "mr-IN"


def test_chat_can_speak_its_reply():
    [message] = chat("status", speak=True).json()["messages"]
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


@pytest.mark.parametrize("text", ["hi", "Hello!", "namaste", "नमस्कार", "good morning"])
def test_a_greeting_gets_a_greeting_without_asking_the_model(monkeypatch, text):
    def no_model(*a, **k):
        raise AssertionError("a greeting needs no model call")

    monkeypatch.setattr(sarvam, "chat_json", no_model)
    [message] = chat(text).json()["messages"]
    assert message["text"] == conversation.GREETING


@pytest.mark.parametrize("text", ["thanks", "Thank you!", "धन्यवाद"])
def test_thanks_gets_a_welcome(text):
    [message] = chat(text).json()["messages"]
    assert message["text"] == conversation.THANKS


def test_other_smalltalk_is_classified_and_greeted(monkeypatch):
    classify_as(monkeypatch, intent="smalltalk", grievance_class=None, product=None)
    [message] = chat("how are you doing today").json()["messages"]
    assert message["text"] == conversation.GREETING


# --- Documents sent in the chat ---------------------------------------------------


def upload(*files, **form):
    return client.post(
        "/api/chat/upload",
        files=[("files", f) for f in files],
        data={"session_id": SESSION, "language": "en-IN", **form},
    )


def test_a_document_in_the_chat_waits_for_consent():
    [message] = upload(("bill.png", PNG, "image/png")).json()["messages"]
    assert message["text"] == conversation.CONSENT_PROMPT


BIKE_POLICY = [(1, "Two Wheeler Package Policy. Insured Declared Value (IDV): Rs 85,000. Period: 01/04/2026 to 31/03/2027."),
               (2, "Own damage cover includes flood and fire. Compulsory deductible: Rs 100.")]


def consent_given():
    case_id = client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]
    client.post("/api/consent", json={"case_id": case_id, "scope": "read_documents", "granted": True})
    return case_id


@pytest.fixture
def her_documents(monkeypatch):
    """Doc AI reads the bike policy; the RAG answer is faked and records where it looked."""
    from app.rag import mine

    asked = []

    def fake_ask(question, **kwargs):
        asked.append((question, kwargs))
        if kwargs.get("insurer") != mine.YOUR_DOCUMENT:
            return Answer("no_source", "not found", "not found", (), False, None, "en-IN", False)
        cite = Citation(mine.YOUR_DOCUMENT, "bike.pdf", 1, "", mine.VERIFIED_BY)
        text = f"A two-wheeler package policy with an IDV of Rs 85,000 {cite.label}."
        return Answer("answered", text, text, (cite,), False, None, "en-IN", False)

    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: BIKE_POLICY)
    monkeypatch.setattr(conversation, "_ask", fake_ask)
    yield asked
    mine._DOCS.clear()


def test_a_document_in_the_chat_is_read_and_explained_not_met_with_a_missing_list(her_documents):
    case_id = consent_given()
    body = upload(("bike.pdf", b"%PDF-1.4 bike", "application/pdf")).json()
    summary, offer = body["messages"]
    assert "IDV of Rs 85,000" in summary["text"]
    assert summary["citations"][0]["label"] == "[Your document, bike.pdf, p.1]"
    assert summary["unverified"] is False  # her own document, not a legal value of ours
    assert offer["text"] == conversation.READ_IT
    assert not any("Still missing" in m["text"] or "Which document" in m["text"] for m in body["messages"])
    question, kwargs = her_documents[0]
    assert question == conversation.SUMMARY_QUESTION and kwargs["question_language"] == "en-IN"
    assert len(client.get(f"/api/checklist/{case_id}").json()["missing"]) == 6  # a bike policy fills no claim slot


def test_a_question_sent_with_the_document_is_answered_instead_of_a_summary(monkeypatch, her_documents):
    consent_given()
    classify_as(monkeypatch, intent="question", grievance_class=None, product="motor_policy")
    body = upload(("bike.pdf", b"%PDF-1.4 bike", "application/pdf"), text="is flood covered?").json()
    answer, offer = body["messages"]
    assert answer["citations"][0]["doc_type"] == "bike.pdf"
    assert offer["text"] == conversation.READ_IT
    assert [q for q, _ in her_documents] == ["is flood covered?"]  # no summary was asked for


@pytest.mark.parametrize("text", ["final bill", "hi", ""])
def test_a_label_or_greeting_with_the_document_still_gets_the_summary(her_documents, text):
    consent_given()
    upload(("bike.pdf", b"%PDF-1.4 bike", "application/pdf"), text=text)
    assert [q for q, _ in her_documents] == [conversation.SUMMARY_QUESTION]


def test_a_broad_question_falls_back_to_the_opening_pages(monkeypatch, her_documents):
    from app.rag import mine

    consent_given()
    upload(("bike.pdf", b"%PDF-1.4 bike", "application/pdf"))
    looked = []

    def fake_ask(question, **kwargs):
        looked.append(kwargs["collection"].count())
        if len(looked) == 1:  # nothing close enough on the first look
            return Answer("no_source", "not found", "not found", (), False, None, "en-IN", False)
        cite = Citation(mine.YOUR_DOCUMENT, "bike.pdf", 1, "", mine.VERIFIED_BY)
        text = f"A two-wheeler policy from 01/04/2026 to 31/03/2027 {cite.label}."
        return Answer("answered", text, text, (cite,), False, None, "en-IN", False)

    monkeypatch.setattr(conversation, "_ask", fake_ask)
    classify_as(monkeypatch, intent="question", grievance_class=None, product=None)
    [message] = chat("tell me what i need to know").json()["messages"]
    assert message["citations"][0]["page"] == 1
    assert len(looked) == 2


def test_when_her_documents_do_not_say_the_reply_says_so(monkeypatch, her_documents):
    consent_given()
    upload(("bike.pdf", b"%PDF-1.4 bike", "application/pdf"))
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: Answer(
        "no_source", "Tell me which policy", "Tell me which policy", (), False, None, "en-IN", False))
    classify_as(monkeypatch, intent="question", grievance_class=None, product=None)
    [message] = chat("what is the room rent limit?").json()["messages"]
    assert message["text"] == conversation.NOT_IN_HERS


def test_her_questions_are_answered_from_her_document_first(monkeypatch, her_documents):
    consent_given()
    upload(("bike.pdf", b"%PDF-1.4 bike", "application/pdf"))
    classify_as(monkeypatch, intent="question", grievance_class=None, product="motor_policy")
    [message] = chat("is flood covered?").json()["messages"]
    assert message["citations"][0]["doc_type"] == "bike.pdf"
    assert her_documents[-1][0] == "is flood covered?"


def test_a_claim_document_sent_in_the_chat_still_ticks_its_slot(monkeypatch, her_documents):
    discharge = [(1, "DISCHARGE SUMMARY. Hospitalisation from 05/10/2026. Room rent and cashless details.")]
    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: discharge)
    case_id = consent_given()
    upload(("d.png", PNG, "image/png"), text="discharge summary")
    assert "discharge_summary" not in client.get(f"/api/checklist/{case_id}").json()["missing"]


def test_an_unreadable_document_asks_for_a_better_copy(monkeypatch):
    consent_given()
    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: [])
    [message] = upload(("blur.png", PNG, "image/png")).json()["messages"]
    assert message["text"] == conversation.COULD_NOT_READ.format(name="blur.png")


def test_delete_everything_forgets_her_documents(her_documents):
    from app.rag import mine

    case_id = consent_given()
    upload(("bike.pdf", b"%PDF-1.4 bike", "application/pdf"))
    assert mine.has(case_id)
    chat("delete everything")
    assert not mine.has(case_id)


def test_the_chat_refuses_a_file_that_is_not_a_photo_or_pdf():
    response = upload(("notes.txt", b"plain text", "text/plain"))
    assert response.status_code == 400
    assert "notes.txt" in response.json()["error"]


def test_chat_upload_needs_a_valid_session():
    response = client.post("/api/chat/upload", files=[("files", ("a.png", PNG, "image/png"))],
                           data={"session_id": "../x"})
    assert response.status_code == 400


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


def test_transcribe_returns_the_words_and_answers_nothing(monkeypatch):
    seen = {}

    def stt(data, filename, **k):
        seen.update(k)
        return {"transcript": " रूम भाड्याची मर्यादा किती आहे? "}

    monkeypatch.setattr(sarvam, "speech_to_text", stt)
    monkeypatch.setattr(conversation, "respond", lambda *a, **k: pytest.fail("transcribe must not answer"))
    response = client.post("/api/transcribe", data={"language": "mr-IN"},
                           files={"audio": ("note.webm", b"webm-bytes", "audio/webm")})
    assert response.json() == {"transcript": "रूम भाड्याची मर्यादा किती आहे?"}
    assert seen["language"] == "mr-IN"


def test_transcribe_with_no_words_says_so(monkeypatch):
    monkeypatch.setattr(sarvam, "speech_to_text", lambda data, filename, **k: {"transcript": ""})
    response = client.post("/api/transcribe", files={"audio": ("note.webm", b"x", "audio/webm")})
    assert response.status_code == 422


def test_transcribe_needs_audio():
    assert client.post("/api/transcribe", data={"language": "en-IN"}).status_code == 422


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
    assert heads["deductible_total"] == 95000 and heads["exempt_total"] == 38850
    assert heads["unmapped"] == []


def test_answering_the_questions_gives_a_known_deduction():
    body, case_id = demo_readiness()
    facts = {**body["facts"], "wait_months": 24, "exclusion_listed": False, "policy_in_force": True,
             "documents_collected": 6, "documents_required": 6}
    result = client.post("/api/readiness", json={"case_id": case_id, "facts": facts}).json()

    assert result["outcome"] == "file_with_known_deduction"
    assert result["breakdown"] == {
        "room_cap_per_day": 5000.0, "room_quoted_per_day": 8000.0, "ratio": 0.625,
        "deductible_heads": 95000.0, "exempt_heads": 38850.0, "deduction": 35625,
        "payable_estimate": 98225, "pending": False,
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
