"""WhatsApp channel over Twilio.

Flow: Twilio posts each inbound message to /api/whatsapp/webhook. The route
checks Twilio's signature, answers at once (Twilio gives up after 15 s), and
hands the message to ``process`` in the background. The conversation itself
lives in app/conversation.py, shared with the web chat; this module only
speaks Twilio. Replies go out through Twilio's REST API: the text in her
language, then the same text as a voice note, which Twilio fetches from
PUBLIC_BASE_URL/media/<token>.mp3 (so that URL must be reachable).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import sqlite3
from dataclasses import dataclass
from typing import Mapping

import httpx

from app import cases, config, conversation, store
from app.conversation import (  # re-exported: the channel's words are the conversation's words
    _AWAITING_CONSENT,
    CONSENT_DECLINED,
    CONSENT_PROMPT,
    DELETED,
    UNVERIFIED_BADGE,
    Message,
    Reply,
)
from app.services import i18n, voice
from app.services.voice import _MEDIA, get_media, put_media  # noqa: F401  (kept for callers and tests)

logger = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"


@dataclass(frozen=True)
class Media:
    url: str
    content_type: str


@dataclass(frozen=True)
class Inbound:
    user: str  # Twilio's From, e.g. whatsapp:+91...
    text: str
    media: tuple[Media, ...] = ()


def parse_inbound(form: Mapping[str, str]) -> Inbound:
    try:
        count = max(0, int(form.get("NumMedia") or 0))
    except ValueError:
        count = 0
    media = tuple(
        Media(form[f"MediaUrl{i}"], (form.get(f"MediaContentType{i}") or "").lower())
        for i in range(count)
        if form.get(f"MediaUrl{i}")
    )
    return Inbound(user=form.get("From", ""), text=(form.get("Body") or "").strip(), media=media)


# --- Twilio signatures -------------------------------------------------------


def signature(url: str, params: Mapping[str, str], token: str) -> str:
    """Twilio's X-Twilio-Signature: HMAC-SHA1 over the URL plus sorted POST params."""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_signature(url: str, params: Mapping[str, str], provided: str, token: str) -> bool:
    if not provided or not token:
        return False
    return hmac.compare_digest(signature(url, params, token), provided)


# --- Twilio I/O --------------------------------------------------------------


def _auth() -> tuple[str, str] | None:
    if config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN:
        return config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN
    return None


def download_media(url: str) -> bytes:
    response = httpx.get(url, auth=_auth(), follow_redirects=True, timeout=30)
    response.raise_for_status()
    return response.content


def send(to: str, body: str | None = None, media_url: str | None = None) -> bool:
    """Send one WhatsApp message. False (and a log line) if it could not go."""
    if not (_auth() and config.TWILIO_WHATSAPP_FROM):
        logger.warning("Twilio is not configured; a reply was not sent.")
        return False
    data = {"From": config.TWILIO_WHATSAPP_FROM, "To": to}
    if body:
        data["Body"] = body
    if media_url:
        data["MediaUrl"] = media_url
    try:
        response = httpx.post(
            f"{TWILIO_API}/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json", data=data, auth=_auth(), timeout=30
        )
    except httpx.HTTPError as exc:
        logger.warning("Twilio send failed: %s", exc)
        return False
    if response.status_code >= 400:
        logger.warning("Twilio refused a message [%s]: %s", response.status_code, response.text[:300])
        return False
    return True


# --- Conversation (shared with the web chat) ---------------------------------


def handle(conn: sqlite3.Connection, inbound: Inbound, checklist: cases.Checklist | None = None) -> Reply:
    attachments = tuple(
        conversation.Attachment(media.content_type, (lambda url=media.url: download_media(url)), inbound.text or None)
        for media in inbound.media
    )
    return conversation.respond(conn, inbound.user, inbound.text, attachments, checklist=checklist)


def _voice(text: str, language: str) -> str | None:
    """Speak ``text`` and return a URL Twilio can fetch, or None if that is not possible."""
    if not config.PUBLIC_BASE_URL:
        logger.info("PUBLIC_BASE_URL is not set; sending text without a voice note.")
        return None
    path = voice.speak(text, language)
    return f"{config.PUBLIC_BASE_URL}{path}" if path else None


def deliver(user: str, language: str, message: Message) -> None:
    """Send one message in her language, as text and then as a voice note."""
    if message.localized:
        badge = i18n.translate(UNVERIFIED_BADGE, language)
        body = f"{message.text}\n{badge}" if message.unverified else message.text
    else:
        text = f"{message.text}\n{UNVERIFIED_BADGE}" if message.unverified else message.text
        body = i18n.translate(text, language)
    send(user, body=body)  # WhatsApp has no chips: the sources stay in the text
    audio_url = _voice(conversation.without_citations(body), language)  # but are not read aloud
    if audio_url:
        send(user, media_url=audio_url)


def process(inbound: Inbound) -> None:
    """Handle one inbound message end to end. Runs after the webhook has answered."""
    try:
        conn = store.connect()
        try:
            reply = handle(conn, inbound)
        finally:
            conn.close()
        for message in reply.messages:
            deliver(inbound.user, reply.language, message)
    except Exception:
        logger.exception("WhatsApp message could not be handled")
