"""Spoken replies: text-to-speech through Sarvam, held briefly for a channel to fetch.

The web chat plays /media/<token>.mp3 relative to the API (WhatsApp uploads its own
voice notes to Meta). Tokens are random, kept in
memory only (the last 200), and hold nothing but the spoken reply.
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
from collections import OrderedDict

from app.clients import sarvam

logger = logging.getLogger(__name__)

_MEDIA: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_MEDIA_LOCK = threading.Lock()
_MEDIA_MAX = 200


def put_media(data: bytes, content_type: str) -> str:
    token = secrets.token_urlsafe(16)
    with _MEDIA_LOCK:
        _MEDIA[token] = (data, content_type)
        while len(_MEDIA) > _MEDIA_MAX:
            _MEDIA.popitem(last=False)
    return token


def get_media(token: str) -> tuple[bytes, str] | None:
    with _MEDIA_LOCK:
        return _MEDIA.get(token)


def speak(text: str, language: str) -> str | None:
    """Speak ``text`` as mp3 and return the path /media/<token>.mp3, or None if speech failed."""
    try:
        payload = sarvam.text_to_speech(text, language=language, codec="mp3")
    except Exception as exc:  # a missing voice reply must never stop the text reply
        logger.warning("Text-to-speech failed: %s", exc)
        return None
    audio = b"".join(base64.b64decode(chunk) for chunk in payload.get("audios") or [])
    if not audio:
        return None
    return f"/media/{put_media(audio, 'audio/mpeg')}.mp3"
