"""n8n delivery and follow-up: sending needs approval and her consent, callbacks need the secret,
sent means n8n said so, and the response clock runs on the step's own window."""

from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, store
from app.main import app
from app.services import n8n
from scripts import seed_demo

client = TestClient(app)
CASE = "demo-moratorium"
SECRET = "test-secret"
HEADERS = {"x-praman-secret": SECRET}


class FakeResponse:
    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(config, "N8N_DISPATCH_URL", "https://n8n.example/webhook/praman")
    monkeypatch.setattr(config, "N8N_SECRET", SECRET)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://praman.example")
    conn = store.connect()
    try:
        seed_demo.seed(conn)
    finally:
        conn.close()


@pytest.fixture
def posted(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def approved_draft(consent=True) -> int:
    conn = store.connect()
    try:
        from app.services import drafts

        draft = drafts.compose(conn, CASE, "en-IN")
        drafts.approve(conn, CASE, draft["id"])
        if consent:
            store.record_consent(conn, CASE, "contact_insurer", True)
        return draft["id"]
    finally:
        conn.close()


def send(draft_id):
    return client.post(f"/api/case/{CASE}/draft/{draft_id}/send")


def events(kind):
    conn = store.connect()
    try:
        return store.case_events(conn, CASE, kind)
    finally:
        conn.close()


def status_of(draft_id):
    conn = store.connect()
    try:
        return store.get_draft(conn, CASE, draft_id)["status"]
    finally:
        conn.close()


# --- Sending ---------------------------------------------------------------------


def test_sending_is_refused_when_n8n_is_not_set_up(monkeypatch, posted):
    draft_id = approved_draft()
    monkeypatch.setattr(config, "N8N_DISPATCH_URL", "")
    assert send(draft_id).status_code == 503
    assert posted == []


def test_a_letter_that_is_only_drafted_is_not_sent(posted):
    conn = store.connect()
    try:
        from app.services import drafts

        draft_id = drafts.compose(conn, CASE, "en-IN")["id"]
    finally:
        conn.close()
    response = send(draft_id)
    assert response.status_code == 409 and "Approve" in response.json()["error"]
    assert posted == []


def test_a_letter_is_not_sent_without_her_consent_to_contact_the_insurer(posted):
    draft_id = approved_draft(consent=False)
    response = send(draft_id)
    assert response.status_code == 409 and "contacting the insurer" in response.json()["error"]
    assert posted == [] and status_of(draft_id) == "approved"


def test_dispatch_sends_only_what_delivery_needs_signed_with_the_secret(posted):
    draft_id = approved_draft()
    response = send(draft_id)
    assert response.status_code == 200 and response.json()["status"] == "sending"

    call, = posted
    assert call["url"] == "https://n8n.example/webhook/praman"
    assert call["headers"] == HEADERS
    payload = call["json"]
    assert set(payload) == {
        "case_id", "draft_id", "kind", "addressee", "letter", "step", "respond_within_days", "callback_base",
    }
    assert payload["addressee"] == "Grievance cell, Example General Insurance Company Ltd"
    assert payload["step"] == "insurer_grievance_cell" and payload["respond_within_days"] == 14
    assert payload["callback_base"] == "https://praman.example"
    assert status_of(draft_id) == "sending"
    assert [e["detail"]["draft_id"] for e in events("letter_dispatched")] == [draft_id]
    assert events("letter_delivered") == []  # handed over is not delivered


def test_the_letter_is_redacted_before_it_leaves(posted):
    draft_id = approved_draft()
    conn = store.connect()
    try:
        conn.execute("UPDATE drafts SET text = text || ?", ("\nMy PAN is ABCDE1234F.",))
        conn.commit()
    finally:
        conn.close()
    send(draft_id)
    assert "ABCDE1234F" not in posted[0]["json"]["letter"]


def test_a_workflow_that_cannot_be_reached_leaves_the_letter_approved(monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", down)
    draft_id = approved_draft()
    assert send(draft_id).status_code == 502
    assert status_of(draft_id) == "approved" and events("letter_dispatched") == []


def test_a_letter_already_sent_is_not_sent_twice(posted):
    draft_id = approved_draft()
    send(draft_id)
    assert send(draft_id).status_code == 409
    assert len(posted) == 1


# --- Callbacks -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["delivered", "failed", "clock-due"])
def test_callbacks_are_refused_without_the_secret(path):
    body = {"case_id": CASE, "draft_id": 1}
    assert client.post(f"/api/n8n/{path}", json=body).status_code == 401
    assert client.post(f"/api/n8n/{path}", json=body, headers={"x-praman-secret": "wrong"}).status_code == 401


def test_callbacks_are_refused_when_no_secret_is_configured(monkeypatch):
    monkeypatch.setattr(config, "N8N_SECRET", "")
    response = client.post("/api/n8n/clock-due", json={"case_id": CASE}, headers={"x-praman-secret": ""})
    assert response.status_code == 401


def delivered(draft_id, channel="email"):
    return client.post("/api/n8n/delivered", json={"case_id": CASE, "draft_id": draft_id, "channel": channel}, headers=HEADERS)


def test_delivery_marks_the_letter_sent_and_starts_the_clock_on_its_own_window(posted):
    draft_id = approved_draft()
    send(draft_id)
    body = delivered(draft_id).json()
    assert body["status"] == "sent"
    assert body["clock"]["step"] == "insurer_grievance_cell"
    assert body["clock"]["respond_by"] == (date.today() + timedelta(days=14)).isoformat()
    assert body["clock"]["verified_by"] == "UNVERIFIED"
    assert status_of(draft_id) == "sent"
    assert events("letter_delivered")[0]["detail"] == {"draft_id": draft_id, "channel": "email"}
    assert len(events("clock_started")) == 1


def test_a_repeated_delivery_callback_changes_nothing(posted):
    draft_id = approved_draft()
    send(draft_id)
    first = delivered(draft_id).json()
    assert delivered(draft_id).json() == first
    assert len(events("letter_delivered")) == 1 and len(events("clock_started")) == 1


def test_a_failed_delivery_puts_the_letter_back_to_approved(posted):
    draft_id = approved_draft()
    send(draft_id)
    response = client.post("/api/n8n/failed", json={"case_id": CASE, "draft_id": draft_id, "reason": "mailbox full"}, headers=HEADERS)
    assert response.json()["status"] == "approved"
    assert status_of(draft_id) == "approved"
    assert events("delivery_failed")[0]["detail"]["reason"] == "mailbox full"
    assert events("letter_delivered") == [] and events("clock_started") == []
    assert send(draft_id).status_code == 200  # she can send it again


def test_a_callback_for_another_cases_draft_is_not_found():
    response = client.post("/api/n8n/delivered", json={"case_id": CASE, "draft_id": 9999}, headers=HEADERS)
    assert response.status_code == 404


# --- The clock -------------------------------------------------------------------


def run_clock(today=None, notify=None):
    conn = store.connect()
    try:
        return n8n.clock_due(conn, CASE, notify=notify, today=today)
    finally:
        conn.close()


def sent_letter():
    draft_id = approved_draft()
    send(draft_id)
    return delivered(draft_id).json()["clock"]["respond_by"]


def test_with_no_clock_there_is_nothing_to_do():
    conn = store.connect()
    try:
        store.record_consent(conn, CASE, "contact_insurer", True)
    finally:
        conn.close()
    assert run_clock() == {"action": "stop", "reason": "no_known_deadline"}


def test_before_the_window_ends_the_workflow_is_told_to_keep_waiting(posted):
    respond_by = sent_letter()
    assert run_clock(today=date.fromisoformat(respond_by)) == {"action": "wait", "respond_by": respond_by}
    assert events("clock_due") == []


def test_after_the_window_the_ladder_picks_the_next_step_and_she_is_told(posted):
    respond_by = date.fromisoformat(sent_letter())
    told = []
    result = run_clock(today=respond_by + timedelta(days=1), notify=lambda case, text: told.append(text))
    assert result["action"] == "escalate"
    assert result["step"] == "insurer_grievance_cell" and result["next_step"] == "irdai_grievance"
    assert result["brief_path"] == f"/api/case/{CASE}/brief"
    assert "has not replied" in result["message"] and "filed" not in result["message"].lower()
    assert told == [result["message"]]
    assert events("clock_due")[0]["detail"] == {"step": "insurer_grievance_cell", "action": "escalate", "next_step": "irdai_grievance"}


def test_the_same_window_is_handled_once(posted):
    respond_by = date.fromisoformat(sent_letter())
    late = respond_by + timedelta(days=1)
    run_clock(today=late)
    assert run_clock(today=late) == {"action": "stop", "reason": "already_handled"}
    assert len(events("clock_due")) == 1


def test_a_failing_notification_does_not_fail_the_answer(posted):
    respond_by = date.fromisoformat(sent_letter())

    def broken(case, text):
        raise RuntimeError("whatsapp down")

    assert run_clock(today=respond_by + timedelta(days=1), notify=broken)["action"] == "escalate"


def test_the_last_step_on_a_ladder_asks_a_person(posted, monkeypatch):
    respond_by = date.fromisoformat(sent_letter())
    monkeypatch.setattr(n8n, "_route", lambda conn, case_id: type("R", (), {
        "steps": ("insurer_grievance_cell",), "respondent_name": "Example General Insurance Company Ltd",
    })())
    result = run_clock(today=respond_by + timedelta(days=1))
    assert result["action"] == "ask_agent" and result["next_step"] is None


def test_a_resolved_case_stops_the_workflow(posted):
    respond_by = date.fromisoformat(sent_letter())
    conn = store.connect()
    try:
        store.record_event(conn, CASE, "case_status", {"status": "resolved"})
    finally:
        conn.close()
    assert run_clock(today=respond_by + timedelta(days=1)) == {"action": "stop", "reason": "case_resolved"}


def test_withdrawn_consent_stops_the_workflow(posted):
    respond_by = date.fromisoformat(sent_letter())
    conn = store.connect()
    try:
        store.record_consent(conn, CASE, "contact_insurer", False)
    finally:
        conn.close()
    assert run_clock(today=respond_by + timedelta(days=1)) == {"action": "stop", "reason": "consent_withdrawn"}


def test_a_deleted_case_stops_the_workflow(posted):
    respond_by = date.fromisoformat(sent_letter())
    assert client.delete(f"/api/case/{CASE}").status_code == 200
    assert run_clock(today=respond_by + timedelta(days=1)) == {"action": "stop", "reason": "case_deleted"}


def test_the_console_counts_letters_sent_and_follow_ups_from_events(posted):
    respond_by = date.fromisoformat(sent_letter())
    run_clock(today=respond_by + timedelta(days=1))
    counters = client.get("/api/metrics").json()["counters"]
    assert counters["letters_sent"] == 1 and counters["follow_ups_triggered"] == 1
