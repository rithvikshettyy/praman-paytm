"""Praman backend - the one process.

Routes stay thin: parse, delegate to a plain function, serialise. Every error
leaves as JSON, and document text never enters a log line.

Run from backend/:  uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import re
import sys

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app import cases, config, console, conversation, readiness, store
from app.channels import whatsapp
from app.clients import sarvam
from app.clients.sarvam import SarvamBadRequest, SarvamUnavailable
from app.core import ladder_engine as le
from app.core import ladders
from app.services import documents, drafts, voice
from app.services.documents import ExtractionFailed, UploadRejected

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="[%(asctime)s] [%(levelname)s] %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Praman")
# Only the frontend's origin (CORS_ORIGINS, default the Next.js dev server) may call the API from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

for problem in config.validate():
    logger.warning("Configuration: %s", problem)
if not config.TWILIO_AUTH_TOKEN:
    logger.warning("TWILIO_AUTH_TOKEN is not set: the WhatsApp webhook accepts unsigned requests.")


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


# --- WhatsApp (Twilio) -------------------------------------------------------


@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook(request: Request, background: BackgroundTasks):
    """Answer Twilio at once; the reply goes out from a background task."""
    params = {key: str(value) for key, value in (await request.form()).items()}
    if config.TWILIO_AUTH_TOKEN:
        # Twilio signs the public URL it called, not the one a tunnel forwards to.
        base = config.PUBLIC_BASE_URL or f"{request.url.scheme}://{request.url.netloc}"
        url = f"{base}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
        if not whatsapp.valid_signature(url, params, request.headers.get("X-Twilio-Signature", ""), config.TWILIO_AUTH_TOKEN):
            logger.warning("Refused a WhatsApp webhook with a bad signature")
            return Response(status_code=403)
    background.add_task(whatsapp.process, whatsapp.parse_inbound(params))
    return Response("<Response/>", media_type="text/xml")


@app.get("/media/{name}")
def media(name: str):
    """Spoken replies, for Twilio and the web chat to fetch. Tokens are random and short-lived in memory."""
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
    """Case list; filter=needs_paytm | routed_away."""
    conn = store.connect()
    try:
        return {"cases": console.case_list(conn, filter)}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        conn.close()


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
        messages.append({
            "text": text,
            "unverified": message.unverified,
            "citations": list(message.citations),
            "audio_url": voice.speak(text, reply.language) if speak else None,
        })
    return {"case_id": reply.case_id, "language": reply.language, "messages": messages}


def _converse(session_id, text: str, language, insurer, product, speak: bool):
    user = _web_user(session_id)
    if user is None:
        return _bad("session_id must be 8 to 64 letters, digits or hyphens.")
    conn = store.connect()
    try:
        reply = conversation.respond(
            conn, user, text, language=language, context={"insurer": insurer, "product": product}
        )
    finally:
        conn.close()
    return _chat_reply(reply, speak)


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
    result = _converse(session_id, transcript, language, insurer, product, speak=True)
    if isinstance(result, JSONResponse):
        return result
    return {"transcript": transcript, **result}


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
        summaries, facts = {}, {}
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
            facts.update(review.facts)
        state = cases.checklist_state(conn, case_id)
        facts["documents_required"] = state.required
        if state.collected:
            facts["documents_collected"] = state.collected
        fact_sheet = le.Facts(**facts)
        verdict = cases.check_readiness(conn, case_id, fact_sheet)
        ladder = ladders.load("insurance_health_claim")
        return {"case_id": case_id, "documents": summaries, **readiness.view(verdict, fact_sheet, ladder)}
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
