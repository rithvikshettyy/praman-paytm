"""Ring a phone with the voice agent, to try it. Run from backend/:

    python scripts/call_me.py +919876543210 --language Hindi --agreed

``--agreed`` says the owner of the number asked for this call: the number goes to Sarvam's calling
service, so it is recorded as her ``phone_call`` consent on her case. This is a script and not a public
endpoint on purpose: an open "call this number" endpoint could be used to ring strangers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, store, voice_agent  # noqa: E402
from app.clients import sarvam  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("phone", help="the number to ring, with country code, e.g. +919876543210")
    parser.add_argument("--language", default=None, help='the language the agent opens in, e.g. "Hindi"')
    parser.add_argument("--agreed", action="store_true", help="the owner of this number asked for the call")
    args = parser.parse_args()

    user = voice_agent.phone_user(args.phone)
    if user is None:
        print("That is not a phone number with a country code.")
        return 2
    if not args.agreed:
        print("Not calling: pass --agreed once the owner of the number has asked for this call.")
        return 2
    conn = store.connect()
    case = store.case_for_user(conn, user)
    store.record_consent(conn, case["id"], "phone_call", True)
    webhook = f"{config.PUBLIC_BASE_URL}/api/voice-agent/webhook?token={config.VOICE_AGENT_SECRET}" if (
        config.PUBLIC_BASE_URL and config.VOICE_AGENT_SECRET) else None
    attempt = sarvam.place_outbound_call(
        "+" + user.removeprefix("whatsapp:+"), language=args.language, webhook_url=webhook,
        metadata={"case_id": case["id"]},
    )
    print(f"Calling. Attempt id: {attempt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
