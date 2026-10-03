"""Meta Cloud API I/O: signatures, parsing, media, sending. No conversation logic.

Every failure is logged (status and the start of Meta's error body, never the
message text) and returned as False or None; nothing here raises.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass

import httpx

from app import config  # noqa: F401  (loads backend/.env: the one env file for the whole service)

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def token() -> str:
    return _env("WA_TOKEN")


def phone_number_id() -> str:
    return _env("WA_PHONE_NUMBER_ID")


def app_secret() -> str:
    return _env("WA_APP_SECRET")


def verify_token() -> str:
    return _env("WA_VERIFY_TOKEN")


def graph_version() -> str:
    return _env("WA_GRAPH_VERSION", "v26.0")


def configured() -> list[str]:
    """Names of the WA_ settings that are not set."""
    values = {"WA_TOKEN": token(), "WA_PHONE_NUMBER_ID": phone_number_id(),
              "WA_APP_SECRET": app_secret(), "WA_VERIFY_TOKEN": verify_token()}
    return [name for name, value in values.items() if not value]


def verify_signature(raw: bytes, header: str | None, secret: str) -> bool:
    """x-hub-signature-256 is 'sha256=' + HMAC-SHA256(app secret, raw body)."""
    if not (raw and header and secret):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


# --- Inbound -----------------------------------------------------------------


@dataclass(frozen=True)
class Inbound:
    user: str  # whatsapp:+91... (the same shape as before, so existing cases still match)
    message_id: str
    kind: str  # text | audio | image | document | unsupported
    text: str = ""
    media_id: str | None = None
    mime: str = ""
    filename: str | None = None


def parse_inbound(message: dict) -> Inbound:
    kind = message.get("type")
    base = {"user": f"whatsapp:+{message.get('from', '')}", "message_id": message.get("id") or ""}
    if kind == "text":
        return Inbound(kind="text", text=(message.get("text") or {}).get("body", "").strip(), **base)
    if kind == "audio":
        audio = message.get("audio") or {}
        return Inbound(kind="audio", media_id=audio.get("id"), mime=audio.get("mime_type", ""), **base)
    if kind in ("image", "document"):
        media = message.get(kind) or {}
        return Inbound(
            kind=kind, text=(media.get("caption") or "").strip(), media_id=media.get("id"),
            mime=(media.get("mime_type") or "").split(";")[0].strip().lower(),
            filename=media.get("filename"), **base,
        )
    if kind == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply")
        if reply:
            # The id is stable (YES, journey:find, letter); the title is translated and can be cut.
            return Inbound(kind="text", text=(reply.get("id") or reply.get("title") or "").strip(), **base)
    if kind == "button":
        return Inbound(kind="text", text=((message.get("button") or {}).get("text") or "").strip(), **base)
    return Inbound(kind="unsupported", **base)


def download_media(media_id: str) -> bytes:
    """Two requests, both with the token: the id resolves to a short-lived URL, then the URL is fetched."""
    headers = {"Authorization": f"Bearer {token()}"}
    with httpx.Client(timeout=TIMEOUT) as client:
        info = client.get(f"{GRAPH}/{graph_version()}/{media_id}", headers=headers)
        info.raise_for_status()
        blob = client.get(info.json()["url"], headers=headers)
        blob.raise_for_status()
        return blob.content


# --- Outbound ----------------------------------------------------------------


def _post(path: str, **kwargs) -> dict | None:
    if not (token() and phone_number_id()):
        logger.warning("WhatsApp is not configured; nothing was sent.")
        return None
    url = f"{GRAPH}/{graph_version()}/{phone_number_id()}/{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(url, headers={"Authorization": f"Bearer {token()}"}, **kwargs)
    except httpx.HTTPError as exc:
        logger.warning("Meta request failed: %s", exc)
        return None
    if response.status_code >= 400:
        logger.warning("Meta refused a request [%s]: %s", response.status_code, response.text[:300])
        return None
    try:
        return response.json()
    except ValueError:
        return {}


def _send(payload: dict) -> bool:
    return _post("messages", json={"messaging_product": "whatsapp", **payload}) is not None


def send_text(to: str, body: str) -> bool:
    return _send({"to": to, "type": "text", "text": {"body": body[:4096]}})


def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    """Up to three reply buttons; a tap comes back as its title, which the conversation reads."""
    return _send({
        "to": to, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": bid, "title": title[:20]}} for bid, title in buttons[:3]
            ]},
        },
    })


def send_list(to: str, body: str, button: str, rows: list[tuple[str, str, str]]) -> bool:
    """A tap-to-open list, up to 10 rows of (id, title, description); a tap comes back as the row id."""
    return _send({
        "to": to, "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {"button": button[:20], "sections": [{"title": "Languages", "rows": [
                {"id": rid, "title": title[:24], "description": description[:72]}
                for rid, title, description in rows[:10]
            ]}]},
        },
    })


def send_typing(message_id: str) -> bool:
    """Marks the message read and shows the typing bubble (Meta clears it after 25 s or on our reply)."""
    if not message_id:
        return False
    return _send({"status": "read", "message_id": message_id, "typing_indicator": {"type": "text"}})


def upload_media(data: bytes, filename: str, mime: str) -> str | None:
    result = _post(
        "media", files={"file": (filename, data, mime)}, data={"messaging_product": "whatsapp", "type": mime}
    )
    return (result or {}).get("id")


def send_audio(to: str, media_id: str) -> bool:
    return _send({"to": to, "type": "audio", "audio": {"id": media_id}})
