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
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setenv("WA_APP_SECRET", SECRET)
    monkeypatch.setenv("WA_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WA_TOKEN", "token")
    monkeypatch.setenv("WA_PHONE_NUMBER_ID", "123")
    i18n._CACHE.clear()
    channel._SEEN.clear()
    channel._USER_LOCKS.clear()
    conversation._AWAITING_CONSENT.clear()

    out = SimpleNamespace(texts=[], buttons=[], audio=[], uploads=[], media={"m1": PNG}, spoken=[], typing=[])
    monkeypatch.setattr(meta, "send_text", lambda to, body: out.texts.append((to, body)) or True)
    monkeypatch.setattr(meta, "send_buttons", lambda to, body, buttons: out.buttons.append((to, body, buttons)) or True)
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
    assert [to for to, _ in env.texts] == [NUMBER]


def test_a_message_delivered_twice_is_answered_once(env):
    post(payload(text_msg("hello", mid="dup")))
    post(payload(text_msg("hello", mid="dup")))
    assert len(env.texts) == 1


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
              "interactive": {"type": "button_reply", "button_reply": {"id": "YES", "title": "YES"}}}
    assert meta.parse_inbound(button).text == "YES"
    assert meta.parse_inbound({"from": NUMBER, "id": "s", "type": "sticker"}).kind == "unsupported"


def test_signature_check_rejects_empty_inputs():
    assert not meta.verify_signature(b"", "sha256=x", SECRET)
    assert not meta.verify_signature(b"x", None, SECRET)
    assert not meta.verify_signature(b"x", "sha256=x", "")


# --- The conversation ----------------------------------------------------------


def test_text_gets_a_reply_and_no_voice_note(env):
    post(payload(text_msg("hello")))
    assert env.texts and env.audio == []


def test_first_photo_asks_consent_with_buttons_and_reads_nothing(env, monkeypatch):
    monkeypatch.setattr(documents, "read_pages", lambda *a, **k: pytest.fail("read before consent"))
    post(payload(media_msg("image")))
    [(to, _, buttons)] = env.buttons
    assert to == NUMBER and buttons == [("YES", "YES"), ("NO", "NO")]
    assert env.texts == []


def test_yes_reads_the_waiting_photo_and_summarises_with_citations(env):
    post(payload(media_msg("image", mid="a")))
    post(payload(text_msg("YES", mid="b")))
    bodies = [body for _, body in env.texts]
    assert any("sum insured of Rs 5,00,000" in b and "[Your document, policy.pdf, p.1]" in b for b in bodies)
    assert conversation.READ_IT in bodies


def test_with_consent_a_reading_notice_comes_first(env):
    consent()
    post(payload(media_msg("document", mime="application/pdf", caption="what is covered?")))
    assert env.texts[0][1] == channel.READING


def test_a_file_that_is_not_a_photo_or_pdf_is_turned_away(env):
    env.media["m1"] = b"plain text, not a document"
    post(payload(media_msg("document", mime="text/plain")))
    assert env.texts == [(NUMBER, channel.SEND_PHOTO_OR_PDF)]


def test_a_sticker_is_turned_away(env):
    post(payload({"from": NUMBER, "id": "s", "type": "sticker"}))
    assert env.texts == [(NUMBER, channel.SEND_PHOTO_OR_PDF)]


def test_a_voice_note_is_heard_answered_in_text_and_by_voice(env):
    env.media["m1"] = b"ogg-bytes"
    post(payload(media_msg("audio", mime="audio/ogg")))
    assert env.texts
    assert env.spoken and env.spoken[0][2] == "opus"
    assert env.uploads == [("voice.ogg", "audio/ogg")]
    assert env.audio == [(NUMBER, "up1")]


def test_a_voice_note_falls_back_to_mp3_when_opus_is_refused(env, monkeypatch):
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
    def broken(*a, **k):
        raise sarvam.SarvamUnavailable("down")

    monkeypatch.setattr(sarvam, "text_to_speech", broken)
    post(payload(media_msg("audio", mime="audio/ogg")))
    assert env.texts and env.audio == []


def test_a_long_consent_prompt_falls_back_to_plain_text(env, monkeypatch):
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
    count = conn.execute("SELECT COUNT(*) FROM cases WHERE channel_user = ?", (USER,)).fetchone()[0]
    conn.close()
    assert errors == [] and count == 1 and len(env.texts) == 4
