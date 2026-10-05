# Praman

**Praman stops avoidable claim rejections before they happen, and sends every question to the party that actually owes the answer.**

*Praman* (प्रमाण) means proof. It is an assistant for Indian policyholders and borrowers, used over WhatsApp or the web, in their own language. Before she files a health insurance claim, it tells her whether the claim will be stopped and, if not, how much will be cut and why. When something has already gone wrong, it works out who owes her the answer (the insurer, the lender or the distributor) and drafts the letter to them by name.

> Prototype built for a hackathon demo. All data in this repository is labelled example data. Praman never files anything. A letter leaves only after she approves it and agrees to it being sent, and then only through n8n (see below).

---

## The hackathon

| | |
|---|---|
| Event | Paytm Build for India hackathon, Mumbai edition |
| Track | AI-Powered Financial Journeys |
| Team | HackOverFlow: Rithvik Shetty, Vinay Pokharkar |
| Build window | 8 hours |
| Deliverable | One live demo, one repo, one video or deck |

The build plan is [`PRD-PAYTM.md`](PRD-PAYTM.md). It describes each change (C1–C7) and each new feature (N1–N9) referenced below.

### The problem

A health claim is usually rejected or cut for reasons that could have been seen in advance:
- The treatment is still inside a waiting period.
- The procedure is excluded.
- The room costs more than the policy's room-rent limit, so part of the bill is cut in proportion.
- Documents are missing.

The policyholder rarely finds out until after she has paid and filed.

A distributor sits in the middle. The customer paid the distributor, so she asks the distributor, even when the insurer or the lender owns the answer. The distributor's support desk becomes a relay, chasing the insurer on her behalf. Every case sent straight to the party that owes the answer is a ticket the distributor never has to handle.

### How Praman answers the track

- **Stops avoidable rejections before they happen.**
  - A tested rule engine checks the claim against her policy and bill before she files.
  - The answer is one of three: file; do not file yet (with the date it becomes possible); or file with a known deduction (with the amount).
- **Sends every question to the right party.**
  - A pure router maps each situation to the insurer, the lender or the distributor, with that party's own escalation ladder and clock.
  - The distributor sees only what is genuinely its own.
- **Shows the distributor its own benefit.**
  - A console lists, from recorded events only, the cases that need the distributor and the complaints waiting for a person. Each case has a one-screen brief for the agent.
  - Nothing is projected. The counts (`GET /api/metrics`) are read from event rows, never estimated.

What Praman does not do: recommend a policy or a lender, predict approval, score credit, or file anything automatically.

---

## The demo (PRD-PAYTM Part 9)

Three minutes, one phone, in Marathi:

1. **Claim readiness.** Her father is about to be admitted. She sends photos of the policy and the hospital bill. Praman replies by voice:
   - the room she was quoted is above her policy's limit;
   - about ₹35,625 will be cut from room, doctor and surgery charges;
   - medicines, tests and implants are not cut;
   - a room within the limit means nothing is cut.

   (Figures are from the example documents in this repo.)
2. **Checklist.** She sends document photos one at a time. Each ticks off a slot, and Praman says, by name, which are still missing.
3. **Leverage.** A second case: a claim rejected for non-disclosure on a policy held for six years.
   - Praman explains the moratorium rule: after five years of continuous cover, a claim cannot be rejected for non-disclosure unless fraud is proved.
   - It reads back a draft to the insurer, addressed by name. She approves it, and the screen says **Approved and ready to send**.
4. **The distributor's view.** The console shows both cases routed to the insurer, a double-debit case under the distributor, and the complaints waiting for a person.
5. **Delete everything.** She types "delete everything" and the case disappears from the console.

`backend/scripts/reset_demo.py` seeds exactly these example cases in about a second.

The same conversation also runs on a **phone call** and starts from three options on web, WhatsApp and phone: *find a policy*, *check my policy*, *complaint*. A pitch deck (23 slides) is at `frontend/public/deck/index.html` (animated, served at `/deck/index.html`) and `frontend/public/deck/Praman-pitch-deck.pptx` (static copy).

---

## How it works

```mermaid
flowchart LR
    WA[WhatsApp via Meta Cloud API] --> CONV
    WEB[Web chat and pages] --> API[FastAPI endpoints] --> CONV
    CONV[Conversation<br/>consent, delete, checklist] --> CLS[Classifier<br/>question / grievance / pre-decision]
    CLS -->|question| RAG[RAG answer<br/>cited, sources only]
    CLS -->|grievance| ROUTE[Respondent router<br/>insurer / lender / distributor]
    DOCS[Policy + bill<br/>Doc AI extraction, confidence gate] --> ENGINE[Rule engine<br/>pure, tested]
    ENGINE --> VERDICT[Verdict + plain messages]
    ROUTE --> DRAFT[Draft to the respondent by name]
    VERDICT --> EVENTS[(Event log)]
    ROUTE --> EVENTS
    DRAFT --> EVENTS
    EVENTS --> CONSOLE[Distributor console]
```

### Principles the code enforces

- **The refusal is a tested function, not a model's opinion.**
  - The rule engine (`backend/app/core/ladder_engine.py`) is pure: no network, no LLM, no randomness, no clock. A test reads its source to prove it.
  - All money and date arithmetic lives there, including the room-cap deduction to the rupee.
  - Every rule has a blocked, a clear and a missing-fact test.
- **A missing fact is never a "no".** Facts default to "not known". A missing fact holds the verdict and becomes a question to her; it never becomes the permissive answer.
- **Nothing is assumed from a document.**
  - Every extracted field carries a confidence, and anything below the gate is asked, not used.
  - A number the model reports that isn't in the document's own text is distrusted.
  - A bill line that can't be placed as "cut" or "never cut" is asked, not guessed.
- **Legal content is verified or badged.**
  - Every rule, regulatory limit and response window carries `verified_by`. Until a named person checks it against the source, it stays `UNVERIFIED`, and anything shown to her that rests on it carries a "Not yet verified" badge.
  - `python backend/scripts/verify_report.py` lists each one with its file and line.
- **Answers come from sources or not at all.**
  - Coverage questions are answered only from her own insurer's documents for her product, or from regulation, with a citation such as `[Example General Insurance, policy_wording, p.2]`.
  - An answer without a valid citation becomes "no source", and the question is handed to the insurer as a written coverage query.
  - Answers never promise approval, and never write to the rule engine's facts.
- **Nothing is filed.** Praman drafts; she approves. An approved letter is "approved and ready to send". Only after she also agrees to Praman contacting the insurer does it go to n8n, with account, Aadhaar, PAN and policy numbers masked. It is marked "sent" only when n8n reports delivery, and that never means the insurer received or accepted it.
- **Privacy by default.**
  - Consent is asked in her language before any document is read.
  - Only the extracted fields are kept, not the file, unless she asks.
  - Aadhaar, PAN, account and policy numbers are masked in every piece of text sent out for AI processing.
  - "Delete everything" deletes the case, its documents, letters and events, including from the console.
- **No invented numbers.** No rejection rates, savings or adoption figures anywhere. Every console number is counted from event rows, and example data is always labelled as an example.

### What is built

| PRD item | What it does | Where |
|---|---|---|
| C1 | Classifier: grievance, question or pre-decision; lending, insurance and platform classes; product | `backend/app/core/agent.py` |
| C2, C4 | Fact sheet, verdict, six rule kinds, required facts per rule | `backend/app/core/ladder_engine.py` |
| C3, N2 | Extraction for policy, bill, letter and loan key fact statement; detecting the document type; confidence gate | `backend/app/services/documents.py` |
| C5 | `verified_by` everywhere, the verification report, badges in every reply | `backend/scripts/verify_report.py` |
| C6 | "Approved and ready to send", never "filed"; enforced by a test | `backend/tests/test_wording.py` |
| C7 | MongoDB store: cases, documents, consents, events, drafts, chat transcript, document text | `backend/app/store.py` |
| N1 | Health-claim readiness ladder: waiting period, exclusion, lapse, room cap, documents, PED cap, moratorium | `backend/data/ladders/insurance_health_claim.yaml` |
| N3 | Document checklist over WhatsApp: photos tick slots, with a numbered-list fallback | `backend/app/cases.py`, `whatsapp/channel.py` |
| N5 | Respondent router with per-respondent ladders and clocks | `backend/app/core/routing.py` |
| N6 | Consent, redaction, delete everything | `backend/app/conversation.py`, `backend/app/services/redact.py` |
| N8 | Distributor console: headline, eleven counters, case list with Resolved or Pending per case, and a one-screen brief for every case | `backend/app/console.py`, `backend/app/handoff.py`, `frontend/app/console` |
| Self-service | "Did this solve it?" under each answer; a Yes is counted as solved without an agent, a No asks for a person and builds the agent's brief | `backend/app/conversation.py`, `frontend/components/ChatWidget.tsx` |
| Coverage Q&A | Answers with citations for questions, in her language | `backend/app/rag/` |
| News layer | Allowlisted RBI and news feeds kept as `news` passages, never verified, never feeding the engine | `backend/app/rag/news.py` |
| Paper checks, bill split | Patient vs insured names, dates, bill total vs lines, items insurers usually do not pay; then the insurer's and her share of the bill | `backend/app/core/ladder_engine.py`, `backend/app/readiness.py` |
| Start options | Find a policy (tap-to-answer questions, options found online, unranked), check my policy, complaint; the same three on web, WhatsApp and phone | `backend/app/guided.py`, `backend/app/services/policy_search.py` |
| Complaints | Registered only on her yes, with how to reach her; Complaints tab in the console | `backend/app/complaints.py`, `frontend/app/console` |
| Delivery and follow-up | Approved letters sent through n8n with consent and redaction; response clock; callbacks | `backend/app/services/n8n.py`, `n8n/` |
| Premium reminders | Email reminders only when she asks, with her consent; "stop reminders" cancels | `backend/app/services/reminders.py`, `n8n/` |
| Phone calls | Sarvam voice agent asks Praman for every answer through one HTTP tool | `backend/app/voice_agent.py`, `voice/` |
| Pitch deck | 23 slides, HTML and PowerPoint | `frontend/public/deck/` |
| Web | Demo site: chat with voice, policies, readiness, checklist, case, console | `frontend/` |

Not built yet, and presented as next steps:
- the lending "fair offer" ruleset (N4; the key-fact-statement extraction schema exists);
- the motor ruleset (N9);
- claim and loan deadline rules (N7). Premium reminders by email are built; deadline rules are not.

### Stack

- **Backend:** Python 3.11 and FastAPI, with MongoDB for storage (local: `docker run -d -p 27017:27017 mongo:7`, or an Atlas `MONGO_URI` in `backend/.env`).
- **AI:** Sarvam for everything:
  - chat and classification;
  - Doc AI to read documents;
  - speech-to-text and text-to-speech;
  - translation and language detection.
- **Coverage questions:** ChromaDB for search, with local MiniLM embeddings or an offline hashing fallback, and PyMuPDF to read PDFs.
- **WhatsApp:** Meta Cloud API (Graph API v26.0).
- **Delivery and reminders:** n8n (Cloud or self-hosted), via two importable workflows in `n8n/`.
- **Phone:** a Sarvam voice agent built in the Sarvam dashboard (setup in `voice/README.md`).
- **Find a policy:** Firecrawl, reading only the sites listed in `POLICY_SEARCH_DOMAINS`.
- **Frontend:** Next.js 16 (App Router), TypeScript and Tailwind CSS v4.

---

## Repository layout

```
whatsapp/         WhatsApp channel (Meta Cloud API): webhook, Graph API client, tests
n8n/              importable workflows (delivery and clock, premium reminders) and their README
voice/            phone-call setup: Sarvam voice agent prompt and tool
backend/
  app/
    core/         rule engine, ladders loader, respondent router, classifier (pure where it matters)
    services/     document extraction, drafts, redaction, voice, translation, block normaliser
    rag/          ingest, retrieve, answer, eval for coverage questions
    conversation.py, guided.py, complaints.py, handoff.py, cases.py, readiness.py, console.py, store.py, voice_agent.py, main.py
  data/
    ladders/      health-claim ladder and escalation steps (every value carries verified_by)
    checklists/   documents a health claim needs
    corpus/       documents the assistant may answer from, listed in sources.yaml
    eval/         golden questions for the RAG eval
    bill_heads.yaml
  scripts/        seed_demo.py, reset_demo.py, verify_report.py
  tests/          1206 tests in total, no network (whatsapp/tests included)
frontend/         Next.js demo site; public/deck holds the pitch deck
PRD-PAYTM.md      the hackathon build plan
CLAUDE.md         standing rules and module map for contributors
```

---

## Running it

### Backend

Run everything from `backend/`.

```sh
cd backend
python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# macOS / Linux:        source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                         # then set SARVAM_API_KEY

uvicorn app.main:app --reload --port 8000   # http://localhost:8000/health
python -m pytest                             # run the test suite (no network, Sarvam is faked)
```

`GET /api/health` reports what is configured (503 until `SARVAM_API_KEY` is set) without echoing secrets.

Legal and regulatory values are marked `UNVERIFIED` until someone checks them by hand. To list each one with its file, line and source:

```sh
python scripts/verify_report.py            # add --strict to fail while any remain
```

### Frontend

Start the backend first, then from `frontend/`:

```sh
cd frontend
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
npm run dev                  # http://localhost:3000
```

The backend accepts browser calls only from `CORS_ORIGINS` (default `http://localhost:3000`). Pages and endpoints are listed in `frontend/README.md`.

### Running the demo

Two terminals:

```sh
# 1. backend/
python scripts/reset_demo.py                 # wipe the demo store, seed the Part 9 example cases, print the headline
uvicorn app.main:app --reload --port 8000

# 2. frontend/
npm run dev
```

Between rehearsals, run `python scripts/reset_demo.py` again (about a second; the server can keep running). It deletes every case in the store, including ones made live. To add the example cases without wiping, use `python scripts/seed_demo.py`.

In `backend/.env`:
- `DISTRIBUTOR_SHORT_NAME` sets how the console headline names the distributor.
- `DISTRIBUTOR_LEGAL_NAME` sets who drafts to the distributor are addressed to.

Without `SARVAM_API_KEY`, the chat can't understand messages or answer questions, so it replies with the document checklist. Documents can still be checked with "Use demo files".

### Coverage questions (RAG)

The dependencies are in `requirements.txt`:
- `pymupdf` reads PDFs page by page.
- `chromadb` keeps the index in `backend/data/index` (git-ignored).

The default embeddings (MiniLM, via Chroma) download about 80 MB once, on first ingest. Set `RAG_EMBEDDINGS=hashing` for an offline index with no download.

1. Put documents under `backend/data/corpus/` (`regulation/`, `insurer/<insurer>/<product>/`, `paytm/`), and list each one in `backend/data/corpus/sources.yaml`.
2. Build or refresh the index (only changed files are re-read):

```sh
python -m app.rag.ingest
```

3. Score answers against `backend/data/eval/golden.yaml` (needs the index and `SARVAM_API_KEY`):

```sh
python -m app.rag.eval
```

The corpus ships with one example policy wording, labelled as an example, so the eval runs out of the box. Replace it with real documents.

### WhatsApp (Meta Cloud API)

All of it lives in `whatsapp/`; the backend mounts it. Its settings sit in the same `backend/.env` as everything else (the `WA_*` block in `backend/.env.example`); there is one env file, and on a host they are ordinary environment variables.

1. At developers.facebook.com, create an app and add the WhatsApp product. Note the phone number id, and create a permanent system-user token (the test token lasts 24 hours).
2. Set `WA_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_APP_SECRET` (App settings, Basic) and `WA_VERIFY_TOKEN` (any string you choose).
3. Expose the backend: `ngrok http 8000`.
4. In WhatsApp, Configuration, set the callback URL to `<ngrok https URL>/api/whatsapp/webhook` and the verify token, then subscribe to `messages`.
5. Message the number: a document gets read and summarised with citations, questions are answered from it, and a voice note is answered in text and by voice.

The webhook refuses any request without a valid `x-hub-signature-256`, and refuses everything if `WA_APP_SECRET` is unset. Run its tests with the backend's: `python -m pytest` from `backend/`.

### n8n and phone calls

- **n8n:** import `n8n/praman-delivery.workflow.json` and `n8n/praman-reminders.workflow.json`, then follow `n8n/README.md` (URLs, shared secret, `PUBLIC_BASE_URL`).
- **Phone:** follow `voice/README.md` to build the agent in the Sarvam dashboard and point its tool at `POST /api/voice-agent/turn`.

---

## Deploying a live demo

Frontend on Vercel, backend on Render, MongoDB on Atlas.

1. **MongoDB Atlas:** create a free cluster, allow `0.0.0.0/0` under Network Access, copy the `mongodb+srv://` string.
2. **Render (Web Service):**
   - Root Directory `backend`; Runtime Python 3.
   - Build Command `pip install -r requirements.txt && python -m app.rag.ingest` (the index is git-ignored, so it is rebuilt on each build).
   - Start Command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`; Health Check Path `/health`.
   - Environment variables: those in `backend/.env.example` that you use, plus `PYTHON_VERSION=3.11.9` and `RAG_EMBEDDINGS=hashing` (MiniLM is too heavy for a 512 MB instance).
3. **Seed the demo cases:** run `python scripts/seed_demo.py` once from `backend/` with `MONGO_URI` set to the Atlas string.
4. **Vercel:** import the repo, Root Directory `frontend`, and set `NEXT_PUBLIC_API_BASE_URL` to the Render URL (no trailing slash). Redeploy after changing it.
5. **Link them:** on Render set `CORS_ORIGINS` to the Vercel URL and `PUBLIC_BASE_URL` to the Render URL. Repoint the n8n callbacks, the WhatsApp webhook and the voice-agent URLs if you use them.
6. **Keep it awake:** Render's free tier sleeps after 15 minutes idle. Point an uptime monitor (for example UptimeRobot, HTTP(s), every 5 minutes) at `https://<service>.onrender.com/health`.

Render's disk is ephemeral: uploaded-document text held in memory, the page-translation cache and the policy-search cache are lost on restart. Cases, events and chat live in MongoDB and survive.

---

## Status and known limits

- **Legal values are unverified.**
  - Every rule, the 36-month pre-existing disease cap, the 60-month moratorium, the room-cap exemptions and the grievance response windows are marked `UNVERIFIED`.
  - They need checking against the IRDAI and RBI primary sources before being relied on. The verification report lists them.
- **Live AI paths need a Sarvam key.** The tests fake Sarvam. The following need a real key to exercise end to end:
  - classification;
  - cited answers to coverage questions;
  - voice replies;
  - speech-to-text on browser recordings.
- **Photos go to Sarvam Doc AI to be read.** Her consent covers this, and the prompt says so. Text sent out is masked, but images cannot be.
- **Some state is held in memory only:** voice notes, photos waiting for her consent, the answers she gives in "find a policy", and the policy-search cache. A server restart drops them.
- **Delivery addresses are examples.** Letters go to the address configured in n8n, not to real insurer grievance desks.
- **Phone calls:** India's DND and calling-consent rules are not handled. Praman has no public endpoint that rings a number.
- **No authentication.** The console and case pages are open, as a demo. They show company names, never her number or name.
- **One example document.** The coverage-question corpus ships with one example policy wording; real insurer wordings and regulations still need adding.

---

## Prior work disclosure

Praman began as an earlier project for analysing loan documents. That project's shared infrastructure was ported into this repository at the start of the build:
- the Sarvam client;
- the Doc AI page-splitting and extraction pipeline;
- the block normaliser;
- the translation cache.

Everything specific to this track was built for this hackathon, and is described in `PRD-PAYTM.md`:
- the rule engine and health-claim ladder;
- document extraction for policies and bills;
- the checklist;
- the respondent router;
- consent, redaction and delete;
- the distributor console;
- coverage questions with citations;
- the demo web front end.

Praman is not affiliated with or endorsed by any insurer, lender or distributor named in the build plan.
