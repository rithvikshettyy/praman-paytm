"""'Did this solve it?' and the agent's brief: every number from events, nothing identifying in the brief."""

import pytest
from fastapi.testclient import TestClient

from app import config, console, conversation, handoff, store
from app.clients import sarvam
from app.main import app
from app.rag.answer import Answer, Citation
from app.services import i18n, voice
from scripts import seed_demo

client = TestClient(app)
SESSION = "handoff-test-0001"
CITE = Citation("Example General Insurance", "policy_wording", 1, "", "UNVERIFIED")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(config, "DISTRIBUTOR_LEGAL_NAME", "Example Broking Pvt Ltd")
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: None)
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: f"[{target}] {text}")
    monkeypatch.setattr(sarvam, "identify_language", lambda text: {})
    i18n._CACHE.clear()
    voice._MEDIA.clear()
    yield
    i18n._CACHE.clear()


def classify_as(monkeypatch, **payload):
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: payload)


def answered(text="Room rent is limited to 1% of the sum insured per day."):
    return Answer("answered", f"{text} {CITE.label}", f"{text} {CITE.label}", (CITE,), True, None, "en-IN", False)


def not_found():
    return Answer("no_source", "I could not find this.", "I could not find this.", (), False,
                  {"respondent": "insurer"}, "en-IN", False)


def chat(text, **extra):
    return client.post("/api/chat", json={"session_id": SESSION, "text": text, "language": "en-IN", **extra}).json()


def ask_a_question(monkeypatch, result, text="what is the room rent limit?"):
    classify_as(monkeypatch, intent="question", grievance_class=None, product="health_policy")
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: result)
    return chat(text, insurer="Example General Insurance Company Ltd", product="health_policy")["messages"][0]


def send_feedback(answer_id, solved, **extra):
    return client.post("/api/feedback", json={"session_id": SESSION, "answer_id": answer_id, "solved": solved, **extra})


def counters():
    conn = store.connect()
    try:
        return console.metrics(conn)
    finally:
        conn.close()


def case_id():
    return client.post("/api/session", json={"session_id": SESSION}).json()["case_id"]


# --- The question under each answer ---------------------------------------------------------


def test_an_answer_carries_the_question_to_ask(monkeypatch):
    message = ask_a_question(monkeypatch, answered())
    assert message["feedback"]["kind"] == "answer" and isinstance(message["feedback"]["answer_id"], int)


def test_a_not_found_reply_offers_a_person(monkeypatch):
    message = ask_a_question(monkeypatch, not_found())
    assert message["feedback"]["kind"] == "not_found"


@pytest.mark.parametrize("text", ["hi", "thanks"])
def test_greetings_are_not_rated(text):
    assert chat(text)["messages"][0]["feedback"] is None


def test_a_routed_grievance_is_not_rated_but_what_she_wrote_is_kept(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="platform/payment_failed", product="health_policy")
    message = chat("my premium was debited twice")["messages"][0]
    assert message["feedback"] is None
    conn = store.connect()
    try:
        [event] = store.case_events(conn, case_id(), "grievance_reported")
        assert event["detail"]["text"] == "my premium was debited twice"
    finally:
        conn.close()


# --- Yes and No ------------------------------------------------------------------------------


def test_yes_counts_as_solved_on_the_console_and_no_agent_is_needed(monkeypatch):
    message = ask_a_question(monkeypatch, answered())
    body = send_feedback(message["feedback"]["answer_id"], True).json()
    assert body["solved"] is True and body["messages"][0]["text"] == conversation.FEEDBACK_SOLVED
    c = counters()["counters"]
    assert (c["answers_given"], c["answers_confirmed_solved"], c["asked_for_a_person"]) == (1, 1, 0)
    assert counters()["headline"]["needed_distributor"] == 0


def test_an_answer_nobody_rated_is_not_counted_as_solved(monkeypatch):
    ask_a_question(monkeypatch, answered())
    c = counters()["counters"]
    assert (c["answers_given"], c["answers_confirmed_solved"]) == (1, 0)


def test_no_asks_for_a_person_and_the_case_needs_the_distributor(monkeypatch):
    message = ask_a_question(monkeypatch, answered())
    body = send_feedback(message["feedback"]["answer_id"], False).json()
    assert body["solved"] is False and body["messages"][0]["text"] == conversation.FEEDBACK_PERSON
    assert "Nothing has been sent" in body["messages"][0]["text"]
    assert counters()["counters"]["asked_for_a_person"] == 1
    assert counters()["headline"] == {"cases": 1, "needed_distributor": 1, "distributor": config.DISTRIBUTOR_SHORT_NAME,
                                      "text": f"Of 1 case, 1 needed {config.DISTRIBUTOR_SHORT_NAME}."}
    conn = store.connect()
    try:
        [row] = console.case_list(conn, "needs_paytm")
        assert row["asked_for_person"] is True and row["needs_distributor"] is True
        assert console.case_list(conn, "routed_away") == []
    finally:
        conn.close()


def test_asking_for_a_person_after_a_not_found_reply_works_too(monkeypatch):
    message = ask_a_question(monkeypatch, not_found())
    send_feedback(message["feedback"]["answer_id"], False)
    conn = store.connect()
    try:
        [event] = store.case_events(conn, case_id(), "agent_requested")
        assert event["detail"]["reason"] == "not_found"
    finally:
        conn.close()


def test_the_first_reply_to_each_answer_stands(monkeypatch):
    message = ask_a_question(monkeypatch, answered())
    answer_id = message["feedback"]["answer_id"]
    send_feedback(answer_id, True)
    again = send_feedback(answer_id, False).json()
    assert again["solved"] is True  # the second tap changes nothing
    c = counters()["counters"]
    assert (c["answers_confirmed_solved"], c["asked_for_a_person"]) == (1, 0)


@pytest.mark.parametrize("body", [
    {"session_id": SESSION, "answer_id": "1", "solved": True},
    {"session_id": SESSION, "answer_id": 1, "solved": "yes"},
    {"session_id": SESSION, "answer_id": True, "solved": True},
    {"session_id": SESSION, "solved": True},
])
def test_bad_feedback_is_refused(body):
    assert client.post("/api/feedback", json=body).status_code == 400


def test_a_bad_session_is_refused():
    assert client.post("/api/feedback", json={"session_id": "../x", "answer_id": 1, "solved": True}).status_code == 400


def test_feedback_only_for_an_answer_she_was_given(monkeypatch):
    message = ask_a_question(monkeypatch, answered())
    other = client.post("/api/feedback", json={"session_id": "someone-else-0002", "answer_id": message["feedback"]["answer_id"], "solved": True})
    assert other.status_code == 404
    assert send_feedback(99999, True).status_code == 404
    assert counters()["counters"]["answers_confirmed_solved"] == 0


# --- The brief ----------------------------------------------------------------------------------


def brief_of(cid):
    return client.get(f"/api/case/{cid}/brief")


def test_the_brief_for_a_case_that_asked_for_a_person(monkeypatch):
    classify_as(monkeypatch, intent="question", grievance_class=None, product="health_policy")
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: answered())
    message = chat("what is the room rent limit?")["messages"][0]
    send_feedback(message["feedback"]["answer_id"], False)
    body = brief_of(case_id()).json()
    assert body["why_here"]["reason"] == "asked_for_person"
    [turn] = body["conversation"]
    assert turn["question"] == "what is the room rent limit?" and turn["answered"] is True
    assert turn["answer_en"] == "Room rent is limited to 1% of the sum insured per day." and turn["pages"] == [1]
    assert turn["solved"] is False
    assert body["problem"] == "Question about her policy"  # nothing was routed: she only asked
    assert body["language"] == {"code": "en-IN", "name": "English"}
    assert "she said it did not solve it" in body["text"] and body["text"].startswith("CASE BRIEF")


def test_the_brief_for_a_distributor_case_has_her_words_and_the_first_step(monkeypatch):
    classify_as(monkeypatch, intent="grievance", grievance_class="platform/payment_failed", product="health_policy")
    chat("my premium was debited twice")
    body = brief_of(case_id()).json()
    assert body["why_here"]["reason"] == "distributor_owned"
    assert body["she_wrote"][0]["text"] == "my premium was debited twice"
    assert body["respondent"] == "Example Broking Pvt Ltd"
    assert "First step on the ladder: Customer support" in body["next_step"]


def test_her_words_are_shown_beside_their_english(monkeypatch):
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: "My premium was debited twice")
    classify_as(monkeypatch, intent="grievance", grievance_class="platform/payment_failed", product="health_policy")
    client.post("/api/chat", json={"session_id": SESSION, "text": "माझा प्रीमियम दोनदा कापला गेला", "language": "mr-IN"})
    body = brief_of(case_id()).json()
    assert body["she_wrote"] == [{"text": "माझा प्रीमियम दोनदा कापला गेला", "text_en": "My premium was debited twice"}]
    assert body["language"]["name"] == "Marathi"
    assert '(in English: "My premium was debited twice")' in body["text"]


def test_a_section_with_nothing_recorded_says_so_by_being_empty():
    body = brief_of(case_id()).json()
    assert body["conversation"] == [] and body["she_wrote"] == [] and body["facts"] == []
    assert body["documents"] == {"shared_in_chat": False, "claim_documents": [], "claim_documents_missing": [], "read": []}
    assert body["why_here"]["reason"] == "not_routed"


def test_an_unknown_case_has_no_brief():
    assert client.get("/api/case/nope/brief").status_code == 404


def test_the_brief_never_holds_her_identity_or_identifiers(monkeypatch):
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: text)
    message = ask_a_question(monkeypatch, answered("Your number is 1234 5678 9012 and PAN ABCDE1234F."),
                             "my aadhaar is 1234 5678 9012 and PAN ABCDE1234F, what is the limit?")
    send_feedback(message["feedback"]["answer_id"], False)
    body = brief_of(case_id()).json()
    for leaked in ("1234 5678 9012", "ABCDE1234F", SESSION, "web:"):
        assert leaked not in body["text"] and leaked not in str(body)
    assert "whatsapp" not in body["text"].lower()


def test_delete_everything_removes_the_answers_the_feedback_and_the_counts(monkeypatch):
    message = ask_a_question(monkeypatch, answered())
    send_feedback(message["feedback"]["answer_id"], False)
    cid = case_id()
    assert counters()["counters"]["answers_given"] == 1
    assert chat("delete everything")["messages"][0]["text"] == conversation.DELETED
    c = counters()["counters"]
    assert (c["answers_given"], c["answers_confirmed_solved"], c["asked_for_a_person"]) == (0, 0, 0)
    assert brief_of(cid).status_code == 404


# --- The demo ---------------------------------------------------------------------------------------


def test_the_demo_seed_shows_a_solved_answer_and_briefs_for_every_case():
    conn = store.connect()
    try:
        seed_demo.seed(conn)
        c = console.metrics(conn)["counters"]
        assert (c["answers_given"], c["answers_confirmed_solved"], c["asked_for_a_person"]) == (1, 1, 0)
        for case in seed_demo.EXAMPLES:
            assert handoff.brief(conn, case)["example"] is True
        assert handoff.brief(conn, seed_demo.DOUBLE_DEBIT)["she_wrote"][0]["text"] == "My premium was debited twice."
    finally:
        conn.close()


def test_a_policy_shared_in_the_chat_is_noted_and_its_kind_is_known(monkeypatch):
    from app.rag import mine

    mine._DOCS.clear()
    cid = case_id()
    mine.add(cid, "policy.pdf", [(1, "Two Wheeler Package Policy")], "motor_policy")
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: answered("Your IDV is Rs 1,45,000."))
    classify_as(monkeypatch, intent="question", grievance_class=None, product="motor_policy")
    chat("what is my IDV?")
    body = brief_of(cid).json()
    mine._DOCS.clear()
    assert body["product"] == "Motor insurance (bike, car)"
    assert body["documents"]["shared_in_chat"] is True and "the file is not kept" in body["text"]
    assert "policy.pdf" not in str(body)  # not even her file's name


# --- Resolved or pending, marked by an agent ----------------------------------------------------


def mark(cid, status):
    return client.post(f"/api/console/cases/{cid}/status", json={"status": status})


def listed(filter=None):
    url = "/api/console/cases" + (f"?filter={filter}" if filter else "")
    return [row["case_id"] for row in client.get(url).json()["cases"]]


def a_case_that_asked_for_a_person(monkeypatch):
    message = ask_a_question(monkeypatch, answered())
    send_feedback(message["feedback"]["answer_id"], False)
    return case_id()


def test_a_resolved_case_leaves_the_list_but_is_not_deleted(monkeypatch):
    cid = a_case_that_asked_for_a_person(monkeypatch)
    assert listed() == [cid] and listed("needs_paytm") == [cid]
    assert mark(cid, "resolved").json() == {"case_id": cid, "status": "resolved"}
    assert listed() == [] and listed("needs_paytm") == [] and listed("routed_away") == []
    assert listed("resolved") == [cid]
    assert brief_of(cid).status_code == 200  # the case and its brief are still there
    assert counters()["headline"]["cases"] == 1  # every number still counts it
    assert counters()["counters"]["cases_marked_resolved"] == 1


def test_pending_brings_a_resolved_case_back(monkeypatch):
    cid = a_case_that_asked_for_a_person(monkeypatch)
    mark(cid, "resolved")
    mark(cid, "pending")
    assert listed() == [cid] and listed("resolved") == []
    assert counters()["counters"]["cases_marked_resolved"] == 0


def test_the_latest_mark_stands_and_each_row_says_its_status(monkeypatch):
    cid = a_case_that_asked_for_a_person(monkeypatch)
    assert client.get("/api/console/cases").json()["cases"][0]["status"] == "pending"
    mark(cid, "resolved")
    mark(cid, "resolved")
    assert client.get("/api/console/cases?filter=resolved").json()["cases"][0]["status"] == "resolved"
    assert counters()["counters"]["cases_marked_resolved"] == 1  # the case, not the clicks


def test_only_resolved_and_pending_are_accepted_for_a_real_case(monkeypatch):
    cid = a_case_that_asked_for_a_person(monkeypatch)
    for bad in ("done", "", None, 1):
        assert mark(cid, bad).status_code == 400
    assert client.post(f"/api/console/cases/{cid}/status", json={}).status_code == 400
    assert mark("no-such-case", "resolved").status_code == 404
    assert listed() == [cid]


def test_a_resolved_case_still_goes_when_she_deletes_everything(monkeypatch):
    cid = a_case_that_asked_for_a_person(monkeypatch)
    mark(cid, "resolved")
    chat("delete everything")
    assert listed("resolved") == [] and counters()["counters"]["cases_marked_resolved"] == 0
