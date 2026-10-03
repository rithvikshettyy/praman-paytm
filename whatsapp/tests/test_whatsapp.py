"""WhatsApp over Meta's Cloud API: webhook, parsing, and the shared conversation."""

import base64
import hashlib
import hmac
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, conversation, store
from app.clients import sarvam
from app.main import app
from app.rag.answer import Answer, Citation
from app.services import documents, i18n
from whatsapp import channel, meta

NUMBER = "919000000000"
USER = f"whatsapp:+{NUMBER}"
SECRET = "app-secret"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16
POLICY = [(1, "HEALTH INSURANCE POLICY SCHEDULE. Sum insured Rs 5,00,000.")]
client = TestClient(app)


def fake_ask(question, **kwargs):
    cite = Citation("Your document", "policy.pdf", 1, "", "user")
    text = f"A health policy with a sum insured of Rs 5,00,000 {cite.label}."
    return Answer("answered", text, text, (cite,), False, None, "en-IN", False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setenv("WA_APP_SECRET", SECRET)
    monkeypatch.setenv("WA_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WA_TOKEN", "token")
    monkeypatch.setenv("WA_PHONE_NUMBER_ID", "123")
    i18n._CACHE.clear()
    channel._SEEN.clear()
    channel._USER_LOCKS.clear()
    conversation._AWAITING_CONSENT.clear()

    out = SimpleNamespace(texts=[], buttons=[], audio=[], uploads=[], media={"m1": PNG}, spoken=[], typing=[], lists=[])
    monkeypatch.setattr(meta, "send_text", lambda to, body: out.texts.append((to, body)) or True)
    monkeypatch.setattr(meta, "send_buttons", lambda to, body, buttons: out.buttons.append((to, body, buttons)) or True)
    monkeypatch.setattr(
        meta, "send_list", lambda to, body, button, rows: out.lists.append((to, body, button, rows)) or True
    )
    monkeypatch.setattr(meta, "send_audio", lambda to, media_id: out.audio.append((to, media_id)) or True)
    monkeypatch.setattr(meta, "send_typing", lambda message_id: out.typing.append(message_id) or True)
    monkeypatch.setattr(meta, "upload_media", lambda data, name, mime: out.uploads.append((name, mime)) or "up1")
    monkeypatch.setattr(meta, "download_media", lambda media_id: out.media[media_id])

    def tts(text, *, language, speaker=None, codec=None):
        out.spoken.append((text, language, codec))
        return {"audios": [base64.b64encode(b"voice").decode()]}

    monkeypatch.setattr(sarvam, "text_to_speech", tts)
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: f"[{target}] {text}")
    monkeypatch.setattr(sarvam, "identify_language", lambda text: {})
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: None)
    monkeypatch.setattr(sarvam, "speech_to_text", lambda *a, **k: {"transcript": "namaste praman"})
    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: POLICY)
    monkeypatch.setattr(conversation, "_ask", fake_ask)
    yield out
    i18n._CACHE.clear()


def consent(user=USER):
    conn = store.connect()
    store.record_consent(conn, store.case_for_user(conn, user)["id"], "read_documents", True)
    conn.close()


def onboarded(language="en-IN", user=USER):
    """A sender who already chose a language, so the test starts at the conversation."""
    conn = store.connect()
    store.set_language(conn, store.case_for_user(conn, user)["id"], language)
    conn.close()


def payload(*messages):
    return {"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}


def text_msg(body, mid="wamid.1", sender=NUMBER):
    return {"from": sender, "id": mid, "type": "text", "text": {"body": body}}


def media_msg(kind, mid="wamid.2", mime="image/png", caption=None, media_id="m1", sender=NUMBER):
    body = {"id": media_id, "mime_type": mime}
    if caption:
        body["caption"] = caption
    return {"from": sender, "id": mid, "type": kind, kind: body}


def post(body, secret=SECRET, signature=None):
    raw = json.dumps(body).encode()
    sig = signature or "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/whatsapp/webhook", content=raw, headers={"x-hub-signature-256": sig})


# --- Handshake and signature ---------------------------------------------------


def test_handshake_echoes_the_challenge(env):
    params = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}
    ok = client.get("/api/whatsapp/webhook", params=params)
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/api/whatsapp/webhook", params={**params, "hub.verify_token": "nope"})
    assert bad.status_code == 403


def test_a_bad_or_missing_signature_is_refused(env):
    assert post(payload(text_msg("hi")), signature="sha256=forged").status_code == 401
    assert client.post("/api/whatsapp/webhook", json=payload(text_msg("hi"))).status_code == 401
    assert env.texts == [] and env.buttons == []


def test_no_app_secret_refuses_everything(env, monkeypatch):
    monkeypatch.delenv("WA_APP_SECRET")
    assert post(payload(text_msg("hi")), secret="").status_code == 401


def test_a_good_signature_is_acknowledged_and_answered(env):
    assert post(payload(text_msg("hello"))).status_code == 200
    assert env.typing == ["wamid.1"]
    assert [to for to, *_ in env.lists] == [NUMBER]


def test_a_message_delivered_twice_is_answered_once(env):
    post(payload(text_msg("hello", mid="dup")))
    post(payload(text_msg("hello", mid="dup")))
    assert len(env.lists) == 1


def test_status_callbacks_are_ignored(env):
    body = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}
    assert post(body).status_code == 200
    assert env.texts == [] and env.typing == []


# --- Parsing -------------------------------------------------------------------


def test_parse_inbound_shapes():
    assert meta.parse_inbound(text_msg(" hi ")).text == "hi"
    assert meta.parse_inbound(text_msg("hi")).user == USER
    doc = meta.parse_inbound(media_msg("document", mime="application/pdf; charset=x", caption="bill"))
    assert (doc.kind, doc.mime, doc.text, doc.media_id) == ("document", "application/pdf", "bill", "m1")
    button = {"from": NUMBER, "id": "b", "type": "interactive",
              "interactive": {"type": "button_reply", "button_reply": {"id": "journey:find", "title": "Find a policy"}}}
    assert meta.parse_inbound(button).text == "journey:find"
    assert meta.parse_inbound({"from": NUMBER, "id": "s", "type": "sticker"}).kind == "unsupported"


def test_signature_check_rejects_empty_inputs():
    assert not meta.verify_signature(b"", "sha256=x", SECRET)
    assert not meta.verify_signature(b"x", None, SECRET)
    assert not meta.verify_signature(b"x", "sha256=x", "")


# --- The conversation ----------------------------------------------------------


def test_text_gets_a_reply_and_no_voice_note(env):
    onboarded()
    post(payload(text_msg("hello")))
    assert env.texts and env.audio == []


def test_first_photo_asks_consent_with_buttons_and_reads_nothing(env, monkeypatch):
    onboarded()
    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: pytest.fail("read before consent"))
    post(payload(media_msg("image")))
    [(to, _, buttons)] = env.buttons
    assert to == NUMBER and buttons == [("YES", "YES"), ("NO", "NO")]
    assert env.texts == []


def test_yes_reads_the_waiting_photo_and_summarises_with_citations(env):
    onboarded()
    post(payload(media_msg("image", mid="a")))
    post(payload(text_msg("YES", mid="b")))
    bodies = [body for _, body in env.texts]
    assert any("sum insured of Rs 5,00,000" in b and "[Your document, policy.pdf, p.1]" in b for b in bodies)
    assert conversation.READ_IT in bodies


def test_with_consent_a_reading_notice_comes_first(env):
    onboarded()
    consent()
    post(payload(media_msg("document", mime="application/pdf", caption="what is covered?")))
    assert env.texts[0][1] == channel.READING


def test_a_file_that_is_not_a_photo_or_pdf_is_turned_away(env):
    onboarded()
    env.media["m1"] = b"plain text, not a document"
    post(payload(media_msg("document", mime="text/plain")))
    assert env.texts == [(NUMBER, channel.SEND_PHOTO_OR_PDF)]


def test_a_sticker_is_turned_away(env):
    post(payload({"from": NUMBER, "id": "s", "type": "sticker"}))
    assert env.texts == [(NUMBER, channel.SEND_PHOTO_OR_PDF)]


def test_a_voice_note_is_heard_answered_in_text_and_by_voice(env):
    onboarded()
    env.media["m1"] = b"ogg-bytes"
    post(payload(media_msg("audio", mime="audio/ogg")))
    assert env.texts
    assert env.spoken and env.spoken[0][2] == "opus"
    assert env.uploads == [("voice.ogg", "audio/ogg")]
    assert env.audio == [(NUMBER, "up1")]


def test_a_voice_note_falls_back_to_mp3_when_opus_is_refused(env, monkeypatch):
    onboarded()
    def tts(text, *, language, speaker=None, codec=None):
        if codec == "opus":
            raise sarvam.SarvamBadRequest("no opus")
        return {"audios": [base64.b64encode(b"voice").decode()]}

    monkeypatch.setattr(sarvam, "text_to_speech", tts)
    post(payload(media_msg("audio", mime="audio/ogg")))
    assert env.uploads == [("voice.mpeg", "audio/mpeg")]


def test_an_empty_transcript_is_told_so(env, monkeypatch):
    monkeypatch.setattr(sarvam, "speech_to_text", lambda *a, **k: {"transcript": "  "})
    post(payload(media_msg("audio", mime="audio/ogg")))
    assert env.texts == [(NUMBER, channel.COULD_NOT_HEAR)]


def test_a_speech_failure_still_sends_the_text(env, monkeypatch):
    onboarded()
    def broken(*a, **k):
        raise sarvam.SarvamUnavailable("down")

    monkeypatch.setattr(sarvam, "text_to_speech", broken)
    post(payload(media_msg("audio", mime="audio/ogg")))
    assert env.texts and env.audio == []


def test_a_long_consent_prompt_falls_back_to_plain_text(env, monkeypatch):
    onboarded()
    monkeypatch.setattr(i18n, "translate", lambda text, language: text + "x" * 1100)
    post(payload(media_msg("image")))
    assert env.buttons == [] and len(env.texts[0][1]) > 1024


def test_delete_everything_deletes_the_case(env):
    consent()
    post(payload(text_msg("delete everything")))
    conn = store.connect()
    assert store.find_case_for_user(conn, USER) is None
    conn.close()
    assert conversation.DELETED in env.texts[0][1]


def test_first_messages_arriving_together_open_one_case(env):
    errors = []

    def run(i):
        try:
            channel.process(text_msg("hello", mid=f"w{i}"))
        except Exception as exc:  # process must never raise
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    conn = store.connect()
    count = conn.db.cases.count_documents({"channel_user": USER})
    conn.close()
    assert errors == [] and count == 1 and len(env.lists) == 4


# --- Onboarding: language, welcome, three options --------------------------------


def labels(out):
    return [body for _, body in out.texts]


def row_ids(out, index=-1):
    return [rid for rid, _, _ in out.lists[index][3]]


def test_first_text_gets_only_the_language_list(env):
    post(payload(text_msg("hello")))
    assert env.texts == [] and env.buttons == [] and len(env.lists) == 1
    assert len(channel.NATIVE_NAMES) == len(config.SUPPORTED_LANGUAGES) == 11
    # 10 rows is WhatsApp's limit: nine languages and "more", then the other two and "back".
    assert row_ids(env) == [f"lang:{c}" for c in list(config.SUPPORTED_LANGUAGES)[:9]] + ["lang:more"]
    assert [title for _, title, _ in env.lists[0][3]][:2] == ["English", "हिन्दी"]


def test_more_languages_shows_the_rest_and_a_way_back(env):
    post(payload(text_msg("hello", mid="a")))
    post(payload(tap("lang:more", mid="b")))
    assert row_ids(env) == ["lang:pa-IN", "lang:od-IN", "lang:back"]
    post(payload(tap("lang:back", mid="c")))
    assert row_ids(env)[-1] == "lang:more"
    post(payload(tap("lang:more", mid="d")))
    post(payload(tap("lang:od-IN", mid="e")))
    conn = store.connect()
    assert store.find_case_for_user(conn, USER)["language"] == "od-IN"
    conn.close()
    assert env.buttons  # the three options follow


def test_tapping_a_language_row_sets_it(env):
    post(payload(text_msg("hello", mid="a")))
    post(payload(tap("lang:ta-IN", mid="b")))
    conn = store.connect()
    assert store.find_case_for_user(conn, USER)["language"] == "ta-IN"
    conn.close()


def test_if_meta_refuses_the_list_the_numbered_text_is_sent(env, monkeypatch):
    monkeypatch.setattr(meta, "send_list", lambda *a, **k: False)
    post(payload(text_msg("hello")))
    assert env.texts == [(NUMBER, channel.LANGUAGE_MENU)]
    assert all(name in channel.LANGUAGE_MENU for name in channel.NATIVE_NAMES.values())


@pytest.mark.parametrize("reply", ["3", "বাংলা", "Bengali", " bengali. "])
def test_choosing_a_language_sets_it_and_sends_welcome_then_three_buttons(env, reply):
    post(payload(text_msg(reply)))
    conn = store.connect()
    assert store.find_case_for_user(conn, USER)["language"] == "bn-IN"
    conn.close()
    assert env.texts == [(NUMBER, f"[bn-IN] {conversation.WELCOME}")]
    [(to, body, buttons)] = env.buttons
    assert [bid for bid, _ in buttons] == ["journey:find", "journey:check", "journey:complain"]
    assert all(len(title) <= 20 for _, title in conversation.MENU_BUTTONS)  # English; the real send cuts at 20


def test_an_unrecognised_reply_gets_the_menu_again(env):
    post(payload(text_msg("hello", mid="a")))
    post(payload(text_msg("12", mid="b")))
    post(payload(text_msg("klingon", mid="c")))
    assert len(env.lists) == 3 and env.texts == []


def test_a_photo_before_a_language_is_not_downloaded_or_read(env, monkeypatch):
    monkeypatch.setattr(meta, "download_media", lambda media_id: pytest.fail("downloaded before onboarding"))
    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: pytest.fail("read before onboarding"))
    post(payload(media_msg("image")))
    assert env.texts == [(NUMBER, channel.CHOOSE_FIRST)] and len(env.lists) == 1


def test_delete_everything_works_before_a_language_is_chosen(env):
    consent()
    post(payload(text_msg("delete everything")))
    assert conversation.DELETED in env.texts[0][1]
    post(payload(text_msg("hi", mid="again")))
    assert len(env.lists) == 1


def test_menu_and_language_keywords(env):
    onboarded()
    post(payload(text_msg("menu", mid="a")))
    assert [bid for bid, _ in env.buttons[0][2]] == ["journey:find", "journey:check", "journey:complain"]
    post(payload(text_msg("language", mid="b")))
    assert len(env.lists) == 1
    post(payload(text_msg("2", mid="c")))
    conn = store.connect()
    assert store.find_case_for_user(conn, USER)["language"] == "hi-IN"
    conn.close()


def heard(monkeypatch, words):
    monkeypatch.setattr(sarvam, "speech_to_text", lambda *a, **k: {"transcript": words})


def test_a_voice_note_asking_for_the_menu_gets_a_voice_reply(env, monkeypatch):
    onboarded()
    heard(monkeypatch, "menu")
    post(payload(media_msg("audio", mime="audio/ogg")))
    assert len(env.buttons) == 1 and env.audio == [(NUMBER, "up1")]


def test_a_voice_note_choosing_a_language_gets_the_welcome_in_voice(env, monkeypatch):
    heard(monkeypatch, "3")
    post(payload(media_msg("audio", mime="audio/ogg")))
    assert env.texts == [(NUMBER, f"[bn-IN] {conversation.WELCOME}")]
    assert len(env.buttons) == 1 and len(env.audio) == 2  # the welcome and the menu, spoken


def test_a_typed_menu_request_gets_no_voice(env):
    onboarded()
    post(payload(text_msg("menu")))
    assert len(env.buttons) == 1 and env.audio == []


def tap(button_id, mid="t1"):
    return {"from": NUMBER, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}}}


@pytest.mark.parametrize("journey", ["find", "check", "complain"])
def test_choosing_an_option_records_it_and_says_what_to_send(env, journey):
    onboarded()
    post(payload(tap(f"journey:{journey}")))
    conn = store.connect()
    case = store.find_case_for_user(conn, USER)
    assert store.latest_event(conn, case["id"], "journey_chosen")["detail"] == {"journey": journey}
    conn.close()
    assert env.texts == [(NUMBER, conversation.JOURNEY_OPENINGS[journey])]


def classify(monkeypatch, **payload):
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: payload)


def choose(journey):
    onboarded()
    post(payload(tap(f"journey:{journey}", mid=f"j-{journey}")))


def test_complaint_names_who_owes_the_answer_and_offers_a_letter(env, monkeypatch):
    from app import cases

    monkeypatch.setattr(cases, "respondent_names", lambda conn, case_id: {"insurer": "Example General Insurance Company Ltd"})
    choose("complain")
    classify(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    post(payload(text_msg("my claim was rejected", mid="c1")))
    [(_, body, buttons)] = env.buttons  # the route message carries the letter button
    assert "Example General Insurance Company Ltd" in body
    assert [bid for bid, _ in buttons] == ["letter"]
    post(payload(text_msg("letter", mid="c2")))
    sent = labels(env)
    assert any("To:" in b and "Example General Insurance Company Ltd" in b for b in sent)
    assert sent[-1] == conversation.DRAFT_NOT_SENT


def test_complaint_without_the_insurers_name_says_so_and_never_crashes(env, monkeypatch):
    choose("complain")
    classify(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    post(payload(text_msg("my claim was rejected", mid="c1")))
    assert conversation.NEED_NAME in env.texts[-1][1] and env.buttons == []
    post(payload(text_msg("letter", mid="c2")))
    assert env.texts[-1][1] == conversation.NEED_NAME


def _complain_after_sending_a_policy(env, monkeypatch, extract):
    monkeypatch.setattr(documents, "extract", extract)
    choose("complain")
    consent()
    post(payload(media_msg("image", mid="p1")))
    classify(monkeypatch, intent="grievance", grievance_class="insurance/claim_denied", product="health_policy")
    post(payload(text_msg("my claim was rejected", mid="c1")))


def _policy_read(confidence):
    return lambda *a, **k: documents.Extraction(
        "policy", False, "doc_ai",
        documents.normalise_fields("policy", {"insurer": "Example General Insurance Company Ltd", "confidence": {"insurer": confidence}}),
    )


def test_a_policy_she_sends_gives_the_insurers_name_and_the_letter(env, monkeypatch):
    _complain_after_sending_a_policy(env, monkeypatch, _policy_read(0.95))
    [(_, body, buttons)] = env.buttons
    assert "Example General Insurance Company Ltd" in body and [bid for bid, _ in buttons] == ["letter"]
    post(payload(text_msg("letter", mid="c2")))
    assert labels(env)[-1] == conversation.DRAFT_NOT_SENT


def test_a_policy_read_with_low_confidence_does_not_name_the_insurer(env, monkeypatch):
    _complain_after_sending_a_policy(env, monkeypatch, _policy_read(0.1))
    assert env.buttons == [] and conversation.NEED_NAME in env.texts[-1][1]
    post(payload(text_msg("letter", mid="c2")))
    assert env.texts[-1][1] == conversation.NEED_NAME


def test_a_failed_read_of_the_policy_still_answers_without_a_name(env, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("Doc AI down")

    _complain_after_sending_a_policy(env, monkeypatch, broken)
    assert env.buttons == [] and conversation.NEED_NAME in env.texts[-1][1]


def test_a_policy_she_is_only_considering_is_not_read_for_a_name(env, monkeypatch):
    monkeypatch.setattr(documents, "extract", lambda *a, **k: pytest.fail("read for a name while buying"))
    choose("find")
    consent()
    post(payload(media_msg("image", mid="p1")))


def test_a_complaint_that_is_not_placed_asks_for_detail_not_a_coverage_answer(env, monkeypatch):
    choose("complain")
    classify(monkeypatch, intent="question", grievance_class=None, product=None)
    post(payload(text_msg("is this covered", mid="c1")))
    assert env.texts[-1][1] == conversation.NEEDS_DETAIL


def test_buying_without_documents_says_what_to_look_for_when_no_source(env, monkeypatch):
    from app.rag.answer import Answer

    seen = {}

    def no_source(question, **kwargs):
        seen.update(kwargs)
        return Answer("no_source", "", "", (), False, None, "en-IN", False)

    monkeypatch.setattr(conversation, "_ask", no_source)
    choose("find")
    post(payload(text_msg("which policy suits my parents", mid="f1")))
    assert env.texts[-1][1] == conversation.WHAT_TO_LOOK_FOR
    assert not seen.get("insurer") and not seen.get("product")  # no insurer's wording is borrowed


def test_a_policy_she_is_considering_fills_no_checklist_and_names_no_respondent(env, monkeypatch):
    from app import cases

    asked = []

    def ask(question, **kwargs):
        asked.append(question)
        return fake_ask(question, **kwargs)

    monkeypatch.setattr(conversation, "_ask", ask)
    choose("find")
    consent()
    post(payload(media_msg("image", mid="p1", caption="policy")))
    assert conversation.BUY_CHECK_QUESTION in asked and conversation.SUMMARY_QUESTION not in asked
    conn = store.connect()
    case = store.find_case_for_user(conn, USER)
    state = cases.checklist_state(conn, case["id"], cases.load_checklist())
    assert not state.collected
    assert "insurer" not in cases.respondent_names(conn, case["id"])
    conn.close()


def test_check_journey_still_summarises_with_the_plain_question(env, monkeypatch):
    asked = []
    monkeypatch.setattr(conversation, "_ask", lambda q, **k: asked.append(q) or fake_ask(q, **k))
    choose("check")
    consent()
    post(payload(media_msg("image", mid="p1")))
    assert conversation.SUMMARY_QUESTION in asked


# --- The transcript and her saved documents (MongoDB) --------------------------


def case_of(conn):
    return store.find_case_for_user(conn, USER)["id"]


def test_the_chat_is_kept_masked_with_her_words_and_ours(env):
    onboarded()
    consent()
    post(payload(text_msg("my PAN is ABCDE1234F, is cataract covered", mid="t1")))
    conn = store.connect()
    turns = store.case_messages(conn, case_of(conn))
    assert [t["role"] for t in turns][0] == "user" and "praman" in [t["role"] for t in turns]
    assert all("ABCDE1234F" not in t["text"] for t in turns)
    conn.close()


def test_a_document_she_sent_survives_a_restart(env):
    from app.rag import mine

    onboarded()
    consent()
    post(payload(media_msg("image", mid="d1")))
    conn = store.connect()
    cid = case_of(conn)
    assert [d["filename"] for d in store.case_pages(conn, cid)] and mine.has(cid)
    mine.forget(cid)  # a restart empties memory
    assert not mine.has(cid)
    post(payload(text_msg("what is the sum insured", mid="d2")))
    assert mine.has(cid)
    conn.close()


def test_nothing_of_hers_is_saved_before_she_consents(env):
    onboarded()
    post(payload(media_msg("image", mid="n1")))
    conn = store.connect()
    cid = case_of(conn)
    assert store.case_pages(conn, cid) == []
    conn.close()


def test_delete_everything_leaves_no_chat_and_no_document_text(env):
    from app.rag import mine

    onboarded()
    consent()
    post(payload(media_msg("image", mid="x1")))
    conn = store.connect()
    cid = case_of(conn)
    assert store.case_messages(conn, cid) and store.case_pages(conn, cid)
    post(payload(text_msg("delete everything", mid="x2")))
    assert store.find_case_for_user(conn, USER) is None
    assert store.case_messages(conn, cid) == [] and store.case_pages(conn, cid) == []
    assert not mine.has(cid)
    conn.close()


# --- Reset: erase everything and start again ---------------------------------------


@pytest.mark.parametrize("word", ["reset", "Reset!", "start over", "रीसेट"])
def test_reset_erases_the_case_and_restarts_at_the_language_list(env, word):
    choose("find")
    consent()
    env.texts.clear(), env.lists.clear(), env.buttons.clear()
    post(payload(text_msg(word, mid="r1")))
    assert env.texts == [(NUMBER, conversation.DELETED)] and len(env.lists) == 1
    assert row_ids(env)[-1] == "lang:more"
    conn = store.connect()
    assert store.find_case_for_user(conn, USER) is None  # case, consent and journey are gone
    conn.close()
    post(payload(text_msg("hi", mid="r2")))  # the next message starts onboarding from nothing
    assert len(env.lists) == 2
    conn = store.connect()
    assert store.find_case_for_user(conn, USER)["language"] is None
    conn.close()


def test_reset_works_before_a_language_is_chosen(env):
    post(payload(text_msg("reset")))
    assert (NUMBER, conversation.DELETED) in env.texts and len(env.lists) == 1


def test_delete_everything_does_not_restart_onboarding(env):
    onboarded()
    post(payload(text_msg("delete everything")))
    assert env.texts == [(NUMBER, conversation.DELETED)] and env.lists == []


def test_the_welcome_mentions_reset():
    assert "reset" in conversation.WELCOME
