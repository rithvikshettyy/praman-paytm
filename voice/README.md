# Phone calls: the Sarvam voice agent

The agent you build in the Sarvam dashboard (indus.sarvam.ai) holds the call: it greets, listens, speaks in
her language and handles interruptions. For every answer it asks Praman through an **HTTP tool**, so the phone is
one more way into the same conversation as the web chat and WhatsApp.

```
She calls the agent's number
  -> Sarvam agent greets her, asks which journey (find a policy / check my policy / complaint)
  -> for each thing she says, the agent calls the tool  POST /api/voice-agent/turn
       {caller, journey, text, language}      (caller = her number, filled in by Sarvam)
  -> Praman runs the conversation (guided journeys, RAG on her documents) and returns {reply}
  -> the agent says the reply out loud
  -> when the call ends, Sarvam posts a webhook; Praman records that it happened (status, length), nothing said
```

She is the case of her number, `whatsapp:+<country code and number>`: the same identity as WhatsApp. A policy
she sent on WhatsApp is the one she is asked about on the phone. If she has sent none, the agent says to send it.

## 1. backend/.env

```
PUBLIC_BASE_URL=https://<your tunnel>          # Sarvam must reach this (same tunnel as n8n)
VOICE_AGENT_SECRET=<a long random string>      # the tool and the webhook are refused without it
# For ringing a phone (scripts/call_me.py); the key defaults to SARVAM_API_KEY:
SARVAM_VOICE_ORG_ID=...
SARVAM_VOICE_WORKSPACE_ID=...
SARVAM_VOICE_APP_ID=...
SARVAM_VOICE_APP_VERSION=1
SARVAM_VOICE_CONNECTION_ID=...
SARVAM_VOICE_AGENT_NUMBER=+91...
```

## 2. The tool (Build > Tools > API Tool)

| Field | Value |
|---|---|
| Name | `praman_answer` |
| Method and URL | `POST {PUBLIC_BASE_URL}/api/voice-agent/turn` |
| Authentication | Bearer token. Store `VOICE_AGENT_SECRET` in the workspace **Secrets**, never in the tool |
| Body (JSON) | `{"caller": "@User Identifier", "journey": "<find, check or complain>", "text": "<what she just said, in her words>", "language": "<the language you are speaking, e.g. Hindi>"}` |
| Timeout | 25 seconds (an answer can take a few seconds) |
| Response template | `{{reply}}` |
| On failure | say: "Sorry, I could not reach my notes just now. Please try again in a moment." |

`journey` and `text` are filled by the agent from the conversation; `caller` is the call-context **User Identifier**.
If your endpoint sits behind a firewall, allow Sarvam's IP `4.213.167.70`.

## 3. Greeting and system prompt

Greeting (in the agent's starting language):

> Hello, I am Praman, an AI assistant. I can help you find a policy, check your policy, or register a complaint.
> Which would you like?

System prompt:

```
You are Praman, an AI voice assistant for insurance in India. Say you are an AI if asked.
Speak the caller's language and keep every turn short: one or two sentences.

Three things you can do. Find out which the caller wants:
1. find a policy -> journey "find". Praman asks the questions; you only pass on what she says.
2. check my policy -> journey "check". She asks about the policy she already sent on WhatsApp or the website.
3. complaint -> journey "complain". She says what went wrong.

For EVERY thing the caller says, call tool:praman_answer with the journey, her words, and the language
you are speaking. Then say the reply from the tool as it is. Do not add facts, numbers, prices, names of
insurers or promises of your own. Never say a claim will be approved or paid. If the tool says it could not
find something, say so; do not guess.
If the caller wants to talk to a person or the complaint is not settled, pass what she said to the tool:
Praman registers the complaint. If she asks for a policy to be checked and the tool says to send it,
tell her to send it on WhatsApp to this number, then call again.
Never ask for Aadhaar, PAN, bank or card numbers.
```

## 4. Webhook (Deploy > the deployment's webhook)

`{PUBLIC_BASE_URL}/api/voice-agent/webhook?token=<VOICE_AGENT_SECRET>`

Sarvam's webhooks carry no signature, so the secret travels in the URL. Praman keeps only that a call
happened (`call_completed`: status, seconds, inbound or outbound). The transcript in the payload is not kept.

## 5. Try it

- Inbound: call the agent's number from a phone, say "find a policy".
- Outbound, to your own phone: `python scripts/call_me.py +91XXXXXXXXXX --language Hindi --agreed`
  (run from `backend/`; `--agreed` records that the owner of the number asked for the call; the number goes to
  Sarvam's calling service). This is a script and not an endpoint on purpose: an open "call this number" endpoint
  could be used to ring strangers.

## What it does and does not do

- The same guardrails as chat: answers come from her document or the rules, find-a-policy options are unranked and
  unverified, nothing is promised, nothing is sent to an insurer.
- A complaint registered on a call offers the number she is calling from as how to reach her, and needs her yes.
- The agent's own words (greeting, language, voice) are Sarvam's; Praman's words are the tool's `reply`.
- Not covered by Sarvam's docs, so decide before outbound campaigns: Do-Not-Disturb rules, calling consent and
  caller-ID requirements in India.
- The webhook's field for the caller's number is not named in Sarvam's docs; Praman looks for the usual names
  and falls back to the `case_id` it passed when it started the call.
