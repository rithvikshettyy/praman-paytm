"""PRD-PAYTM N3 over WhatsApp (Twilio): photos tick slots, replies by voice."""

import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, conversation, store
from app.channels import whatsapp as wa
from app.clients import sarvam
from app.main import app
from app.services import documents, i18n

USER = "whatsapp:+910000000000"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16
DISCHARGE = "DISCHARGE SUMMARY\nDate of discharge: 08/10/2026\nDiagnosis: cataract"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://praman.example")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    i18n._CACHE.clear()
    wa._MEDIA.clear()
    wa._AWAITING_CONSENT.clear()
    conn = store.connect()
    store.record_consent(conn, store.case_for_user(conn, USER)["id"], "read_documents", True)
    conn.close()

    sent, spoken, translated = [], [], []

    def send(to, body=None, media_url=None):
        sent.append(SimpleNamespace(to=to, body=body, media_url=media_url))
        return True

    def tts(text, *, language, speaker=None, codec=None):
        spoken.append(SimpleNamespace(text=text, language=language, codec=codec))
        return {"audios": [base64.b64encode(b"ID3-fake-mp3").decode()]}

    def translate(text, target, **kwargs):
        translated.append(target)
        return f"[{target}] {text}"

    monkeypatch.setattr(wa, "send", send)
    monkeypatch.setattr(wa, "download_media", lambda url: PNG)
    monkeypatch.setattr(sarvam, "text_to_speech", tts)
    monkeypatch.setattr(sarvam, "translate", translate)
    monkeypatch.setattr(sarvam, "identify_language", lambda text: {"language_code": "mr-IN"})
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: None)  # classifier: unknown intent
    monkeypatch.setattr(documents, "read_text", lambda *a, **k: "blurry")
    yield SimpleNamespace(sent=sent, spoken=spoken, translated=translated, monkeypatch=monkeypatch)
    i18n._CACHE.clear()


def form(text="", media=0, content_type="image/jpeg", user=USER):
    fields = {"From": user, "Body": text, "NumMedia": str(media)}
    for i in range(media):
        fields[f"MediaUrl{i}"] = f"https://api.twilio.com/media/{i}"
        fields[f"MediaContentType{i}"] = content_type
    return fields


def say(env, **kwargs):
    env.sent.clear()
    env.spoken.clear()
    wa.process(wa.parse_inbound(form(**kwargs)))
    texts = [m.body for m in env.sent if m.body]
    audio = [m.media_url for m in env.sent if m.media_url]
    return texts, audio


def case_state():
    conn = store.connect()
    try:
        case = store.case_for_user(conn, USER)
        from app import cases

        return cases.checklist_state(conn, case["id"], cases.load_checklist())
    finally:
        conn.close()


# --- Parsing -----------------------------------------------------------------


def test_parse_inbound_reads_twilio_fields():
    inbound = wa.parse_inbound(form(text=" bill ", media=2, content_type="image/png"))
    assert inbound.user == USER
    assert inbound.text == "bill"
    assert [m.content_type for m in inbound.media] == ["image/png", "image/png"]
    assert inbound.media[1].url.endswith("/1")


# --- Photos tick slots, replies list what is missing by voice ----------------


def test_a_clear_photo_ticks_its_slot_and_the_reply_names_what_is_missing(env):
    env.monkeypatch.setattr(documents, "read_text", lambda *a, **k: DISCHARGE)
    texts, audio = say(env, media=1)

    assert texts == ["Still missing, 5 of 6: Final bill, ID proof, Policy copy, Prescriptions, Claim form.\n" + wa.UNVERIFIED_BADGE]
    assert audio and audio[0].startswith("https://praman.example/media/") and audio[0].endswith(".mp3")
    assert env.spoken[0].codec == "mp3"
    assert case_state().collected == 1


def test_replies_come_in_her_language_by_voice(env):
    texts, _ = say(env, text="नमस्कार, माझ्या वडिलांचा क्लेम")
    assert texts[0] == f"[mr-IN] {conversation.HELP}"  # no claim documents yet: no health checklist
    assert env.spoken[0].language == "mr-IN"

    env.monkeypatch.setattr(documents, "read_text", lambda *a, **k: DISCHARGE)
    texts, audio = say(env, media=1)
    assert texts[0].startswith("[mr-IN] Still missing, 5 of 6")
    assert env.spoken[0].language == "mr-IN"
    assert audio


def test_a_caption_names_the_slot(env):
    say(env, text="bill", media=1)
    assert "final_bill" not in case_state().missing


def test_unclear_photo_gets_a_numbered_list_and_a_number_picks_the_slot(env):
    texts, _ = say(env, media=1)
    assert "1. Discharge summary" in texts[0]
    assert "6. Claim form" in texts[0]
    assert case_state().pending_document_id is not None

    texts, _ = say(env, text="2")
    assert texts == ["Still missing, 5 of 6: Discharge summary, ID proof, Policy copy, Prescriptions, Claim form.\n" + wa.UNVERIFIED_BADGE]
    state = case_state()
    assert "final_bill" not in state.missing
    assert state.pending_document_id is None


def test_a_devanagari_number_picks_the_slot_too(env):
    say(env, media=1)
    say(env, text="६")
    assert "claim_form" not in case_state().missing


def test_a_number_out_of_range_asks_again(env):
    say(env, media=1)
    texts, _ = say(env, text="9")
    assert "1. Discharge summary" in texts[0]
    assert case_state().pending_document_id is not None


def test_a_voice_note_is_not_filed_as_a_document(env):
    texts, _ = say(env, media=1, content_type="audio/ogg")
    assert case_state().collected == 0
    assert texts[0].endswith(conversation.HELP)


def test_text_without_a_pending_photo_gets_the_current_checklist(env):
    texts, _ = say(env, text="what is missing?")
    assert texts[0].startswith("[mr-IN] Still missing, 6 of 6")


def test_voice_is_skipped_but_text_still_sent_without_a_public_url(env):
    env.monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    texts, audio = say(env, text="status")
    assert texts and not audio


def test_a_speech_failure_still_sends_the_text(env):
    def broken(*args, **kwargs):
        raise sarvam.SarvamUnavailable("down")

    env.monkeypatch.setattr(sarvam, "text_to_speech", broken)
    texts, audio = say(env, text="status")
    assert texts and not audio


def test_never_says_filed(env):
    for slot_number in range(1, 7):
        say(env, media=1)
        texts, _ = say(env, text=str(slot_number))
    assert "All 6 documents are in. Nothing is missing." in texts[0]
    assert "filed" not in texts[0].lower()


def test_tts_asks_sarvam_for_the_requested_codec(monkeypatch):
    seen = {}

    def fake_call(attr, method, **kwargs):
        seen.update(kwargs)
        return {"audios": []}

    monkeypatch.setattr(sarvam, "_speech_call", fake_call)
    sarvam.text_to_speech("hello", language="mr-IN", codec="mp3")
    assert seen["output_audio_codec"] == "mp3"

    seen.clear()
    sarvam.text_to_speech("hello", language="mr-IN")
    assert "output_audio_codec" not in seen


# --- Webhook, signature, media -----------------------------------------------

client = TestClient(app)
WEBHOOK = "https://praman.example/api/whatsapp/webhook"


def test_signature_round_trip():
    params = form(text="hi")
    signature = wa.signature(WEBHOOK, params, "secret")
    assert wa.valid_signature(WEBHOOK, params, signature, "secret")
    assert not wa.valid_signature(WEBHOOK, {**params, "Body": "tampered"}, signature, "secret")
    assert not wa.valid_signature(WEBHOOK, params, signature, "other-secret")
    assert not wa.valid_signature(WEBHOOK, params, "", "secret")


def test_webhook_acknowledges_then_replies(env):
    env.monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret")
    params = form(text="status")
    response = client.post(
        "/api/whatsapp/webhook", data=params, headers={"X-Twilio-Signature": wa.signature(WEBHOOK, params, "secret")}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert "<Response" in response.text
    assert any(m.body and "Still missing" in m.body for m in env.sent)


def test_webhook_refuses_a_bad_signature(env):
    env.monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret")
    response = client.post("/api/whatsapp/webhook", data=form(text="status"), headers={"X-Twilio-Signature": "forged"})
    assert response.status_code == 403
    assert env.sent == []


def test_voice_note_audio_is_served_for_twilio_to_fetch(env):
    _, audio = say(env, text="status")
    path = audio[0].removeprefix("https://praman.example")
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"ID3-fake-mp3"


def test_unknown_media_is_404():
    assert client.get("/media/nope.mp3").status_code == 404


# --- N6: consent before the first document is read ---------------------------

NEWCOMER = "whatsapp:+910000000001"


def state_for(user):
    from app import cases

    conn = store.connect()
    try:
        case = store.case_for_user(conn, user)
        return cases.checklist_state(conn, case["id"], cases.load_checklist()), case["id"]
    finally:
        conn.close()


def consents(case_id, scope):
    conn = store.connect()
    try:
        return store.has_consent(conn, case_id, scope)
    finally:
        conn.close()


def test_first_photo_gets_a_consent_prompt_in_her_language_and_nothing_is_read(env):
    def boom(*args, **kwargs):
        raise AssertionError("nothing may be read before consent")

    env.monkeypatch.setattr(documents, "read_text", boom)
    say(env, text="नमस्कार, वडिलांचा क्लेम आहे", user=NEWCOMER)
    texts, audio = say(env, media=1, user=NEWCOMER)

    assert texts == ["[mr-IN] " + wa.CONSENT_PROMPT]
    assert audio  # by voice too
    state, _ = state_for(NEWCOMER)
    assert state.collected == 0 and state.pending_document_id is None


def test_yes_records_consent_and_reads_the_photo_she_already_sent(env):
    say(env, media=1, user=NEWCOMER)
    env.monkeypatch.setattr(documents, "read_text", lambda *a, **k: DISCHARGE)
    texts, _ = say(env, text="हो", user=NEWCOMER)

    state, case_id = state_for(NEWCOMER)
    assert consents(case_id, "read_documents") and consents(case_id, "store_fields")
    assert "discharge_summary" not in state.missing
    assert texts[0].startswith("Still missing, 5 of 6")


def test_no_records_the_refusal_and_reads_nothing(env):
    say(env, media=1, user=NEWCOMER)
    texts, _ = say(env, text="no", user=NEWCOMER)

    state, case_id = state_for(NEWCOMER)
    assert texts == [wa.CONSENT_DECLINED]
    assert consents(case_id, "read_documents") is False
    assert state.collected == 0

    texts, _ = say(env, media=1, user=NEWCOMER)
    assert texts == [wa.CONSENT_PROMPT]


def test_an_unclear_answer_asks_again(env):
    say(env, media=1, user=NEWCOMER)
    texts, _ = say(env, text="maybe later", user=NEWCOMER)
    assert texts[0].endswith(wa.CONSENT_PROMPT)


# --- N6: delete everything ----------------------------------------------------


@pytest.mark.parametrize("command", ["delete everything", "Delete everything!", "DELETE EVERYTHING", "सगळं हटवा"])
def test_delete_everything_deletes_the_case_and_says_so(env, command):
    env.monkeypatch.setattr(documents, "read_text", lambda *a, **k: DISCHARGE)
    say(env, media=1)
    _, case_id = state_for(USER)
    conn = store.connect()
    store.record_event(conn, case_id, "case_routed", {"respondent": "insurer", "distributor_owned": False})
    conn.close()

    texts, _ = say(env, text=command)
    assert texts[-1].endswith(wa.DELETED)

    conn = store.connect()
    try:
        assert store.get_case(conn, case_id) is None
        for table in ("documents", "consents", "events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE case_id = ?", (case_id,)).fetchone()[0] == 0
        from app import console

        assert console.case_list(conn) == []
    finally:
        conn.close()


def test_after_deleting_she_starts_fresh_and_is_asked_again(env):
    say(env, text="delete everything")
    texts, _ = say(env, media=1)
    assert texts == [wa.CONSENT_PROMPT]


def test_a_cited_answer_keeps_its_sources_in_text_but_not_in_the_voice_note(env):
    label = "[Example General Insurance, policy_wording, p.1]"
    message = wa.Message(f"Room rent is limited to 1% of the sum insured per day {label}.",
                         citations=({"label": label},), localized=True)
    wa.deliver(USER, "en-IN", message)
    assert label in env.sent[0].body  # no chips on WhatsApp: the source stays written
    assert env.spoken[-1].text == "Room rent is limited to 1% of the sum insured per day."
