"""The phone channel: Sarvam's voice agent asks Praman for each answer. Same conversation as the web chat
and WhatsApp, keyed by the caller's number; replies made to be heard; a complaint offers the number she is
calling from; the call-ended webhook keeps only that the call happened."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import complaints, config, conversation, guided, store, voice_agent
from app.clients import sarvam
from app.main import app
from app.services import i18n, policy_search

client = TestClient(app)
SECRET = "voice-secret"
BEARER = {"Authorization": f"Bearer {SECRET}"}
CALLER = "+919876543210"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "VOICE_AGENT_SECRET", SECRET)
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: None)
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: text)
    monkeypatch.setattr(sarvam, "identify_language", lambda text: {})
    monkeypatch.setattr(policy_search, "suggest", lambda kind, needs=None, language="en-IN": "Options found online: 1. Plan A.")
    i18n._CACHE.clear()
    guided._FIND.clear()
    guided._COMPLAINT.clear()
    conversation._AWAITING_CONSENT.clear()
    yield
    i18n._CACHE.clear()


def classify_as(monkeypatch, **payload):
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: payload)


def ask(text="", journey=None, caller=CALLER, language="English", headers=BEARER):
    body = {"caller": caller, "text": text, "language": language}
    if journey:
        body["journey"] = journey
    return client.post("/api/voice-agent/turn", json=body, headers=headers)


def case_id(caller=CALLER):
    c = store.connect()
    return store.find_case_for_user(c, voice_agent.phone_user(caller))["id"]


# --- Who is calling ----------------------------------------------------------------------


@pytest.mark.parametrize("number, expected", [
    ("+919876543210", "whatsapp:+919876543210"),
    ("919876543210", "whatsapp:+919876543210"),
    ("9876543210", "whatsapp:+919876543210"),
    ("+91 98765-43210", "whatsapp:+919876543210"),
    ("12345", None), ("", None), (None, None), ("not a number", None),
])
def test_a_phone_number_is_the_same_user_as_her_whatsapp(number, expected):
    assert voice_agent.phone_user(number) == expected


@pytest.mark.parametrize("said, code", [("Hindi", "hi-IN"), ("hi-IN", "hi-IN"), ("marathi", "mr-IN"), ("hi", "hi-IN"), ("Klingon", None), (None, None)])
def test_the_language_the_agent_names_becomes_a_code(said, code):
    assert voice_agent.language_code(said) == code


# --- Replies made to be heard --------------------------------------------------------------


def test_speech_has_no_citations_links_or_list_marks():
    text = "Your cover is 5 lakh [Example, policy, p.2].\n- Room rent is capped.\n- See https://example.com/page for more.\n1. Second point"
    out = voice_agent.speakable(text)
    assert "[" not in out and "http" not in out and "\n" not in out and not out.startswith("-")
    assert "Room rent is capped." in out and "Second point" in out


def test_long_text_is_cut_at_a_sentence():
    text = " ".join(f"This is sentence number {i}." for i in range(1, 60))
    out = voice_agent.speakable(text, 200)
    assert len(out) <= 200 and out.endswith(".") and "sentence number 1." in out


# --- The endpoints are for the agent only ------------------------------------------------------


def test_the_tool_endpoint_needs_the_secret():
    assert ask("hello", headers={}).status_code == 401
    assert ask("hello", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert ask("hello", headers={"x-praman-secret": SECRET}).status_code == 200


def test_nothing_is_allowed_when_no_secret_is_set(monkeypatch):
    monkeypatch.setattr(config, "VOICE_AGENT_SECRET", "")
    assert ask("hello", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.post("/api/voice-agent/webhook", json={}, headers={}).status_code == 401


def test_a_caller_that_is_not_a_number_is_refused():
    assert ask("hello", caller="somebody").status_code == 400
    assert client.post("/api/voice-agent/turn", json={"text": "hi"}, headers=BEARER).status_code == 400


# --- The three journeys by phone -----------------------------------------------------------------


def test_choosing_find_a_policy_opens_with_what_she_can_say():
    out = ask(journey="find").json()
    assert out["journey"] == "find" and "Which insurance" in out["reply"]
    assert out["options"] == ["Health", "Life", "Motor"] and "You can say: Health, Life, Motor." in out["reply"]


def test_find_a_policy_runs_the_same_questions_in_words(monkeypatch):
    ask(journey="find")
    assert "Who is this insurance for" in ask("health insurance", "find").json()["reply"]
    assert "each of your parents" in ask("it is for my parents", "find").json()["reply"]
    assert "illness" in ask("they are 62 and 58", "find").json()["reply"]
    assert "How much cover" in ask("yes, my father has diabetes", "find").json()["reply"]
    assert "yearly premium" in ask("10 lakh", "find").json()["reply"]
    final = ask("skip", "find").json()["reply"]
    assert "Options found online" in final and "Before you buy, check" in final
    assert "http" not in final and "\n" not in final


def test_the_journey_is_remembered_between_turns():
    ask(journey="find")
    again = ask("health", None).json()  # the agent did not repeat the journey
    assert again["journey"] == "find" and "Who is this insurance for" in again["reply"]


def test_check_my_policy_without_a_policy_says_to_send_one():
    out = ask("what does my policy cover", "check").json()
    assert out["journey"] == "check"
    assert "photo or PDF" in out["reply"] or "policy" in out["reply"].lower()


def test_a_policy_sent_on_whatsapp_is_the_one_asked_about_on_the_phone(monkeypatch):
    c = store.connect()
    whatsapp_case = store.case_for_user(c, "whatsapp:+919876543210")["id"]
    ask("hello", "check")
    assert case_id() == whatsapp_case  # one person, one case, whichever channel


def test_a_complaint_by_phone_offers_the_number_she_is_calling_from(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    ask(journey="complain")
    first = ask("my claim was rejected after six years", "complain").json()
    assert "Talk to someone" in first["reply"]
    asked = ask("talk to someone", "complain").json()["reply"]
    assert "calling from, ending 3210" in asked
    confirm = ask("yes", "complain").json()["reply"]
    assert "Reply YES to register" in confirm and "+91******3210" in confirm
    done = ask("yes", "complain").json()["reply"]
    assert "registered" in done and "C-" in done

    [item] = complaints.listing(store.connect(), "pending")
    assert item["contact"] == "+919876543210" and item["case_id"] == case_id()
    assert "claim was rejected" in item["text"]


def test_she_can_give_a_different_number_instead(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    ask(journey="complain")
    ask("my claim was rejected", "complain")
    ask("talk to someone", "complain")
    confirm = ask("use 9123456789 instead", "complain").json()["reply"]
    assert "+91******6789" in confirm
    ask("yes", "complain")
    assert complaints.listing(store.connect(), "pending")[0]["contact"] == "+919123456789"


def test_silence_gets_a_polite_prompt():
    assert "say that again" in ask("", None).json()["reply"]


# --- When a call ends -------------------------------------------------------------------------


def test_the_webhook_needs_the_secret_and_keeps_only_that_the_call_happened():
    ask("hello", "check")
    payload = {
        "status": "connected", "duration": 95, "attempt_id": "a1", "interaction_id": "i1",
        "channel_info": {"agent_phone": "+911111111111"}, "caller_number": CALLER,
        "interaction_transcript": [{"role": "user", "en_text": "my pan is ABCDE1234F"}],
    }
    assert client.post("/api/voice-agent/webhook", json=payload).status_code == 401
    assert client.post("/api/voice-agent/webhook?token=wrong", json=payload).status_code == 401
    ok = client.post(f"/api/voice-agent/webhook?token={SECRET}", json=payload)
    assert ok.status_code == 200 and ok.json() == {"recorded": True}
    [event] = store.case_events(store.connect(), case_id(), "call_completed")
    assert event["detail"] == {"status": "connected", "seconds": 95, "direction": "inbound"}
    assert "ABCDE" not in str(event)


def test_an_outbound_call_is_matched_by_the_case_id_it_was_started_with():
    c = store.connect()
    case = store.case_for_user(c, "whatsapp:+919000000000")
    payload = {"status": "no_answer", "duration": None, "webhook_config": {"url": "x", "metadata": {"case_id": case["id"]}}}
    assert client.post(f"/api/voice-agent/webhook?token={SECRET}", json=payload).json() == {"recorded": True}
    [event] = store.case_events(c, case["id"], "call_completed")
    assert event["detail"]["direction"] == "outbound" and event["detail"]["seconds"] is None


def test_a_call_from_nobody_we_know_is_not_an_error():
    ok = client.post(f"/api/voice-agent/webhook?token={SECRET}", json={"status": "failed"})
    assert ok.status_code == 200 and ok.json() == {"recorded": False}


# --- Ringing her (the outbound call) ------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {"attempt_id": "att-1"}, "x"

    def json(self):
        return self._body


@pytest.fixture
def voice_ids(monkeypatch):
    for name, value in {
        "SARVAM_VOICE_API_KEY": "key", "SARVAM_VOICE_ORG_ID": "org1", "SARVAM_VOICE_WORKSPACE_ID": "ws1",
        "SARVAM_VOICE_APP_ID": "app1", "SARVAM_VOICE_CONNECTION_ID": "conn1", "SARVAM_VOICE_AGENT_NUMBER": "+911800000000",
    }.items():
        monkeypatch.setattr(config, name, value)


def test_the_call_request_has_what_sarvam_needs_and_masks_everything_else(monkeypatch, voice_ids):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    attempt = sarvam.place_outbound_call(
        "+919876543210", language="Hindi", variables={"note": "my PAN is ABCDE1234F"},
        webhook_url="https://praman.example/hook", metadata={"case_id": "c1"},
    )
    assert attempt == "att-1"
    assert seen["url"] == "https://apps.sarvam.ai/api/outbounds/v1/orgs/org1/workspaces/ws1/outbounds"
    assert seen["headers"] == {"X-API-Key": "key"}
    body = seen["json"]
    assert body["user_config"] == {"user_phone_number": "+919876543210"}
    assert body["app_config"]["connection_config"] == {"connection_id": "conn1", "agent_phone_number": "+911800000000"}
    assert body["app_config"]["app_overrides"] == {"initial_language_name": "Hindi"}
    assert "ABCDE1234F" not in str(body) and body["webhook_config"]["metadata"] == {"case_id": "c1"}


def test_calling_is_refused_when_the_agent_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "SARVAM_VOICE_ORG_ID", "")
    with pytest.raises(sarvam.SarvamUnavailable):
        sarvam.place_outbound_call("+919876543210")


@pytest.mark.parametrize("status, error", [(422, sarvam.SarvamBadRequest), (401, sarvam.SarvamBadRequest), (500, sarvam.SarvamUnavailable)])
def test_a_failed_call_request_is_reported(monkeypatch, voice_ids, status, error):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(status))
    with pytest.raises(error):
        sarvam.place_outbound_call("+919876543210")


def test_an_unreachable_calling_service_is_reported(monkeypatch, voice_ids):
    def down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(sarvam.SarvamUnavailable):
        sarvam.place_outbound_call("+919876543210")
