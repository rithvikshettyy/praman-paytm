"""Premium reminders: the date is hers to confirm, nothing is set up without her consent, only what the
workflow needs leaves, and a reminder goes out only while it is still wanted."""

from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, conversation, store
from app.clients import sarvam
from app.main import app
from app.services import n8n, reminders

client = TestClient(app)
SECRET = "test-secret"
HEADERS = {"x-praman-secret": SECRET}
CASE = "web:reminders"
TODAY = date(2027, 3, 1)
DUE = date(2027, 3, 14)


class FakeResponse:
    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(config, "N8N_REMINDER_URL", "https://n8n.example/webhook/reminder")
    monkeypatch.setattr(config, "N8N_SECRET", SECRET)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://praman.example")
    monkeypatch.setattr(config, "REMINDER_EMAIL", "someone@example.com")
    monkeypatch.setattr(config, "REMINDER_DAYS_BEFORE", (7, 1))
    monkeypatch.setattr(sarvam, "identify_language", lambda text: None)
    conversation._REMINDER_FLOW.clear()


@pytest.fixture
def conn():
    connection = store.connect()
    store.ensure_case(connection, CASE)
    yield connection
    connection.close()


@pytest.fixture
def posted(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def chat_case(conn) -> str:
    """The case the chat opened for this user (a user id is not a case id)."""
    return store.find_case_for_user(conn, CASE)["id"]


def say(conn, text):
    reply = conversation.respond(conn, CASE, text)
    return " ".join(m.text for m in reply.messages)


# --- Reading a date and planning the reminders -------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("14/03/2027", DUE),
    ("14-03-2027", DUE),
    ("14.03.2027", DUE),
    ("2027-03-14", DUE),
    ("due on 14 March 2027", DUE),
    ("14th Mar, 2027", DUE),
    ("March 14, 2027", DUE),
    ("The premium of Rs 5,000 is due 05/12/2027.", date(2027, 12, 5)),
])
def test_a_date_is_read_day_first(text, expected):
    assert reminders.parse_date(text) == expected


@pytest.mark.parametrize("text", ["", "soon", "31/02/2027", "13/13/2027", "premium 5000", "14 Marchh 2027"])
def test_no_date_or_an_impossible_one_is_none(text):
    assert reminders.parse_date(text) is None


def test_reminders_are_set_only_for_days_still_ahead():
    assert reminders.plan(DUE, TODAY) == [date(2027, 3, 7), date(2027, 3, 13)]
    assert reminders.plan(DUE, date(2027, 3, 10)) == [date(2027, 3, 13)]
    assert reminders.plan(DUE, date(2027, 3, 14)) == []
    assert reminders.plan(DUE, TODAY, (7, 7, 1)) == [date(2027, 3, 7), date(2027, 3, 13)]


# --- Handing it to n8n ---------------------------------------------------------------


def test_nothing_is_scheduled_when_reminders_are_not_set_up(conn, posted, monkeypatch):
    store.record_consent(conn, CASE, reminders.CONSENT, True)
    monkeypatch.setattr(config, "REMINDER_EMAIL", "")
    with pytest.raises(n8n.NotConfigured):
        reminders.schedule(conn, CASE, DUE, TODAY)
    assert posted == []


def test_nothing_is_scheduled_without_her_consent(conn, posted):
    with pytest.raises(n8n.NotReady, match="Agree"):
        reminders.schedule(conn, CASE, DUE, TODAY)
    assert posted == [] and store.case_events(conn, CASE, "reminder_scheduled") == []


def test_a_date_that_has_passed_is_refused(conn, posted):
    store.record_consent(conn, CASE, reminders.CONSENT, True)
    with pytest.raises(n8n.NotReady, match="too close"):
        reminders.schedule(conn, CASE, DUE, DUE)
    assert posted == []


def test_scheduling_sends_only_what_the_workflow_needs_and_keeps_no_email(conn, posted):
    store.record_consent(conn, CASE, reminders.CONSENT, True)
    done = reminders.schedule(conn, CASE, DUE, TODAY)
    (call,) = posted
    assert call["url"] == config.N8N_REMINDER_URL and call["headers"] == HEADERS
    assert call["json"] == {
        "case_id": CASE, "reminder_id": done["reminder_id"], "email": "someone@example.com",
        "due_date": "2027-03-14", "remind_on": ["2027-03-07", "2027-03-13"],
        "callback_base": "https://praman.example",
    }
    (event,) = store.case_events(conn, CASE, "reminder_scheduled")
    assert "someone@example.com" not in str(event["detail"])


def test_n8n_being_down_leaves_nothing_scheduled(conn, monkeypatch):
    store.record_consent(conn, CASE, reminders.CONSENT, True)

    def down(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(n8n.DeliveryUnavailable):
        reminders.schedule(conn, CASE, DUE, TODAY)
    assert store.case_events(conn, CASE, "reminder_scheduled") == []


# --- When a reminder date arrives -----------------------------------------------------


def scheduled(conn, posted):
    store.record_consent(conn, CASE, reminders.CONSENT, True)
    return reminders.schedule(conn, CASE, DUE, TODAY)["reminder_id"]


def test_a_reminder_goes_out_once_and_says_may_be_due(conn, posted):
    reminder_id = scheduled(conn, posted)
    result = reminders.due(conn, CASE, reminder_id, "2027-03-13")
    assert result["send"] is True
    assert "may be due tomorrow" in result["subject"] and "14 Mar 2027" in result["message"]
    assert "overdue" not in result["message"].lower() and "paid" in result["message"]
    assert reminders.due(conn, CASE, reminder_id, "2027-03-13") == {"send": False, "reason": "already_sent"}
    assert reminders.due(conn, CASE, reminder_id, "2027-03-07")["send"] is True  # the other date still goes
    assert len(store.case_events(conn, CASE, "reminder_sent")) == 2


def test_a_date_that_was_not_planned_is_refused(conn, posted):
    reminder_id = scheduled(conn, posted)
    assert reminders.due(conn, CASE, reminder_id, "2027-03-10") == {"send": False, "reason": "not_planned"}
    assert reminders.due(conn, CASE, reminder_id, "not a date")["send"] is False


def test_a_cancelled_reminder_does_not_go_out(conn, posted):
    reminder_id = scheduled(conn, posted)
    assert reminders.cancel(conn, CASE) is True
    assert reminders.due(conn, CASE, reminder_id, "2027-03-13") == {"send": False, "reason": "cancelled"}
    assert reminders.cancel(conn, CASE) is False  # nothing left to cancel


def test_a_newer_reminder_replaces_the_old_one(conn, posted):
    old = scheduled(conn, posted)
    reminders.schedule(conn, CASE, date(2027, 4, 20), TODAY)
    assert reminders.due(conn, CASE, old, "2027-03-13") == {"send": False, "reason": "replaced"}


def test_withdrawn_consent_stops_it(conn, posted):
    reminder_id = scheduled(conn, posted)
    store.record_consent(conn, CASE, reminders.CONSENT, False)
    assert reminders.due(conn, CASE, reminder_id, "2027-03-13") == {"send": False, "reason": "consent_withdrawn"}


def test_a_deleted_case_stops_it(conn, posted):
    reminder_id = scheduled(conn, posted)
    store.delete_case(conn, CASE)
    assert reminders.due(conn, CASE, reminder_id, "2027-03-13") == {"send": False, "reason": "case_deleted"}


def test_the_callback_needs_the_secret(conn, posted):
    reminder_id = scheduled(conn, posted)
    body = {"case_id": CASE, "reminder_id": reminder_id, "on": "2027-03-13"}
    assert client.post("/api/n8n/reminder-due", json=body).status_code == 401
    assert client.post("/api/n8n/reminder-due", json=body, headers={"x-praman-secret": "wrong"}).status_code == 401
    assert client.post("/api/n8n/reminder-due", json={"case_id": CASE}, headers=HEADERS).status_code == 400
    ok = client.post("/api/n8n/reminder-due", json=body, headers=HEADERS)
    assert ok.status_code == 200 and ok.json()["send"] is True


# --- In the chat -----------------------------------------------------------------------


def test_asking_for_a_reminder_asks_for_the_date_when_the_policy_has_none(conn, posted):
    reply = say(conn, "remind me to pay my premium")
    assert "What date" in reply and posted == []


def test_a_date_is_confirmed_with_her_before_anything_is_set(conn, posted):
    say(conn, "remind me to pay my premium")
    reply = say(conn, "14/03/2027")
    assert "14 Mar 2027" in reply and "s***@example.com" in reply and "Reply YES" in reply
    assert "someone@example.com" not in reply and posted == []
    assert not store.has_consent(conn, chat_case(conn), reminders.CONSENT)


def test_yes_records_consent_and_schedules(conn, posted, monkeypatch):
    monkeypatch.setattr(reminders, "plan", lambda due, today, offsets=None: [date(2027, 3, 13)])
    say(conn, "remind me to pay my premium")
    say(conn, "14/03/2027")
    reply = say(conn, "yes")
    assert "Done" in reply and "13/03" in reply
    assert store.has_consent(conn, chat_case(conn), reminders.CONSENT) and len(posted) == 1
    assert CASE not in conversation._REMINDER_FLOW


def test_another_date_changes_it_and_cancel_drops_it(conn, posted):
    say(conn, "remind me to pay my premium")
    say(conn, "14/03/2027")
    assert "20 Apr 2027" in say(conn, "no, it is 20 April 2027")
    assert "have not set" in say(conn, "cancel")
    assert posted == [] and not store.has_consent(conn, chat_case(conn), reminders.CONSENT)


def test_a_message_without_a_date_is_asked_again(conn, posted):
    say(conn, "remind me to pay my premium")
    assert "did not catch a date" in say(conn, "soon")


def test_stop_reminders_cancels_and_says_so(conn, posted, monkeypatch):
    monkeypatch.setattr(reminders, "plan", lambda due, today, offsets=None: [date(2027, 3, 13)])
    say(conn, "remind me to pay my premium")
    say(conn, "14/03/2027")
    say(conn, "yes")
    assert "will not email you" in say(conn, "stop reminders")
    assert reminders.current(conn, chat_case(conn)) is None
    assert "no premium reminders" in say(conn, "stop reminders")


def test_the_chat_says_so_when_reminders_are_not_set_up(conn, posted, monkeypatch):
    monkeypatch.setattr(config, "REMINDER_EMAIL", "")
    assert "not set up" in say(conn, "remind me to pay my premium")
    assert posted == []


def test_delete_everything_forgets_a_reminder_in_progress(conn, posted):
    say(conn, "remind me to pay my premium")
    assert CASE in conversation._REMINDER_FLOW
    say(conn, "delete everything")
    assert CASE not in conversation._REMINDER_FLOW
