"""The web chat's start options: find a policy (questions, parents), check my policy, complaint (registered
for a person, shown on the console). Sarvam and the web search are faked."""

import pytest
from fastapi.testclient import TestClient

from app import complaints, config, conversation, guided, store
from app.clients import sarvam
from app.main import app
from app.services import i18n, policy_search

client = TestClient(app)
SESSION = "9d1c7e52-aaaa-4bbb-8ccc-0123456789ab"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: None)
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: text)
    monkeypatch.setattr(sarvam, "identify_language", lambda text: {})
    i18n._CACHE.clear()
    guided._FIND.clear()
    guided._COMPLAINT.clear()
    conversation._AWAITING_CONSENT.clear()
    yield
    i18n._CACHE.clear()


@pytest.fixture
def searched(monkeypatch):
    calls = []

    def fake(kind, requirements=None, language="en-IN"):
        calls.append({"kind": kind, "needs": requirements})
        return "Options found online, in no order:\n1. Example Plan (Example Insurer) [example.com]"

    monkeypatch.setattr(policy_search, "suggest", fake)
    return calls


def classify_as(monkeypatch, **payload):
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: payload)


def journey(name):
    return client.post("/api/journey", json={"session_id": SESSION, "journey": name, "language": "en-IN"})


def say(text):
    response = client.post("/api/chat", json={"session_id": SESSION, "text": text, "language": "en-IN"})
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def texts(messages):
    return " ".join(m["text"] for m in messages)


def case_id():
    return client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]


def conn():
    return store.connect()


# --- The start options ---------------------------------------------------------------


def test_a_bad_journey_is_refused():
    assert journey("nonsense").status_code == 400
    assert client.post("/api/journey", json={"session_id": "x", "journey": "find"}).status_code == 400


def test_picking_find_offers_the_three_kinds_and_remembers_the_pick():
    body = journey("find").json()
    [message] = body["messages"]
    assert [b["id"] for b in message["buttons"]] == ["find:health", "find:life", "find:motor"]
    assert "parents" in message["text"]
    c = conn()
    assert store.latest_event(c, case_id(), "journey_chosen")["detail"] == {"journey": "find"}


def test_check_and_complaint_open_with_what_to_do():
    assert "photo or PDF" in texts(journey("check").json()["messages"])
    assert "what went wrong" in texts(journey("complain").json()["messages"])


# --- Find a policy ---------------------------------------------------------------------


def test_finding_health_cover_for_parents_asks_the_questions_and_searches_only_the_kind_of_cover(searched):
    journey("find")
    assert "Who is this insurance for" in texts(say("find:health"))
    assert "each of your parents" in texts(say("who:parents"))
    assert "illness" in texts(say("62 and 58"))
    assert "How much cover" in texts(say("illness:yes"))
    assert "yearly premium" in texts(say("cover:10 lakh"))
    final = say("skip")

    [call] = searched
    assert call["kind"] == "health"
    assert "senior citizens" in call["needs"] and "pre-existing disease cover" in call["needs"]
    assert "10 lakh" in call["needs"]
    assert "62" not in call["needs"] and "58" not in call["needs"]  # ages never go out

    assert final[0]["unverified"] is True and "Example Plan" in final[0]["text"]
    checks = final[1]["text"]
    assert "entry-age limit" in checks and "waiting period before an illness you already have" in checks
    assert "senior citizens" in checks
    assert "I do not rank insurers" in checks
    assert "approved" in checks and "will be approved" not in checks.replace("cannot say what you will be approved", "")


def test_the_answers_are_not_stored(searched):
    journey("find")
    for reply in ("find:health", "who:self", "40", "illness:yes", "cover:5 lakh", "skip"):
        say(reply)
    c = conn()
    kinds = {e["kind"] for e in c.db.events.find({"case_id": case_id()})}
    assert kinds == {"journey_chosen", "find_kind"}
    assert guided._FIND == {}


def test_a_search_that_finds_nothing_still_gives_what_to_check(monkeypatch):
    monkeypatch.setattr(policy_search, "suggest", lambda *a, **k: None)
    journey("find")
    for reply in ("find:motor", "vehicle:car", "vage:new", "ctype:not sure"):
        messages = say(reply)
    assert "could not look online" in messages[0]["text"] and "garages" in messages[1]["text"]


def test_life_cover_asks_for_the_amount_and_term(searched):
    journey("find")
    for reply in ("find:life", "goal:savings", "35", "50 lakh"):
        say(reply)
    assert "Send the number of years" in texts(say("a long time"))
    say("20")
    say("skip")
    [call] = searched
    assert call["kind"] == "life" and "maturity" in call["needs"]
    assert "50 lakh" in call["needs"] and "20 year term" in call["needs"] and "35" not in call["needs"]


def test_a_bad_answer_is_asked_again_not_guessed(searched):
    journey("find")
    assert "Which insurance" in texts(say("banana"))
    say("health")
    assert "pick one" in texts(say("purple")).lower()
    say("myself")
    assert "did not see an age" in texts(say("old"))
    assert searched == []


def test_a_word_inside_another_word_is_not_an_answer(searched):
    journey("find")
    say("find:health")
    say("who:self")
    say("40")
    # "know" contains "no" but is not the answer "No"
    assert "pick one" in texts(say("I do not know")).lower()


def test_cancel_stops_the_questions(searched):
    journey("find")
    say("find:health")
    assert "stopped the questions" in texts(say("cancel"))
    assert guided._FIND == {}


def test_delete_everything_forgets_the_questions(searched):
    journey("find")
    say("find:health")
    say("delete everything")
    assert guided._FIND == {}


# --- Complaint -------------------------------------------------------------------------


def register_one(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    journey("complain")
    first = say("my claim was rejected after six years")
    say("talk")
    say("9876543210")
    return first, say("yes")


def test_the_answer_comes_with_a_way_to_reach_a_person(monkeypatch):
    first, _ = register_one(monkeypatch)
    assert first[-1]["buttons"] == [{"id": "talk", "title": "Talk to someone"}]


def test_a_complaint_is_registered_with_her_contact_and_shows_on_the_console(monkeypatch):
    _, done = register_one(monkeypatch)
    assert "registered" in done[0]["text"] and "C-" in done[0]["text"]
    assert "+91******3210" in done[0]["text"] and "9876543210" not in done[0]["text"]
    assert "Nothing has been sent to your insurer" in done[0]["text"]

    [item] = client.get("/api/console/complaints").json()["complaints"]
    assert item["status"] == "pending" and item["contact"] == "+919876543210"
    assert item["grievance_class"] == "insurance/claim_denied" and item["product"] == "health_policy"
    assert "claim was rejected" in item["text"] and item["case_id"] == case_id()
    assert store.has_consent(conn(), case_id(), complaints.CONSENT)


def test_nothing_is_registered_until_she_says_yes(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    journey("complain")
    say("my claim was rejected")
    say("talk")
    confirm = say("9876543210")
    assert "Reply YES" in confirm[0]["text"]
    assert client.get("/api/console/complaints").json()["complaints"] == []
    assert "have not registered" in texts(say("no"))
    assert client.get("/api/console/complaints").json()["complaints"] == []
    assert not store.has_consent(conn(), case_id(), complaints.CONSENT)


def test_asking_for_a_person_first_asks_what_went_wrong(monkeypatch):
    journey("complain")
    assert "tell me in a line what went wrong" in texts(say("I want to talk to someone"))
    assert "phone number or email" in texts(say("the insurer keeps delaying my claim payment"))


def test_the_policy_number_is_kept_as_its_last_four_only(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    journey("complain")
    assert "ending 6789" in texts(say("my policy number is HDF123456789"))
    say("claim rejected for no reason")
    say("talk")
    say("a@b.in")
    say("yes")
    [item] = client.get("/api/console/complaints").json()["complaints"]
    assert item["policy_last4"] == "6789"
    assert "123456789" not in str(item)


def test_she_can_register_without_giving_contact_details(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    journey("complain")
    say("claim rejected for no reason")
    say("talk")
    assert "no contact given" in texts(say("skip"))
    say("yes")
    assert client.get("/api/console/complaints").json()["complaints"][0]["contact"] is None


def test_a_second_request_does_not_register_a_second_complaint(monkeypatch):
    register_one(monkeypatch)
    assert "already registered" in texts(say("talk"))
    assert len(client.get("/api/console/complaints?status=all").json()["complaints"]) == 1


def test_a_no_to_did_this_solve_it_starts_a_complaint_in_that_journey(monkeypatch):
    journey("complain")
    say("claim was rejected without a reason")
    c = conn()
    answer_id = store.record_event(c, case_id(), "answer_given", {"status": "answered", "question": "q", "pages": []})
    response = client.post("/api/feedback", json={"session_id": SESSION, "answer_id": answer_id, "solved": False}).json()
    assert response["solved"] is False
    assert "phone number or email" in texts(response["messages"])


def test_a_no_outside_the_complaint_journey_is_unchanged(monkeypatch):
    say("hello")
    c = conn()
    answer_id = store.record_event(c, case_id(), "answer_given", {"status": "answered", "question": "q", "pages": []})
    response = client.post("/api/feedback", json={"session_id": SESSION, "answer_id": answer_id, "solved": False}).json()
    assert "support team" in texts(response["messages"])
    assert complaints.listing(conn(), None) == []


# --- The console -----------------------------------------------------------------------


def test_an_agent_resolves_a_complaint_and_it_moves_tabs(monkeypatch):
    register_one(monkeypatch)
    [item] = client.get("/api/console/complaints").json()["complaints"]
    done = client.post(f"/api/console/complaints/{item['id']}/status", json={"status": "resolved"})
    assert done.status_code == 200
    assert client.get("/api/console/complaints").json()["complaints"] == []
    [resolved] = client.get("/api/console/complaints?status=resolved").json()["complaints"]
    assert resolved["id"] == item["id"] and resolved["status"] == "resolved"
    client.post(f"/api/console/complaints/{item['id']}/status", json={"status": "pending"})
    assert len(client.get("/api/console/complaints").json()["complaints"]) == 1


def test_the_counters_count_complaints(monkeypatch):
    register_one(monkeypatch)
    counters = client.get("/api/metrics").json()["counters"]
    assert counters["complaints_registered"] == 1 and counters["complaints_pending"] == 1
    [item] = client.get("/api/console/complaints").json()["complaints"]
    client.post(f"/api/console/complaints/{item['id']}/status", json={"status": "resolved"})
    counters = client.get("/api/metrics").json()["counters"]
    assert counters["complaints_registered"] == 1 and counters["complaints_pending"] == 0


def test_bad_console_requests_are_refused():
    assert client.get("/api/console/complaints?status=weird").status_code == 400
    assert client.post("/api/console/complaints/1/status", json={"status": "weird"}).status_code == 400
    assert client.post("/api/console/complaints/999/status", json={"status": "resolved"}).status_code == 404


def test_delete_everything_removes_the_complaint(monkeypatch):
    register_one(monkeypatch)
    say("delete everything")
    assert client.get("/api/console/complaints?status=all").json()["complaints"] == []


def test_the_agent_brief_does_not_carry_her_contact(monkeypatch):
    register_one(monkeypatch)
    brief = client.get(f"/api/case/{case_id()}/brief").text
    assert "9876543210" not in brief and "+91" not in brief


# --- Reading what she types ------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("call me on 98765 43210", None),  # a space inside a number is not a number we trust
    ("9876543210", "+919876543210"),
    ("+91 9876543210", "+919876543210"),
    ("my mail is Me.Name@Example.com please", "me.name@example.com"),
    ("12345", None),
    ("", None),
])
def test_contact_is_read_from_text(text, expected):
    assert complaints.parse_contact(text) == expected


def test_contacts_are_masked_for_showing_back():
    assert complaints.mask_contact("+919876543210") == "+91******3210"
    assert complaints.mask_contact("meera@gmail.com") == "m***@gmail.com"
    assert complaints.mask_contact(None) == "no contact given"


@pytest.mark.parametrize("text, last4", [
    ("my policy number is HDF123456789", "6789"),
    ("policy no: 20-0456-7890", "7890"),
    ("HDFC/2023/998877 is the number", "8877"),
    ("my claim was rejected", None),
    ("I paid 5000 rupees", None),
])
def test_only_the_last_four_of_a_policy_number_are_read(text, last4):
    assert complaints.policy_last4(text) == last4


def test_registering_needs_her_consent():
    c = conn()
    store.ensure_case(c, "c1")
    with pytest.raises(PermissionError):
        complaints.register(c, "c1", "text", None)
    store.record_consent(c, "c1", complaints.CONSENT, True)
    assert complaints.register(c, "c1", "My PAN is ABCDE1234F", None) > 0
    assert "ABCDE1234F" not in complaints.listing(c, None)[0]["text"]
