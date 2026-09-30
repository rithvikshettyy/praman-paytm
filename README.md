# Praman

Praman stops avoidable claim rejections before they happen, and sends every question to the party that actually owes the answer.

```
backend/    Python API, engine, data, tests (FastAPI)
frontend/   Next.js demo site
PRD-PAYTM.md  build PRD
CLAUDE.md     standing rules and module map
```

## Backend

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

### Coverage questions (RAG)

Answers questions (intent `question`) from her insurer's documents for her product, or from regulation, with citations like `[Example General Insurance, policy_wording, p.2]`. It never writes to the fact sheet and never promises a claim will be paid; anything it cannot source becomes NO_SOURCE and goes to the respondent router.

Dependencies (in `requirements.txt`): `pymupdf` reads PDFs page by page, and `chromadb` keeps the index in `backend/data/index` (git-ignored). The default embeddings (MiniLM, via Chroma) download about 80 MB once, on first ingest. Set `RAG_EMBEDDINGS=hashing` for an offline index with no download.

1. Put documents under `backend/data/corpus/` (`regulation/`, `insurer/<insurer>/<product>/`, `paytm/`) and list each one in `backend/data/corpus/sources.yaml`.
2. Build or refresh the index (only changed files are re-read):

```sh
python -m app.rag.ingest
```

3. Score answers against `backend/data/eval/golden.yaml` (needs the index and `SARVAM_API_KEY`):

```sh
python -m app.rag.eval
```

The corpus ships with one example policy wording, labelled as an example, so the eval runs out of the box. Replace it with real documents.

### WhatsApp (Twilio sandbox)

1. Expose the backend: `ngrok http 8000`, and put the https URL in `.env` as `PUBLIC_BASE_URL`.
2. In `.env`, set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_WHATSAPP_FROM` (the sandbox number, `whatsapp:+14155238886`).
3. In the Twilio console's WhatsApp sandbox settings, set "When a message comes in" to `<PUBLIC_BASE_URL>/api/whatsapp/webhook` (POST).
4. Join the sandbox from the demo phone, then send a document photo. The reply lists what is still missing, as text and as a voice note in her language.

With `TWILIO_AUTH_TOKEN` set, the webhook refuses requests without a valid Twilio signature. `PUBLIC_BASE_URL` must be the exact URL Twilio calls, or every signature check fails.

## Frontend

The demo site (Next.js, TypeScript, Tailwind). Start the backend first, then from `frontend/`:

```sh
cd frontend
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
npm run dev                  # http://localhost:3000
```

The backend accepts browser calls only from `CORS_ORIGINS` (default `http://localhost:3000`). Pages and endpoints are listed in `frontend/README.md`.

## Running the demo

Two terminals:

```sh
# 1. backend/
python scripts/reset_demo.py                 # wipe the demo store, seed the Part 9 example cases, print the headline
uvicorn app.main:app --reload --port 8000

# 2. frontend/
npm run dev
```

Between rehearsals, run `python scripts/reset_demo.py` again (about a second; the server can keep running). It deletes every case in the store, including ones made live. To add the example cases without wiping, use `python scripts/seed_demo.py`.

Set `DISTRIBUTOR_SHORT_NAME` (console headline) and `DISTRIBUTOR_LEGAL_NAME` (who drafts to the distributor are addressed to) in `backend/.env`. Without `SARVAM_API_KEY` the chat cannot classify or answer questions and replies with the document checklist; documents can still be checked with "Use demo files".