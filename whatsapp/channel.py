"""WhatsApp over Meta's Cloud API: the webhook and the glue to the shared conversation.

Meta posts every inbound message to /api/whatsapp/webhook. The route checks the
signature, answers at once (Meta retries after a few seconds) and hands each new
message to ``process`` in the background. The conversation itself is
app/conversation.py, shared with the web chat; this module only moves messages.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app import config, conversation, store
from app.clients import sarvam
from app.conversation import CONSENT_PROMPT, UNVERIFIED_BADGE, Message
from app.services import documents, i18n
from app.services.documents import UploadRejected
from whatsapp import meta

logger = logging.getLogger(__name__)
router = APIRouter()

SEND_PHOTO_OR_PDF = "I can read photos and PDFs, and listen to voice notes. Please send it as one of those."
COULD_NOT_HEAR = "I could not hear any words in that voice note. Please try again."
READING = "Reading your document. This can take a minute."
CONSENT_BUTTONS = [("YES", "YES"), ("NO", "NO")]
SEEN_LIMIT = 5000

# Meta redelivers; one message must be answered once.
# ponytail: in memory, lost on restart; a store table if redelivery after a restart matters.
_SEEN: OrderedDict[str, None] = OrderedDict()
_SEEN_LOCK = threading.Lock()

# One message at a time per sender: an album arrives as parallel webhooks, and two
# first messages would race to open the same case. Also keeps replies in order.
_USER_LOCKS: dict[str, threading.Lock] = {}
_USER_LOCKS_LOCK = threading.Lock()


def _first_time(message_id: str) -> bool:
    if not message_id:
        return True
    with _SEEN_LOCK:
        if message_id in _SEEN:
            return False
        _SEEN[message_id] = None
        while len(_SEEN) > SEEN_LIMIT:
            _SEEN.popitem(last=False)
        return True


def _lock_for(user: str) -> threading.Lock:
    with _USER_LOCKS_LOCK:
        return _USER_LOCKS.setdefault(user, threading.Lock())


def _number(user: str) -> str:
    return user.removeprefix("whatsapp:+")


# --- Webhook -----------------------------------------------------------------


@router.get("/api/whatsapp/webhook")
def verify(request: Request):
    """Meta's subscribe handshake: echo hub.challenge."""
    params = request.query_params
    expected = meta.verify_token()
    if expected and params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == expected:
        return PlainTextResponse(params.get("hub.challenge") or "")
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/api/whatsapp/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    # The signature covers the raw bytes, so read them before parsing.
    raw = await request.body()
    if not meta.verify_signature(raw, request.headers.get("x-hub-signature-256"), meta.app_secret()):
        logger.warning("Refused a WhatsApp webhook with a bad or missing signature")
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"ok": True})
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for message in (change.get("value") or {}).get("messages") or []:  # statuses are ignored
                if _first_time(message.get("id", "")):
                    background.add_task(process, message)
    return JSONResponse({"ok": True})


# --- Outbound ----------------------------------------------------------------


def _known_language(user: str) -> str | None:
    conn = store.connect()
    try:
        case = store.find_case_for_user(conn, user) or {}
    finally:
        conn.close()
    return case.get("language")


def _language_of(user: str) -> str:
    return _known_language(user) or config.DEFAULT_LANGUAGE


def deliver(user: str, language: str, message: Message, voice: bool = False) -> None:
    """One message in her language: text (the consent question as buttons), then a voice note if she spoke."""
    body = conversation.render(message, language)
    if message.unverified:
        body = f"{body}\n{i18n.translate(UNVERIFIED_BADGE, language)}"
    if message.text == CONSENT_PROMPT and len(body) <= 1024:
        meta.send_buttons(_number(user), body, CONSENT_BUTTONS)
    else:
        meta.send_text(_number(user), body)
    if voice:
        _speak(user, language, conversation.without_citations(body))


def _speak(user: str, language: str, text: str) -> None:
    """A voice note after the text. Opus is what WhatsApp plays; mp3 if Sarvam will not make opus."""
    try:
        for codec, mime in (("opus", "audio/ogg"), ("mp3", "audio/mpeg")):
            try:
                payload = sarvam.text_to_speech(text, language=language, codec=codec)
            except sarvam.SarvamBadRequest:
                continue
            audio = b"".join(base64.b64decode(chunk) for chunk in payload.get("audios") or [])
            media_id = meta.upload_media(audio, "voice." + mime.split("/")[-1], mime) if audio else None
            if media_id:
                meta.send_audio(_number(user), media_id)
            return
    except Exception as exc:  # she already has the text
        logger.warning("Voice reply failed: %s", exc)


def _say(user: str, text: str) -> None:
    deliver(user, _language_of(user), Message(text))


# --- One message, end to end -------------------------------------------------


def _attachment(inbound: meta.Inbound) -> conversation.Attachment | str:
    """Her file, checked like a web upload, or the message to send back if it will not do."""
    data = meta.download_media(inbound.media_id or "")
    try:
        mime = documents.validate_upload(data, inbound.filename or f"{inbound.kind}.jpg", inbound.mime or None)
    except UploadRejected:
        return SEND_PHOTO_OR_PDF
    return conversation.Attachment(mime, lambda d=data: d, caption=inbound.text or None, filename=inbound.filename)


def _consented(user: str) -> bool:
    conn = store.connect()
    try:
        case = store.find_case_for_user(conn, user)
        return bool(case) and store.has_consent(conn, case["id"], "read_documents")
    finally:
        conn.close()


def process(message: dict) -> None:
    """Handle one inbound message. Runs after the webhook has answered; never raises."""
    try:
        inbound = meta.parse_inbound(message)
        if not _number(inbound.user):
            return
        with _lock_for(inbound.user):
            meta.send_typing(inbound.message_id)
            _handle(inbound)
    except Exception:
        logger.exception("WhatsApp message could not be handled")


def _handle(inbound: meta.Inbound) -> None:
    user, text, attachments, spoke = inbound.user, inbound.text, (), False
    if inbound.kind == "unsupported" or (inbound.kind != "text" and not inbound.media_id):
        return _say(user, SEND_PHOTO_OR_PDF)
    if inbound.kind == "audio":
        heard = sarvam.speech_to_text(meta.download_media(inbound.media_id), "voice.ogg", language=_known_language(user))
        text, spoke = (heard.get("transcript") or "").strip(), True
        if not text:
            return _say(user, COULD_NOT_HEAR)
    elif inbound.kind in ("image", "document"):
        attachment = _attachment(inbound)
        if isinstance(attachment, str):
            return _say(user, attachment)
        attachments = (attachment,)
        if _consented(user):  # WhatsApp drops the typing bubble after 25 s; a full read takes longer
            _say(user, READING)
    conn = store.connect()
    try:
        reply = conversation.respond(conn, user, text, attachments, explain_documents=True)
    finally:
        conn.close()
    for message in reply.messages:
        deliver(user, reply.language, message, voice=spoke)
