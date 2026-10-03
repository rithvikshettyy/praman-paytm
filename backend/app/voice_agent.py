"""The phone channel: Sarvam's voice agent talks, Praman answers.

The conversation on a call is held by the voice agent built in the Sarvam dashboard (greeting, language,
voice, interruptions). When it needs an answer it calls an HTTP tool on this backend (``turn``): the
caller's number, which journey she is in, and what she said. Praman runs the same conversation as the web
chat and WhatsApp (``conversation.respond``) and returns a short text the agent says out loud. When a call
ends Sarvam posts a webhook (``record_call``).

* She is the case of her phone number: ``whatsapp:+<number with country code>``, the same identity
  WhatsApp uses, so a policy she sent on WhatsApp is the one she is asked about on the phone.
* Journeys are the web chat's start options (``guided``): find a policy, check my policy, complaint.
* A reply is made to be heard: no citations, no links, no lists, cut at a sentence.
* Nothing from the call is kept except that it happened (status and length). A complaint registered on a
  call stores what the web chat stores, and her number is offered as how to reach her.
"""

from __future__ import annotations

import hmac
import re
from typing import Any

from app import config, conversation, guided, store
from app.store import Store

_LINK = re.compile(r"https?://\S+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.M)


def authentic(authorization: str | None, secret_header: str | None = None, token: str | None = None) -> bool:
    """True only when a secret is set and the caller sent it: as a bearer token (the agent's HTTP tool),
    in x-praman-secret, or as ?token= (the webhook, which cannot set headers)."""
    expected = config.VOICE_AGENT_SECRET
    if not expected:
        return False
    bearer = (authorization or "")[7:] if (authorization or "").lower().startswith("bearer ") else ""
    return any(hmac.compare_digest(given or "", expected) for given in (bearer, secret_header, token))


def phone_user(number: str | None) -> str | None:
    """The channel user for a phone number: digits with a country code (a bare 10-digit number is Indian)."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 10:
        digits = "91" + digits
    return f"whatsapp:+{digits}" if 11 <= len(digits) <= 15 else None


def language_code(value: str | None) -> str | None:
    """The agent says "Hindi" or "hi-IN"; the conversation wants "hi-IN"."""
    said = (value or "").strip().lower()
    if not said:
        return None
    for code, name in config.SUPPORTED_LANGUAGES.items():
        if said in (code.lower(), name.lower(), code.split("-")[0].lower()):
            return code
    return None


def speakable(text: str, limit: int | None = None) -> str:
    """Text made to be said: no citation labels, links or list marks, lines joined, cut at a sentence."""
    limit = limit or config.VOICE_REPLY_MAX_CHARS
    text = conversation.without_citations(text or "")
    text = _LINK.sub("", text)
    text = _BULLET.sub("", text)
    text = re.sub(r"\s*\n+\s*", ". ", text)
    text = re.sub(r"\.\s*\.", ".", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind("। "))
    return (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0]).strip()


def turn(
    conn: Store, caller: str | None, journey: str | None, text: str | None, language: str | None = None
) -> dict[str, Any] | None:
    """One thing she said on the phone: what the agent should say back. None when the number is unusable."""
    user = phone_user(caller)
    if user is None:
        return None
    case = store.case_for_user(conn, user)
    code = language_code(language)
    said = (text or "").strip()
    current = (store.latest_event(conn, case["id"], "journey_chosen") or {}).get("detail", {}).get("journey")
    picked = journey if journey in guided.JOURNEYS else None

    messages: list[conversation.Message] = []
    if picked and picked != current:
        messages = list(guided.open_journey(conn, user, case, picked))
        current = picked
        if not said:
            return _reply(messages, code or case.get("language") or config.DEFAULT_LANGUAGE, current)
        messages = []  # she already said something: answer that, not the opening
    if current == "complain":
        guided.set_known_contact(user, "+" + user.removeprefix("whatsapp:+"))

    reply = conversation.respond(
        conn, user, said, language=code, explain_documents=True, journey=current, guided=True,
    ) if said else None
    if reply is not None:
        return _reply(list(reply.messages), reply.language, current)
    return _reply([conversation.Message("I did not hear anything. Please say that again.")], code or config.DEFAULT_LANGUAGE, current)


def _reply(messages: list[conversation.Message], language: str, journey: str | None) -> dict[str, Any]:
    spoken = []
    options: list[str] = []
    for message in messages:
        line = speakable(conversation.render(message, language))
        if line:
            spoken.append(line)
        if message.buttons:
            options = [conversation.render(conversation.Message(title), language) for _, title in message.buttons]
    if options:
        spoken.append(speakable("You can say: " + ", ".join(options) + "."))
    return {"reply": " ".join(spoken), "language": language, "journey": journey, "options": options}


def record_call(conn: Store, payload: dict[str, Any]) -> bool:
    """A call ended (Sarvam's webhook): note that it happened, on the case of the number. True when it was
    recorded. The transcript in the payload is not kept: what she needed is recorded by the conversation."""
    user = phone_user(_find_number(payload))
    metadata = ((payload.get("webhook_config") or {}).get("metadata") or {}) if isinstance(payload, dict) else {}
    case = store.find_case_for_user(conn, user) if user else None
    if case is None and isinstance(metadata.get("case_id"), str):
        case = store.get_case(conn, metadata["case_id"])
    if case is None:
        return False
    store.record_event(conn, case["id"], "call_completed", {
        "status": str(payload.get("status") or "unknown")[:30],
        "seconds": payload.get("duration") if isinstance(payload.get("duration"), (int, float)) else None,
        "direction": "outbound" if metadata else "inbound",
    })
    return True


_NUMBER_KEYS = ("user_phone_number", "caller_number", "caller_phone_number", "from_number", "user_identifier",
                "phone_number", "caller")


def _find_number(payload: Any, depth: int = 0) -> str | None:
    """The caller's number in a webhook payload, whatever the field is called (the docs name no field)."""
    if not isinstance(payload, dict) or depth > 3:
        return None
    for key in _NUMBER_KEYS:
        if isinstance(payload.get(key), str) and payload[key].strip():
            return payload[key]
    for value in payload.values():
        found = _find_number(value, depth + 1) if isinstance(value, dict) else None
        if found:
            return found
    return None
