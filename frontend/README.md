# Praman: demo web front end

Next.js (App Router) + TypeScript + Tailwind v4. Mobile first. It only displays what the backend returns: no rules, no money maths, no fallback data. If the API is down, every screen says so.

## Run

The backend must be running first (see the root README). Then, from `frontend/`:

```sh
npm install
cp .env.example .env.local        # NEXT_PUBLIC_API_BASE_URL, default http://localhost:8000
npm run dev                       # http://localhost:3000
```

The backend allows browser calls from `http://localhost:3000` only (`CORS_ORIGINS` in `backend/.env`). Run the site on another origin and the calls will be refused.

For the demo, seed the example cases once from `backend/`: `python scripts/seed_demo.py`.

## Pages

| Path | What it shows | Backend |
|---|---|---|
| `/` | The one-liner, the three services, a link to the console | none |
| `/policies` | The demo policy (labelled as an example) and "Ask about this policy" | `GET /api/policies` |
| `/readiness` | Upload a policy and bill, or use the demo files; the verdict, each rule's message, the deduction by head, open questions, next action; "Before you file": what the insurer would query (name, dates, total, unpaid items) | `POST /api/readiness/documents`, `POST /api/readiness` |
| `/checklist/[caseId]` | The six claim documents, filled or missing, upload per slot | `GET`/`POST /api/checklist/{id}` |
| `/case/[caseId]` | Who owes the answer, the ladder and its clock, the draft read back, Approve, Delete everything | `GET /api/case/{id}`, `POST .../draft`, `POST .../draft/{id}/approve`, `DELETE /api/case/{id}` |
| `/console` | Distributor console: headline, seven counters, case list (all, needs the distributor, routed away). Refreshes every 5 seconds | `GET /api/metrics`, `GET /api/console/cases` |

The chat button on every page talks to `POST /api/chat` (text), `POST /api/transcribe` (a recording becomes words in the text box; she checks them and presses Enter to send) and `POST /api/chat/upload` (documents picked with the paperclip wait as chips above the box and go with her message on Enter, after the consent modal; her question is answered from them, or each is summarised with page citations). All three run the same conversation as the WhatsApp channel. Replies can be played aloud, show their citations, and carry a "Not yet verified" badge when they rest on an unverified source. The language picker in the header translates the whole site with Sarvam (`POST /api/translate`; each string once per language, cached); the chat replies in the same language. Only one voice plays at a time. Closing the chat (×) stops the voice and any recording, drops replies still on their way, and clears the window; her case and documents on the server are kept.

Before the first upload the site asks for consent (`POST /api/consent`). Nothing is read without it. The demo files need none: nothing is read.

## Notes

- One browser is one case: a session id is kept in `localStorage` and `POST /api/session` returns its case.
- Voice uses the browser's `MediaRecorder` (webm or mp4) and Sarvam speech-to-text on the backend. Microphone access needs `localhost` or HTTPS.
- Fonts: Anek Latin, Devanagari, Tamil and Bangla via `next/font`.
- Footer reads "Prototype for demo". Example data is always labelled "Example case".
