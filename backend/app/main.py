"""Praman backend - the one process.

Routes stay thin: parse, delegate to a plain function, serialise. Every error
leaves as JSON, and document text never enters a log line.

Run from backend/:  uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
import re
import sys

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app import cases, complaints, config, console, conversation, guided, handoff, readiness, store, voice_agent
from app.clients import sarvam
from app.clients.sarvam import SarvamBadRequest, SarvamUnavailable
from app.core import ladder_engine as le
from app.core import ladders
from app.services import documents, drafts, i18n, n8n, reminders, voice
from app.services.documents import ExtractionFailed, UploadRejected

sys.path.append(str(config.BACKEND_DIR.parent))  # the whatsapp/ package sits beside backend/
from whatsapp import meta as whatsapp_meta  # noqa: E402
from whatsapp import router as whatsapp_router  # noqa: E402

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="[%(asctime)s] [%(levelname)s] %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.rag import news

    if news.start_background():
        logger.info("News layer refreshes every %d minutes.", config.NEWS_REFRESH_MINUTES)
    yield


app = FastAPI(title="Praman", lifespan=lifespan)
# Only the frontend's origin (CORS_ORIGINS, default the Next.js dev server) may call the API from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

for problem in config.validate():
    logger.warning("Configuration: %s", problem)
for name in whatsapp_meta.configured():
    logger.warning("WhatsApp: %s is not set (backend/.env); the WhatsApp channel will not work.", name)
app.include_router(whatsapp_router)


@app.exception_handler(UploadRejected)
async def handle_upload_rejected(_: Request, exc: UploadRejected):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(ExtractionFailed)
async def handle_extraction_failed(_: Request, exc: ExtractionFailed):
    logger.warning("Extraction failed: %s", exc)
    return JSONResponse({"error": "The document could not be read. Try again, or send a clearer photo."}, status_code=502)


@app.exception_handler(SarvamBadRequest)
async def handle_sarvam_bad_request(_: Request, exc: SarvamBadRequest):
    logger.info("Sarvam rejected a request: %s", exc)
    return JSONResponse(
        {"error": "Sarvam rejected the request.", "detail": str(exc)[:300]}, status_code=400
    )


@app.exception_handler(SarvamUnavailable)
async def handle_sarvam_unavailable(_: Request, exc: SarvamUnavailable):
    logger.warning("Sarvam unavailable: %s", exc)
    return JSONResponse(
        {"error": "The Sarvam service is unavailable. Try again shortly."}, status_code=503
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health")
def api_health():
    """Reports what is configured without ever echoing a secret."""
    problems = config.validate()
    return JSONResponse(
        {
            "status": "degraded" if problems else "ok",
            "problems": problems,
            "sarvam_configured": bool(config.SARVAM_API_KEY),
            "chat_model": config.CHAT_MODEL,
            "supported_languages": sorted(config.SUPPORTED_LANGUAGES),
            # Find a policy: off unless both are set; the sites are hostnames, never a secret.
            "policy_search": {"firecrawl": bool(config.FIRECRAWL_API_KEY), "sites": list(config.POLICY_SEARCH_DOMAINS)},
        },
        status_code=503 if problems else 200,
    )


# --- Checklist (PRD-PAYTM N3, Part 4) ----------------------------------------


@app.get("/api/checklist/{case_id}")
def get_checklist(case_id: str):
    conn = store.connect()
    try:
        if store.get_case(conn, case_id) is None:
            return JSONResponse({"error": "No such case."}, status_code=404)
        return cases.checklist_view(cases.checklist_state(conn, case_id))
    finally:
        conn.close()


@app.post("/api/checklist/{case_id}")
def post_checklist(
    case_id: str,
    file: UploadFile | None = File(None),
    slot: str | None = Form(None),
    document_id: int | None = Form(None),
):
    """Attach a photo to a slot.

    file + slot       -> placed where she says
    file alone        -> placed by caption/OCR, or returned with numbered options
    document_id + slot -> a waiting photo placed where she picked
    """
    if file is None and not (document_id is not None and slot):
        return JSONResponse({"error": "Send a file, or a document_id with the slot she picked."}, status_code=400)
    conn = store.connect()
    try:
        store.ensure_case(conn, case_id)
        if file is not None:
            attached = cases.attach(
                conn, case_id, file.file.read(), file.filename or "upload", mime_type=file.content_type, slot=slot
            )
        else:
            cases.choose_slot(conn, case_id, document_id, slot)
            attached = cases.Attached(document_id, slot, needs_choice=False)
        view = cases.checklist_view(cases.checklist_state(conn, case_id))
        view["attached"] = {"document_id": attached.document_id, "slot": attached.slot, "needs_choice": attached.needs_choice}
        if attached.needs_choice:
            view["options"] = cases.options()
        return view
    except cases.UnknownSlot as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    finally:
        conn.close()


@app.get("/media/{name}")
def media(name: str):
    """Spoken replies, for the web chat to fetch. Tokens are random and short-lived in memory."""
    found = voice.get_media(name.rsplit(".", 1)[0])
    if found is None:
        return Response(status_code=404)
    data, content_type = found
    return Response(data, media_type=content_type)


# --- Readiness (PRD-PAYTM Part 4) --------------------------------------------


@app.post("/api/readiness")
def readiness_check(body: dict = Body(...)):
    """Fact sheet in, Verdict out, with its plain-language messages.

    With a case_id the check is logged against the case (the console counts it).
    """
    try:
        facts = cases.facts_from_json(body.get("facts") or {})
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    ladder = ladders.load("insurance_health_claim")
    case_id = body.get("case_id")
    if case_id:
        conn = store.connect()
        try:
            if store.get_case(conn, case_id) is None:
                return JSONResponse({"error": "No such case."}, status_code=404)
            verdict = cases.check_readiness(conn, case_id, facts)
        finally:
            conn.close()
    else:
        verdict = le.evaluate(facts, ladder.rules)
    # C5: any message built on an UNVERIFIED value makes the whole reply carry the badge.
    return readiness.view(verdict, facts, ladder)


# --- Distributor Console (PRD-PAYTM N8) --------------------------------------


@app.get("/api/console/cases")
def console_cases(filter: str | None = None):
    """Case list; filter=needs_paytm | routed_away | resolved. Without a filter, every case not marked resolved."""
    conn = store.connect()
    try:
        return {"cases": console.case_list(conn, filter)}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        conn.close()


@app.post("/api/console/cases/{case_id}/status")
def set_case_status(case_id: str, body: dict = Body(...)):
    """An agent marks a case resolved (it leaves the list) or pending (it comes back)."""
    status = body.get("status")
    if status not in store.CASE_STATUSES:
        return _bad(f"status must be one of {list(store.CASE_STATUSES)}.")
    conn = store.connect()
    try:
        if store.get_case(conn, case_id) is None:
            return _bad("No such case.", 404)
        store.record_event(conn, case_id, "case_status", {"status": status})
    finally:
        conn.close()
    return {"case_id": case_id, "status": status}


@app.get("/api/console/complaints")
def console_complaints(status: str = "pending"):
    """Complaints she asked a person to take on. status=pending (default) | resolved | all."""
    conn = store.connect()
    try:
        return {"complaints": complaints.listing(conn, None if status == "all" else status)}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        conn.close()


@app.post("/api/console/complaints/{complaint_id}/status")
def set_complaint_status(complaint_id: int, body: dict = Body(...)):
    """An agent marks a complaint resolved (it leaves the pending list) or pending (it comes back)."""
    status = body.get("status")
    if status not in complaints.STATUSES:
        return _bad(f"status must be one of {list(complaints.STATUSES)}.")
    conn = store.connect()
    try:
        if not complaints.set_status(conn, complaint_id, status):
            return _bad("No such complaint.", 404)
    finally:
        conn.close()
    return {"id": complaint_id, "status": status}


@app.get("/api/metrics")
def console_metrics():
    """Headline and six counters, every one counted from event rows."""
    conn = store.connect()
    try:
        return console.metrics(conn)
    finally:
        conn.close()


# --- Consent and deletion (PRD-PAYTM N6) -------------------------------------


@app.post("/api/consent")
def consent(body: dict = Body(...)):
    """Grant or revoke one consent scope for a case. The latest answer wins."""
    case_id, scope, granted = body.get("case_id"), body.get("scope"), body.get("granted")
    if not case_id or scope not in store.CONSENT_SCOPES or not isinstance(granted, bool):
        return JSONResponse(
            {"error": f"Send case_id, a scope from {list(store.CONSENT_SCOPES)}, and granted true or false."},
            status_code=400,
        )
    conn = store.connect()
    try:
        store.ensure_case(conn, case_id)
        store.record_consent(conn, case_id, scope, granted)
        return {"case_id": case_id, "scope": scope, "granted": granted}
    finally:
        conn.close()


@app.delete("/api/case/{case_id}")
def delete_case(case_id: str):
    """Delete everything held for a case, including its console numbers."""
    conn = store.connect()
    try:
        deleted = store.delete_case(conn, case_id)
    finally:
        conn.close()
    conversation.forget_documents(case_id)
    if deleted is None:
        return JSONResponse({"error": "No such case."}, status_code=404)
    return {"case_id": case_id, "deleted": deleted}


# --- Web site (the demo front end) -------------------------------------------
# Thin: the chat runs the same conversation as WhatsApp (app/conversation.py).

_SESSION = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _web_user(session_id) -> str | None:
    return f"web:{session_id}" if isinstance(session_id, str) and _SESSION.match(session_id) else None


def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@app.post("/api/session")
def session(body: dict = Body(...)):
    """The case for this browser (one per session id, like one per WhatsApp number)."""
    user = _web_user(body.get("session_id"))
    if user is None:
        return _bad("session_id must be 8 to 64 letters, digits or hyphens.")
    conn = store.connect()
    try:
        return {"case_id": store.case_for_user(conn, user)["id"]}
    finally:
        conn.close()


def _chat_reply(reply: conversation.Reply, speak: bool) -> dict:
    messages = []
    for message in reply.messages:
        text = conversation.render(message, reply.language)
        if message.citations:  # the sources travel as chips under the message, not inside the sentence
            text = conversation.without_citations(text)
        messages.append({
            "text": text,
            "unverified": message.unverified,
            "citations": list(message.citations),
            "audio_url": voice.speak(text, reply.language) if speak else None,
            # "Did this solve it?": the id to answer with, and which question to ask
            "feedback": {"answer_id": message.feedback[0], "kind": message.feedback[1]} if message.feedback else None,
            # quick replies under the message: tapping one sends its id as her next message
            "buttons": [{"id": b[0], "title": conversation.render(conversation.Message(b[1]), reply.language)} for b in message.buttons],
        })
    return {"case_id": reply.case_id, "language": reply.language, "messages": messages}


def _web_journey(conn, user: str) -> str | None:
    """The start option she picked in the web chat, if any (the latest ``journey_chosen`` on her case)."""
    case = store.find_case_for_user(conn, user)
    found = store.latest_event(conn, case["id"], "journey_chosen") if case else None
    return found["detail"].get("journey") if found else None


@app.post("/api/journey")
def choose_journey(body: dict = Body(...)):
    """The start options of the web chat: {session_id, journey: find | check | complain, language?}.
    Remembers the pick for her case and returns the opening message (and, for find, the first question)."""
    user = _web_user(body.get("session_id"))
    journey = body.get("journey")
    if user is None:
        return _bad("session_id must be 8 to 64 letters, digits or hyphens.")
    if journey not in guided.JOURNEYS:
        return _bad(f"journey must be one of {list(guided.JOURNEYS)}.")
    language = body.get("language") if body.get("language") in config.SUPPORTED_LANGUAGES else None
    conn = store.connect()
    try:
        case = store.case_for_user(conn, user)
        if language:
            store.set_language(conn, case["id"], language)
        messages = guided.open_journey(conn, user, case, journey)
        reply = conversation.Reply(case["id"], language or case.get("language") or config.DEFAULT_LANGUAGE, messages)
    finally:
        conn.close()
    return _chat_reply(reply, speak=False)


def _converse(session_id, text: str, language, insurer, product, speak: bool):
    user = _web_user(session_id)
    if user is None:
        return _bad("session_id must be 8 to 64 letters, digits or hyphens.")
    conn = store.connect()
    try:
        reply = conversation.respond(
            conn, user, text, language=language, context={"insurer": insurer, "product": product},
            explain_documents=True, journey=_web_journey(conn, user), guided=True,
        )
    finally:
        conn.close()
    return _chat_reply(reply, speak)


@app.post("/api/feedback")
def feedback(body: dict = Body(...)):
    """Her Yes or No to "Did this solve it?": {session_id, answer_id, solved, language?}.
    A Yes is counted on the console; a No asks for a person, who then sees a brief."""
    user = _web_user(body.get("session_id"))
    answer_id, solved = body.get("answer_id"), body.get("solved")
    if user is None:
        return _bad("session_id must be 8 to 64 letters, digits or hyphens.")
    if not isinstance(answer_id, int) or isinstance(answer_id, bool) or not isinstance(solved, bool):
        return _bad("Send answer_id (a number) and solved (true or false).")
    conn = store.connect()
    try:
        reply, recorded = conversation.give_feedback(conn, user, answer_id, solved, body.get("language"))
        if not recorded and _web_journey(conn, user) == "complain":
            # In the complaint journey a "No" means a person should take it: start registering the complaint.
            case = store.find_case_for_user(conn, user)
            reply = conversation.Reply(reply.case_id, reply.language, guided.escalate(conn, user, case))
    except conversation.UnknownAnswer:
        return _bad("That answer is not one you were given.", 404)
    finally:
        conn.close()
    return {**_chat_reply(reply, speak=False), "solved": recorded}


@app.post("/api/chat")
def chat(body: dict = Body(...)):
    """One chat message: {session_id, text, language, insurer?, product?, speak?}."""
    text = body.get("text").strip() if isinstance(body.get("text"), str) else ""
    if not text:
        return _bad("text is required.")
    return _converse(body.get("session_id"), text, body.get("language"), body.get("insurer"),
                     body.get("product"), bool(body.get("speak")))


@app.post("/api/voice")
def voice_message(
    audio: UploadFile = File(...),
    session_id: str = Form(...),
    language: str | None = Form(None),
    insurer: str | None = Form(None),
    product: str | None = Form(None),
):
    """A recorded voice message: transcribed by Sarvam, answered like chat, and spoken back."""
    transcript = _transcribe(audio, language)
    if isinstance(transcript, JSONResponse):
        return transcript
    result = _converse(session_id, transcript, language, insurer, product, speak=True)
    if isinstance(result, JSONResponse):
        return result
    return {"transcript": transcript, **result}


@app.post("/api/transcribe")
def transcribe(audio: UploadFile = File(...), language: str | None = Form(None)):
    """Speech to text only. The chat shows the words for her to check and send; nothing is answered here."""
    transcript = _transcribe(audio, language)
    if isinstance(transcript, JSONResponse):
        return transcript
    return {"transcript": transcript}


def _transcribe(audio: UploadFile, language: str | None) -> str | JSONResponse:
    data = audio.file.read()
    if not data:
        return _bad("The recording is empty.", 422)
    try:
        heard = sarvam.speech_to_text(data, audio.filename or "voice.webm", language=language)
    except SarvamBadRequest as exc:
        return _bad(f"The recording could not be transcribed: {str(exc)[:200]}")
    transcript = (heard.get("transcript") or "").strip()
    if not transcript:
        return _bad("I could not hear any words in that recording. Please try again.", 422)
    return transcript


@app.post("/api/translate")
def translate_page(body: dict = Body(...)):
    """The website's own words in her language: {language, texts} -> {translations}, same order.

    Translated by Sarvam once per string and language, then served from a disk cache.
    Amounts, percentages and dates are kept exactly; anything unsafe stays in English.
    """
    language, texts = body.get("language"), body.get("texts")
    if language not in config.SUPPORTED_LANGUAGES:
        return _bad(f"language must be one of {sorted(config.SUPPORTED_LANGUAGES)}.")
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        return _bad("texts must be a list of strings.")
    if len(texts) > i18n.MAX_TEXTS or any(len(t) > i18n.MAX_CHARS for t in texts):
        return _bad(f"Send at most {i18n.MAX_TEXTS} texts of up to {i18n.MAX_CHARS} characters each.")
    return {"language": language, "translations": i18n.translate_page(texts, language)}


@app.post("/api/chat/upload")
def chat_upload(
    files: list[UploadFile] = File(...),
    session_id: str = Form(...),
    text: str | None = Form(None),
    language: str | None = Form(None),
    insurer: str | None = Form(None),
    product: str | None = Form(None),
    speak: bool = Form(False),
):
    """Documents sent in the chat, with her message if she wrote one.

    Consent first. Each document is read and kept in memory for questions; her message is
    answered from them, or, when she wrote none (or only what the document is), each is summarised.
    """
    text = (text or "").strip()
    user = _web_user(session_id)
    if user is None:
        return _bad("session_id must be 8 to 64 letters, digits or hyphens.")
    attachments = []
    for upload in files:
        data = upload.file.read()
        try:
            mime = documents.validate_upload(data, upload.filename or "upload", upload.content_type)
        except UploadRejected as exc:
            return _bad(f"{upload.filename or 'That file'}: {exc}")
        attachments.append(conversation.Attachment(
            mime, (lambda d=data: d), caption=text or None, filename=upload.filename,
        ))
    conn = store.connect()
    try:
        reply = conversation.respond(
            conn, user, text, attachments=tuple(attachments), language=language,
            context={"insurer": insurer, "product": product}, explain_documents=True,
            journey=_web_journey(conn, user), guided=True,
        )
    finally:
        conn.close()
    return _chat_reply(reply, speak)


@app.get("/api/policies")
def policies():
    """'My policies': the labelled demo policy, with its room cap worked out by the engine."""
    extraction = documents.fixture_extraction("policy")
    if extraction is None:
        return {"policies": []}
    review = documents.review("policy", extraction.fields)
    facts = le.derive(le.Facts(**review.facts))
    fields = extraction.fields
    return {"policies": [{
        "id": "demo-policy",
        "example": extraction.example,
        "insurer": fields["insurer"].value,
        "product": "health_policy",
        "policy_number": fields["policy_number"].value,
        "policy_start_on": facts.policy_start_on.isoformat() if facts.policy_start_on else None,
        "sum_insured": facts.sum_insured,
        "room_cap_percent": facts.room_cap_percent,
        "room_cap_per_day": facts.room_cap_per_day,
        "co_pay_percent": fields["co_pay_percent"].value,
        "waiting_periods": {
            "pre_existing_months": fields["ped_wait_months"].value,
            "specified_disease_months": fields["specified_disease_wait_months"].value,
        },
        "exclusions": fields["named_exclusions"].value or [],
        "network_status": fields["network_status"].value,
    }]}


@app.post("/api/readiness/documents")
def readiness_documents(
    case_id: str = Form(...),
    demo: bool = Form(False),
    policy: UploadFile | None = File(None),
    bill: UploadFile | None = File(None),
):
    """Policy and bill in (or the labelled demo files), verdict and open questions out."""
    if not demo and policy is None and bill is None:
        return _bad("Send a policy or a bill, or ask for the demo files.")
    conn = store.connect()
    try:
        if store.get_case(conn, case_id) is None:
            return _bad("No such case.", 404)
        if not demo and not store.has_consent(conn, case_id, "read_documents"):
            return JSONResponse(
                {"error": "Consent is needed before any document is read.", "consent_needed": True}, status_code=403
            )
        summaries, read = {}, {}
        for doc_type, upload in (("policy", policy), ("bill", bill)):
            if demo:
                extraction, original = documents.fixture_extraction(doc_type), None
            elif upload is not None:
                original = upload.file.read()
                extraction = documents.extract(original, upload.filename or doc_type, doc_type, mime_type=upload.content_type)
            else:
                continue
            review = documents.review(doc_type, extraction.fields)
            store.save_document(conn, case_id, extraction, original=original, filename=getattr(upload, "filename", None))
            summaries[doc_type] = readiness.document_summary(doc_type, extraction, review)
            read[doc_type] = extraction.fields
        return {"case_id": case_id, "documents": summaries, **readiness.assess(conn, case_id, read)}
    finally:
        conn.close()


@app.get("/api/case/{case_id}")
def case_page(case_id: str):
    conn = store.connect()
    try:
        detail = cases.case_detail(conn, case_id)
    finally:
        conn.close()
    return detail if detail is not None else _bad("No such case.", 404)


@app.get("/api/case/{case_id}/brief")
def case_brief(case_id: str):
    """The agent's brief for a case: problem, facts, documents, what she asked and what Praman answered."""
    conn = store.connect()
    try:
        found = handoff.brief(conn, case_id)
    finally:
        conn.close()
    return found if found is not None else _bad("No such case.", 404)


@app.post("/api/case/{case_id}/draft")
def draft_letter(case_id: str, body: dict = Body(default={})):
    """Draft the next letter, addressed by name, and read it back in her language."""
    conn = store.connect()
    try:
        return {"draft": drafts.compose(conn, case_id, (body or {}).get("language"))}
    except LookupError:
        return _bad("No such case.", 404)
    except drafts.RespondentUnknown as exc:
        return _bad(str(exc), 409)
    finally:
        conn.close()


@app.post("/api/case/{case_id}/draft/{draft_id}/approve")
def approve_draft(case_id: str, draft_id: int):
    """She approves: the letter is approved and ready to send. Nothing is sent."""
    conn = store.connect()
    try:
        return drafts.approve(conn, case_id, draft_id)
    except LookupError:
        return _bad("No such draft on this case.", 404)
    finally:
        conn.close()


# --- Delivery and follow-up through n8n ---------------------------------------
# She sends an approved letter; the n8n workflow delivers it and calls back. The callbacks carry
# the shared secret (x-praman-secret) and are refused without it.


@app.post("/api/case/{case_id}/draft/{draft_id}/send")
def send_draft(case_id: str, draft_id: int):
    """Hand an approved letter to the delivery workflow. It is sent only once the workflow confirms."""
    conn = store.connect()
    try:
        return n8n.dispatch(conn, case_id, draft_id)
    except LookupError:
        return _bad("No such draft on this case.", 404)
    except n8n.NotReady as exc:
        return _bad(str(exc), 409)
    except n8n.NotConfigured as exc:
        return _bad(str(exc), 503)
    except n8n.DeliveryUnavailable as exc:
        return _bad(str(exc), 502)
    finally:
        conn.close()


def _n8n_callback(secret: str | None, body: dict, run):
    if not n8n.authentic(secret):
        return _bad("Not allowed.", 401)
    case_id, draft_id = body.get("case_id"), body.get("draft_id")
    if not isinstance(case_id, str) or not isinstance(draft_id, int):
        return _bad("Send case_id and draft_id.")
    conn = store.connect()
    try:
        return run(conn, case_id, draft_id)
    except LookupError:
        return _bad("No such draft on this case.", 404)
    finally:
        conn.close()


@app.post("/api/n8n/delivered")
def n8n_delivered(body: dict = Body(...), x_praman_secret: str | None = Header(None)):
    """The workflow reports the letter went out; the response carries the clock to wait on."""
    channel = str(body.get("channel") or "unknown")
    return _n8n_callback(x_praman_secret, body, lambda conn, case_id, draft_id: n8n.delivered(conn, case_id, draft_id, channel))


@app.post("/api/n8n/failed")
def n8n_failed(body: dict = Body(...), x_praman_secret: str | None = Header(None)):
    """The workflow gave up after its retries."""
    reason = str(body.get("reason") or "unknown")
    return _n8n_callback(x_praman_secret, body, lambda conn, case_id, draft_id: n8n.failed(conn, case_id, draft_id, reason))


def _nudge(case: dict, text: str) -> None:
    """Tell her on WhatsApp, in her language. A browser session has no channel to push to."""
    user = case.get("channel_user") or ""
    if user.startswith("whatsapp:+"):
        whatsapp_meta.send_text(user.removeprefix("whatsapp:+"), i18n.translate(text, case.get("language") or config.DEFAULT_LANGUAGE))


@app.post("/api/n8n/clock-due")
def n8n_clock_due(body: dict = Body(...), x_praman_secret: str | None = Header(None)):
    """The workflow woke at the end of a response window: stop, wait, escalate, or ask a person."""
    if not n8n.authentic(x_praman_secret):
        return _bad("Not allowed.", 401)
    case_id = body.get("case_id")
    if not isinstance(case_id, str):
        return _bad("Send case_id.")
    conn = store.connect()
    try:
        return n8n.clock_due(conn, case_id, notify=_nudge)
    finally:
        conn.close()


@app.post("/api/n8n/reminder-due")
def n8n_reminder_due(body: dict = Body(...), x_praman_secret: str | None = Header(None)):
    """The workflow woke for one premium-reminder date: send it only if it is still wanted."""
    if not n8n.authentic(x_praman_secret):
        return _bad("Not allowed.", 401)
    case_id, reminder_id, on = body.get("case_id"), body.get("reminder_id"), body.get("on")
    if not (isinstance(case_id, str) and isinstance(reminder_id, str) and isinstance(on, str)):
        return _bad("Send case_id, reminder_id and on.")
    conn = store.connect()
    try:
        return reminders.due(conn, case_id, reminder_id, on)
    finally:
        conn.close()


# --- Phone calls (the Sarvam voice agent) -------------------------------------------
# The agent built in the Sarvam dashboard holds the call and asks this backend for each answer through an
# HTTP tool (bearer VOICE_AGENT_SECRET); when a call ends it posts a webhook (?token=VOICE_AGENT_SECRET).


@app.post("/api/voice-agent/turn")
def voice_agent_turn(
    body: dict = Body(...), authorization: str | None = Header(None), x_praman_secret: str | None = Header(None),
):
    """One thing the caller said: {caller, journey?, text, language?}. Returns what the agent should say."""
    if not voice_agent.authentic(authorization, x_praman_secret):
        return _bad("Not allowed.", 401)
    caller = body.get("caller") or body.get("user_identifier") or body.get("phone")
    conn = store.connect()
    try:
        result = voice_agent.turn(conn, caller, body.get("journey"), body.get("text"), body.get("language"))
    finally:
        conn.close()
    if result is None:
        return _bad("caller must be a phone number.")
    return result


@app.post("/api/voice-agent/webhook")
def voice_agent_webhook(body: dict = Body(...), token: str | None = None, authorization: str | None = Header(None)):
    """A call ended. Only the fact that it happened is kept."""
    if not voice_agent.authentic(authorization, token=token):
        return _bad("Not allowed.", 401)
    conn = store.connect()
    try:
        return {"recorded": voice_agent.record_call(conn, body)}
    finally:
        conn.close()
