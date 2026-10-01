"""Document intake: validate -> split -> submit Doc AI jobs -> collect -> fields.

Ported from the earlier Praman backend with persistence removed. Nothing here
stores the uploaded file: callers get extracted fields and digitised text back,
and the original bytes are dropped once the jobs are submitted. Keeping the
source document is a consent decision made by the caller, not a default.

The awkward part of Doc AI is the 10-page-per-job cap. A policy wording
routinely runs past it, so splitting is the normal path here, not an edge
case: a document becomes N batches, each batch becomes its own job, and results
are stitched back together with a page offset so page numbers stay absolute.

On top of the pipeline sits per-document-type extraction (PRD-PAYTM C3, N2):
a registry of schemas and fact maps for letter, policy, bill and kfs, a
keyword heuristic that guesses the type, and a confidence gate. A field read
with low confidence is asked, never assumed; a bill line the head map cannot
place is asked, never guessed.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml
from pypdf import PdfReader, PdfWriter

from app import config
from app.clients import sarvam
from app.core import ladder_engine as le
from app.services import blocks as blocks_mod

logger = logging.getLogger(__name__)

# Key terms sit in the opening pages of most documents. Running schema
# extraction over every page of a long wording doubles the page bill for almost
# no recall, so it is capped by default and can be widened per call.
EXTRACT_MAX_BATCHES = 2


class UploadRejected(ValueError):
    """The uploaded file cannot be processed - caller should get a 4xx."""


@dataclass
class Batch:
    index: int
    page_offset: int
    data: bytes
    filename: str


def validate_upload(data: bytes, filename: str, mime_type: str | None) -> str:
    if not data:
        raise UploadRejected("The uploaded file is empty.")
    if len(data) > config.MAX_UPLOAD_BYTES:
        limit_mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise UploadRejected(f"File is larger than the {limit_mb}MB limit.")

    mime = (mime_type or "").split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        mime = _sniff(data, filename)
    if mime not in config.ALLOWED_MIME_TYPES:
        raise UploadRejected(f"Unsupported file type '{mime}'. Upload a PDF or an image.")
    return mime


def _sniff(data: bytes, filename: str) -> str:
    """Identify by magic bytes; the declared content type is caller-controlled."""
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    lowered = filename.lower()
    for ext, mime in (
        (".pdf", "application/pdf"),
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".webp", "image/webp"),
        (".tiff", "image/tiff"),
    ):
        if lowered.endswith(ext):
            return mime
    return "application/octet-stream"


def count_pages(data: bytes, mime_type: str) -> int:
    if mime_type != "application/pdf":
        return 1
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception as exc:
        logger.warning("Could not read PDF page count: %s", exc)
        return 1


def split_into_batches(data: bytes, filename: str, mime_type: str) -> list[Batch]:
    """Split a PDF into batches at or below the Doc AI per-job page cap."""
    cap = max(1, config.DOC_AI_MAX_PAGES_PER_JOB)
    if mime_type != "application/pdf":
        return [Batch(index=0, page_offset=0, data=data, filename=filename)]

    try:
        reader = PdfReader(io.BytesIO(data))
        total = len(reader.pages)
    except Exception as exc:
        logger.warning("PDF unreadable, submitting whole file: %s", exc)
        return [Batch(index=0, page_offset=0, data=data, filename=filename)]

    if total <= cap:
        return [Batch(index=0, page_offset=0, data=data, filename=filename)]

    stem = filename.rsplit(".", 1)[0][:80]
    batches: list[Batch] = []
    for batch_index, start in enumerate(range(0, total, cap)):
        writer = PdfWriter()
        for page_no in range(start, min(start + cap, total)):
            writer.add_page(reader.pages[page_no])
        buf = io.BytesIO()
        writer.write(buf)
        batches.append(
            Batch(
                index=batch_index,
                page_offset=start,
                data=buf.getvalue(),
                filename=f"{stem}_p{start + 1}-{min(start + cap, total)}.pdf",
            )
        )
    logger.info("Split %s (%d pages) into %d Doc AI batches", filename, total, len(batches))
    return batches


def submit(
    data: bytes,
    filename: str,
    *,
    mime_type: str | None = None,
    schema: dict | None = None,
    language: str | None = None,
    extract_max_batches: int = EXTRACT_MAX_BATCHES,
    digitise: bool = True,
) -> list[dict]:
    """Validate a document and submit its Doc AI jobs. Returns the job ledger.

    One digitise job per batch (unless ``digitise`` is False, for a second
    pass over a document already read); schema extraction over the lead
    batches only when a ``schema`` is given.
    """
    mime = validate_upload(data, filename, mime_type)
    batches = split_into_batches(data, filename, mime)
    jobs: list[dict] = []

    try:
        for batch in batches if digitise else ():
            job_id = sarvam.digitise(
                batch.data,
                batch.filename,
                language=language,
                output_format="html",
            )
            jobs.append(_job("digitise", job_id, batch))

        if schema:
            for batch in batches[:extract_max_batches]:
                job_id = sarvam.extract(batch.data, batch.filename, schema, language=language)
                jobs.append(_job("extract", job_id, batch))
    except sarvam.SarvamBadRequest as exc:
        raise UploadRejected(str(exc)) from exc

    return jobs


def _job(kind: str, job_id: str, batch: Batch) -> dict:
    return {
        "kind": kind,
        "job_id": job_id,
        "batch": batch.index,
        "page_offset": batch.page_offset,
        "status": "pending",
    }


# --- Result collection ------------------------------------------------------


def collect(
    jobs: list[dict],
    *,
    numeric_fields: Iterable[str] = (),
    integer_fields: Iterable[str] = (),
) -> dict:
    """Poll every job and return the new state.

    While any job is still running the result is ``{"status": "processing",
    "jobs": [...]}``. Once all are terminal it is ``parsed`` (with text, blocks
    and fields) or ``failed``. Partial success is kept rather than discarded -
    a long wording where two pages failed OCR is still usable.
    """
    jobs = [dict(job) for job in jobs or []]
    if not jobs:
        return {"status": "failed", "error": "No Doc AI jobs recorded.", "jobs": jobs}

    pending = False
    for job in jobs:
        if job.get("status") in sarvam.TERMINAL_STATUSES:
            continue
        try:
            status = sarvam.job_status(job["job_id"])
        except Exception as exc:
            logger.warning("Status check failed for job %s: %s", job.get("job_id"), exc)
            pending = True
            continue

        state = (status.get("status") or "").lower()
        job["status"] = state
        job["usage"] = status.get("usage")
        if not sarvam.is_terminal(state):
            pending = True

    if pending:
        return {"status": "processing", "jobs": jobs}

    return _finalise(jobs, set(numeric_fields), set(integer_fields))


def _finalise(jobs: list[dict], numeric_fields: set[str], integer_fields: set[str]) -> dict:
    digitised: list[blocks_mod.NormalizedDocument] = []
    extracted: dict = {}
    errors: list[str] = []

    for job in jobs:
        if job.get("status") not in sarvam.SUCCESS_STATUSES:
            errors.append(f"{job['kind']} batch {job.get('batch')}: {job.get('status')}")
            continue
        try:
            results = sarvam.job_results(job["job_id"])
        except Exception as exc:
            errors.append(f"{job['kind']} batch {job.get('batch')}: results unavailable ({exc})")
            continue

        if job["kind"] == "digitise":
            for doc in results.get("documents") or []:
                digitised.append(
                    blocks_mod.normalize_pages(
                        doc.get("pages") or [],
                        page_offset=job.get("page_offset", 0),
                        output_format="html",
                    )
                )
        else:
            extracted = _merge_extracted(extracted, _extract_payload(results))

    if not digitised and not extracted:
        return {
            "status": "failed",
            "error": "; ".join(errors)[:1000] or "Doc AI returned no usable output.",
            "jobs": jobs,
        }

    merged = blocks_mod.merge(digitised)
    return {
        "status": "parsed",
        "jobs": jobs,
        "page_count": merged.page_count,
        "text": merged.full_text(),
        "markdown": merged.markdown(),
        "page_blocks": merged.to_page_blocks(),
        "fields": coerce_numbers(extracted, numeric_fields, integer_fields),
        "error": "; ".join(errors)[:1000] or None,
    }


def _extract_payload(results: dict) -> dict:
    """Normalise the several shapes a Doc AI extract result can arrive in."""
    payload = results.get("result")
    if payload is None:
        payload = results.get("annotations")
    if isinstance(payload, str):
        payload = sarvam.parse_json_loose(payload) or {}
    if isinstance(payload, list):
        merged: dict = {}
        for item in payload:
            if isinstance(item, dict):
                merged = _merge_extracted(merged, item)
        payload = merged
    if not isinstance(payload, dict):
        return {}
    # Some responses nest the fields one level down.
    for key in ("fields", "data", "extracted_fields", "result"):
        inner = payload.get(key)
        if isinstance(inner, dict) and len(payload) <= 2:
            return inner
    return payload


def _merge_extracted(base: dict, incoming: dict) -> dict:
    """First non-empty value wins - earlier batches are the authoritative ones."""
    out = dict(base)
    for key, value in (incoming or {}).items():
        if value in (None, "", [], {}):
            continue
        if out.get(key) in (None, "", [], {}):
            out[key] = value
    return out


def coerce_numbers(fields: dict, numeric_fields: set[str], integer_fields: set[str] = frozenset()) -> dict:
    """Make the numeric fields actually numeric.

    Rules compare typed values, so an amount arriving as "₹4,50,000" must not
    silently stay a string. A value that cannot be read becomes ``None`` - a
    missing fact, never a guessed one.
    """
    out = dict(fields or {})
    for key in set(numeric_fields) | set(integer_fields):
        value = out.get(key)
        if value is None or isinstance(value, (int, float)):
            continue
        cleaned = str(value)
        for junk in ("₹", ",", "%", "Rs.", "Rs", "INR", "per annum", "p.a."):
            cleaned = cleaned.replace(junk, "")
        cleaned = cleaned.strip()
        try:
            out[key] = int(cleaned) if key in integer_fields else float(cleaned)
        except (TypeError, ValueError):
            logger.debug("Could not coerce %s=%r to a number", key, value)
            out[key] = None
    return out


# --- Per-document-type extraction (PRD-PAYTM C3, N2) -------------------------

DOC_TYPES = ("letter", "policy", "bill", "kfs")


class ExtractionFailed(RuntimeError):
    """Doc AI could not read the document in time."""


@dataclass(frozen=True)
class FieldSpec:
    """One field to read from a document.

    ``type`` is string | number | integer | date | boolean | string_list |
    line_items. ``fact`` is the ``Facts`` field it feeds, if any.
    ``alternative`` names a field that states the same thing another way, so
    only one of the two needs asking.
    """

    name: str
    type: str
    description: str
    fact: str | None = None
    enum: tuple[str, ...] | None = None
    alternative: str | None = None


_F = FieldSpec

FIELD_SPECS: dict[str, tuple[FieldSpec, ...]] = {
    # A decision letter from an insurer: rejection, partial approval or query.
    "letter": (
        _F("insurer", "string", "Legal name of the insurance company that wrote the letter"),
        _F("letter_date", "date", "Date printed on the letter"),
        _F("claim_number", "string", "Claim number or claim reference as printed"),
        _F("policy_number", "string", "Policy number as printed"),
        _F("decision", "string", "What the insurer decided about the claim",
           enum=("rejected", "partially_approved", "query_raised", "approved")),
        _F("denial_reason", "string", "The category of the reason given for rejecting or cutting the claim",
           fact="denial_reason",
           enum=("non_disclosure", "waiting_period", "exclusion", "documents", "other")),
        _F("amount_claimed", "number", "Amount claimed in Indian rupees, digits only"),
        _F("amount_approved", "number", "Amount approved in Indian rupees, digits only"),
        _F("reason_text", "string", "The reason for the decision, quoted exactly as printed"),
    ),
    # N2: a health insurance policy schedule or wording.
    "policy": (
        _F("insurer", "string", "Legal name of the insurance company"),
        _F("policy_number", "string", "Policy number as printed"),
        _F("policy_start_date", "date", "Start date of the current policy period",
           fact="policy_start_on"),
        _F("continuous_cover_start", "date",
           "Date continuous cover began if the policy was ported or renewed without a break; "
           "leave empty if not stated", fact="continuous_cover_since"),
        _F("sum_insured", "number", "Sum insured in Indian rupees, digits only", fact="sum_insured"),
        _F("room_rent_limit_amount", "number",
           "Room rent limit per day in Indian rupees, only if stated as a rupee amount",
           fact="room_cap_per_day", alternative="room_rent_limit_percent"),
        _F("room_rent_limit_percent", "number",
           "Room rent limit per day as a percentage of the sum insured, only if stated as a percentage",
           fact="room_cap_percent", alternative="room_rent_limit_amount"),
        _F("co_pay_percent", "number", "Co-payment percentage the policyholder pays on each claim"),
        _F("specified_disease_wait_months", "integer",
           "Waiting period in months for specified diseases or procedures"),
        _F("ped_wait_months", "integer", "Waiting period in months for pre-existing diseases",
           fact="ped_wait_months"),
        _F("named_exclusions", "string_list", "Treatments or conditions the policy lists as permanently excluded"),
        _F("network_status", "string", "What the policy says about cashless treatment at network hospitals"),
    ),
    # A hospital bill; line items are grouped into heads by data/bill_heads.yaml.
    "bill": (
        _F("hospital_name", "string", "Name of the hospital that issued the bill"),
        _F("admission_date", "date", "Date of admission", fact="treatment_on"),
        _F("discharge_date", "date", "Date of discharge"),
        _F("room_rent_per_day", "number", "Room rent charged per day in Indian rupees, digits only",
           fact="room_quoted_per_day"),
        _F("line_items", "line_items", "Every charge line on the bill with its amount"),
        _F("bill_total", "number", "Total bill amount in Indian rupees, digits only"),
    ),
    # A loan key fact statement (N4, stretch: schema only).
    "kfs": (
        _F("lender_name", "string", "Full legal name of the lending bank or NBFC, not the app's name"),
        _F("sanctioned_amount", "number", "Sanctioned loan amount in Indian rupees, digits only",
           fact="sanctioned_amount"),
        _F("processing_fee", "number", "Processing fee in Indian rupees, digits only", fact="processing_fee"),
        _F("other_charges", "number", "All other upfront charges in Indian rupees, digits only"),
        _F("net_disbursal", "number", "Amount actually disbursed to the borrower in Indian rupees",
           fact="net_disbursal"),
        _F("apr_percent", "number", "Annual Percentage Rate as a percentage"),
        _F("instalment_amount", "number", "Amount of each instalment in Indian rupees",
           fact="instalment_amount"),
        _F("instalment_count", "integer", "Number of instalments", fact="instalment_count"),
        _F("total_repayable", "number", "Total amount to be repaid in Indian rupees",
           fact="total_repayable"),
        _F("late_fee", "string", "Late payment charges exactly as printed"),
        _F("foreclosure_charge", "string", "Foreclosure or prepayment charges exactly as printed"),
        _F("cooling_off_days", "integer", "Cooling-off or look-up period in days"),
        _F("insurance_bundled", "boolean", "Whether an insurance product is bundled with the loan",
           fact="insurance_bundled"),
        _F("insurance_opted_in", "boolean", "Whether the borrower explicitly opted in to that insurance",
           fact="insurance_consented"),
    ),
}

_SPECS_BY_NAME = {doc_type: {s.name: s for s in specs} for doc_type, specs in FIELD_SPECS.items()}


def _value_schema(spec: FieldSpec) -> dict:
    if spec.type == "date":
        return {"type": "string", "description": f"{spec.description}, in YYYY-MM-DD format"}
    if spec.type == "string_list":
        return {
            "type": "array",
            "description": spec.description,
            "items": {"type": "string", "description": "One item, exactly as printed"},
        }
    if spec.type == "line_items":
        return {
            "type": "array",
            "description": spec.description,
            "items": {
                "type": "object",
                "description": "One charge line of the bill",
                "properties": {
                    "description": {"type": "string", "description": "The line's label, exactly as printed"},
                    "amount": {"type": "number", "description": "The line's amount in Indian rupees, digits only"},
                },
            },
        }
    schema = {"type": spec.type, "description": spec.description}
    if spec.enum:
        schema["enum"] = list(spec.enum)
    return schema


def _schema(specs: tuple[FieldSpec, ...]) -> dict:
    """A Doc AI extraction schema: flat values plus one confidence per value.

    Doc AI wants a root object, a description on every field and nesting no
    deeper than four levels, which is why confidences sit in their own object
    rather than wrapping each value.
    """
    return {
        "type": "object",
        "properties": {
            **{spec.name: _value_schema(spec) for spec in specs},
            "confidence": {
                "type": "object",
                "description": (
                    "For each field above, a number from 0 to 1 saying how clearly the document "
                    "states it. Use 0 when the field is not on the document."
                ),
                "properties": {
                    spec.name: {"type": "number", "description": f"Confidence for {spec.name}, from 0 to 1"}
                    for spec in specs
                },
            },
        },
    }


EXTRACT_SCHEMAS: dict[str, dict] = {doc_type: _schema(specs) for doc_type, specs in FIELD_SPECS.items()}
FACT_MAPS: dict[str, dict[str, str]] = {
    doc_type: {spec.name: spec.fact for spec in specs if spec.fact}
    for doc_type, specs in FIELD_SPECS.items()
}


# --- Detecting the document type ---------------------------------------------

# Checked in this order; on a tie the earlier type wins. No model involved:
# cheap, testable, and wrong-answer-safe because low-confidence fields are
# confirmed with the user anyway.
_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "kfs": ("key fact statement", "key facts statement", "annual percentage rate", "cooling-off", "cooling off period"),
    "policy": ("sum insured", "waiting period", "policy schedule", "policy wording", "pre-existing disease", "co-payment"),
    "bill": ("final bill", "interim bill", "room charges", "pharmacy", "bill no", "inpatient bill", "discharge bill"),
    "letter": ("we regret", "repudiat", "claim has been rejected", "claim is rejected", "dear"),
}


def detect_doc_type(text: str) -> str:
    """Guess letter | policy | bill | kfs from a document's text (ideally its first page)."""
    lowered = (text or "").lower()
    scores = {
        doc_type: sum(1 for kw in keywords if re.search(r"\b" + re.escape(kw), lowered))
        for doc_type, keywords in _TYPE_KEYWORDS.items()
    }
    best = max(scores.values())
    if best == 0:
        return "letter"
    return next(doc_type for doc_type in _TYPE_KEYWORDS if scores[doc_type] == best)


# --- Normalising what Doc AI read --------------------------------------------


@dataclass(frozen=True)
class Field:
    value: Any
    confidence: float


# A number the model reports that cannot be found in the document's own text
# is capped here, well below any sensible gate.
_UNSEEN_CAP = 0.3
_NUMBER_JUNK = re.compile(r"₹|rs\.?|inr|,|\s|%|per annum|p\.a\.|months?|days?", re.IGNORECASE)
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y")


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(_NUMBER_JUNK.sub("", str(raw)))
    except ValueError:
        return None


def _coerce(spec: FieldSpec, raw: Any) -> Any:
    """Type a raw value, or return None when it cannot be read. Never guesses."""
    if raw is None or (isinstance(raw, (str, list)) and not raw):
        return None
    kind = spec.type
    if kind in ("number", "integer"):
        number = _number(raw)
        if number is None:
            return None
        if kind == "integer":
            return int(number) if number.is_integer() else None
        return number
    if kind == "date":
        if isinstance(raw, date):
            return raw
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(str(raw).strip(), fmt).date()
            except ValueError:
                continue
        return None
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw
        return {"yes": True, "true": True, "no": False, "false": False}.get(str(raw).strip().lower())
    if kind == "string_list":
        items = raw if isinstance(raw, list) else [raw]
        return [str(item).strip() for item in items if str(item).strip()] or None
    if kind == "line_items":
        if not isinstance(raw, list):
            return None
        lines = []
        for item in raw:
            if isinstance(item, dict) and str(item.get("description") or "").strip():
                lines.append({"description": str(item["description"]).strip(), "amount": _number(item.get("amount"))})
        return lines or None
    text = str(raw).strip()
    if spec.enum:
        text = text.lower()
        return text if text in spec.enum else None
    return text or None


def _confidence(raw: Any) -> float:
    if isinstance(raw, bool):
        return 0.0
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def _appears(number: float, text: str) -> bool:
    """Whether a number is written in the text, ignoring digit grouping (5,00,000)."""
    joined = re.sub(r"(?<=\d)[,\s](?=\d)", "", text)
    written = str(int(number)) if float(number).is_integer() else f"{number:g}"
    return re.search(rf"(?<![\d.]){re.escape(written)}(?![\d])", joined) is not None


def normalise_fields(doc_type: str, payload: dict, text: str | None = None) -> dict[str, Field]:
    """Type every schema field and attach its confidence.

    A missing or unreadable value has confidence 0. A missing confidence is 0,
    never "sure". When the document text is known, a number that does not
    appear in it is capped below the gate.
    """
    payload = payload if isinstance(payload, dict) else {}
    confidences = payload.get("confidence") if isinstance(payload.get("confidence"), dict) else {}
    out: dict[str, Field] = {}
    for spec in FIELD_SPECS[doc_type]:
        value = _coerce(spec, payload.get(spec.name))
        confidence = _confidence(confidences.get(spec.name)) if value is not None else 0.0
        if value is not None and text is not None and spec.type in ("number", "integer") and not _appears(value, text):
            confidence = min(confidence, _UNSEEN_CAP)
        out[spec.name] = Field(value, confidence)
    return out


# --- Bill heads --------------------------------------------------------------


@dataclass(frozen=True)
class BillHeads:
    deductible: tuple[str, ...]
    exempt: tuple[str, ...]
    verified_by: str
    source: str | None = None


@lru_cache(maxsize=None)
def load_bill_heads(path: Path | None = None) -> BillHeads:
    raw = yaml.safe_load(Path(path or config.BILL_HEADS_PATH).read_text(encoding="utf-8"))
    verified_by = raw.get("verified_by") if isinstance(raw, dict) else None
    if not isinstance(verified_by, str) or not verified_by.strip():
        raise ValueError("bill_heads.yaml needs verified_by (UNVERIFIED until checked)")
    heads = {}
    for head in le.BILL_HEADS:
        words = raw.get(head)
        if not isinstance(words, list) or not words or not all(isinstance(w, str) and w.strip() for w in words):
            raise ValueError(f"bill_heads.yaml needs a non-empty keyword list for {head!r}")
        heads[head] = tuple(w.strip().lower() for w in words)
    overlap = set(heads["deductible"]) & set(heads["exempt"])
    if overlap:
        raise ValueError(f"bill_heads.yaml lists {sorted(overlap)} under both heads")
    return BillHeads(heads["deductible"], heads["exempt"], verified_by.strip(), raw.get("source"))


@lru_cache(maxsize=None)
def _keyword(word: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(word)}(?:s|es)?\b")


def head_for(description: str, heads: BillHeads) -> str | None:
    """deductible | exempt for a bill line, or None when no head, or both, match."""
    lowered = description.lower()
    matched = {
        head
        for head, words in (("deductible", heads.deductible), ("exempt", heads.exempt))
        if any(_keyword(word).search(lowered) for word in words)
    }
    return matched.pop() if len(matched) == 1 else None


def place_bill_lines(
    lines: list[dict], heads: BillHeads, answers: dict[str, str] | None = None
) -> tuple[list[tuple[str, float]], tuple[str, ...]]:
    """Put each line in a head. Returns (placed lines, descriptions still to ask).

    ``answers`` holds the user's own placement of lines the map could not
    place, keyed by the line's description.
    """
    answers = dict(answers or {})
    for description, head in answers.items():
        if head not in le.BILL_HEADS:
            raise ValueError(f"{description!r}: answer must be one of {le.BILL_HEADS}, not {head!r}")
    placed, unplaced = [], []
    for line in lines:
        head = answers.get(line["description"]) or head_for(line["description"], heads)
        if head is None or line.get("amount") is None:
            unplaced.append(line["description"])
        else:
            placed.append((head, line["amount"]))
    return placed, tuple(unplaced)


# --- The confidence gate -----------------------------------------------------


@dataclass(frozen=True)
class Review:
    facts: dict[str, Any]  # ready for Facts(**facts): only trusted values
    to_confirm: tuple[str, ...] = ()  # read, but below the gate: ask "is it X?"
    missing: tuple[str, ...] = ()  # needed, but not on the document: ask "what is X?"
    unmapped_lines: tuple[str, ...] = ()  # bill lines no head could be found for


def review(
    doc_type: str,
    fields: dict[str, Field],
    *,
    confirmed: dict[str, Any] | None = None,
    answers: dict[str, str] | None = None,
    heads: BillHeads | None = None,
    gate: float | None = None,
) -> Review:
    """Decide which extracted values may reach the engine.

    A value reaches ``facts`` only if its confidence clears the gate or the
    user confirmed it (``confirmed`` maps field name to the value she gave,
    which replaces what was read). Everything else is asked.
    """
    gate = config.CONFIDENCE_GATE if gate is None else gate
    confirmed = dict(confirmed or {})
    unknown = set(confirmed) - set(fields)
    if unknown:
        raise KeyError(f"cannot confirm fields this document does not have: {sorted(unknown)}")

    specs = _SPECS_BY_NAME[doc_type]
    fact_map = FACT_MAPS[doc_type]
    facts: dict[str, Any] = {}
    to_confirm: list[str] = []
    missing: list[str] = []
    trusted_lines = None

    for name, extracted in fields.items():
        spec = specs[name]
        if name not in fact_map and spec.type != "line_items":
            continue  # shown to the user, never fed to the engine
        if name in confirmed:
            value, trusted = _coerce(spec, confirmed[name]), True
        else:
            value, trusted = extracted.value, extracted.confidence >= gate
        if value is None:
            alternative = fields.get(spec.alternative) if spec.alternative else None
            if not (alternative and (alternative.value is not None or spec.alternative in confirmed)):
                missing.append(name)
        elif not trusted:
            to_confirm.append(name)
        elif spec.type == "line_items":
            trusted_lines = value
        else:
            facts[fact_map[name]] = value

    unmapped: tuple[str, ...] = ()
    if trusted_lines is not None:
        placed, unmapped = place_bill_lines(trusted_lines, heads or load_bill_heads(), answers)
        if not unmapped:
            totals = le.bill_head_totals(placed)
            facts["bill_deductible_heads"] = totals["deductible"]
            facts["bill_exempt_heads"] = totals["exempt"]

    return Review(facts, tuple(to_confirm), tuple(missing), unmapped)


# --- Extraction end to end ---------------------------------------------------


@dataclass(frozen=True)
class Extraction:
    doc_type: str
    detected: bool  # True when the type came from detect_doc_type, not the caller
    source: str  # doc_ai | fixture
    fields: dict[str, Field] = field(default_factory=dict)
    example: str | None = None  # set on fixture data: it is an example case, not a real document


def _load_fixture(doc_type: str) -> dict | None:
    path = config.FIXTURES_DIR / f"{doc_type}_demo.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_extraction(doc_type: str) -> Extraction | None:
    """The labelled demo document for ``doc_type``, read as if Doc AI had read it. None if there is none."""
    fixture = _load_fixture(doc_type)
    if fixture is None:
        return None
    payload = {**fixture.get("fields", {}), "confidence": fixture.get("confidence", {})}
    return Extraction(doc_type, False, "fixture", normalise_fields(doc_type, payload), example=fixture.get("example"))


def group_bill_lines(lines: list[dict], heads: BillHeads | None = None) -> dict[str, list[dict]]:
    """Bill lines by head, for showing her which charges a room-cap cut can touch."""
    heads = heads or load_bill_heads()
    grouped: dict[str, list[dict]] = {"deductible": [], "exempt": [], "unmapped": []}
    for line in lines or []:
        head = head_for(line["description"], heads) if line.get("amount") is not None else None
        grouped[head or "unmapped"].append({"description": line["description"], "amount": line.get("amount")})
    return grouped


def _wait(jobs: list[dict], timeout: float, poll: float, sleep: Callable[[float], None]) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        result = collect(jobs)
        if result["status"] == "parsed":
            return result
        if result["status"] == "failed":
            raise ExtractionFailed(result.get("error") or "Doc AI could not read the document.")
        if time.monotonic() >= deadline:
            raise ExtractionFailed("Doc AI did not finish reading the document in time.")
        jobs = result["jobs"]
        sleep(poll)


def _first_page_text(result: dict) -> str:
    blocks = result.get("page_blocks") or []
    if not blocks:
        return (result.get("text") or "")[:5000]
    first = min(block["page_number"] for block in blocks)
    return "\n".join(block["text"] for block in blocks if block["page_number"] == first)


def read_text(
    data: bytes,
    filename: str,
    *,
    mime_type: str | None = None,
    language: str | None = None,
    timeout: float | None = None,
    poll_interval: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """OCR only: the text of the first page. Used to place a photo in a checklist slot."""
    timeout = config.DOC_AI_TIMEOUT_SECONDS if timeout is None else timeout
    poll = config.DOC_AI_POLL_SECONDS if poll_interval is None else poll_interval
    result = _wait(submit(data, filename, mime_type=mime_type, language=language), timeout, poll, sleep)
    return _first_page_text(result)


def read_pages(
    data: bytes,
    filename: str,
    *,
    mime_type: str | None = None,
    timeout: float | None = None,
    poll_interval: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[int, str]]:
    """OCR only: every page's text as (page number, text), for answering her questions from it."""
    timeout = config.DOC_AI_TIMEOUT_SECONDS if timeout is None else timeout
    poll = config.DOC_AI_POLL_SECONDS if poll_interval is None else poll_interval
    result = _wait(submit(data, filename, mime_type=mime_type), timeout, poll, sleep)
    pages: dict[int, list[str]] = {}
    for block in result.get("page_blocks") or []:
        pages.setdefault(int(block["page_number"]), []).append(block["text"])
    if not pages and (result.get("text") or "").strip():
        return [(1, result["text"])]
    return [(page, "\n".join(texts)) for page, texts in sorted(pages.items()) if "".join(texts).strip()]


def extract(
    data: bytes,
    filename: str,
    doc_type: str | None = None,
    *,
    mime_type: str | None = None,
    language: str | None = None,
    timeout: float | None = None,
    poll_interval: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Extraction:
    """Read one document into typed fields with confidences.

    With ``doc_type`` given, digitise and extract run together. Without it,
    the document is digitised first, its type detected from the first page,
    then extracted with that type's schema. The caller holds the original
    bytes; this function keeps nothing.

    With ``USE_DOC_FIXTURES`` on and a fixture for the type, the fixture is
    returned instead, marked ``source="fixture"`` with its example label.
    """
    if doc_type is not None and doc_type not in DOC_TYPES:
        raise UploadRejected(f"Unknown document type {doc_type!r}. Expected one of {DOC_TYPES}.")

    if config.USE_DOC_FIXTURES and doc_type is not None:
        demo = fixture_extraction(doc_type)
        if demo is not None:
            return demo

    timeout = config.DOC_AI_TIMEOUT_SECONDS if timeout is None else timeout
    poll = config.DOC_AI_POLL_SECONDS if poll_interval is None else poll_interval
    detected = doc_type is None

    if detected:
        digitised = _wait(submit(data, filename, mime_type=mime_type, language=language), timeout, poll, sleep)
        doc_type = detect_doc_type(_first_page_text(digitised))
        jobs = submit(
            data, filename, mime_type=mime_type, schema=EXTRACT_SCHEMAS[doc_type],
            language=language, digitise=False,
        )
        result = _wait(jobs, timeout, poll, sleep)
        text = digitised.get("text")
    else:
        jobs = submit(data, filename, mime_type=mime_type, schema=EXTRACT_SCHEMAS[doc_type], language=language)
        result = _wait(jobs, timeout, poll, sleep)
        text = result.get("text")

    return Extraction(doc_type, detected, "doc_ai", normalise_fields(doc_type, result.get("fields") or {}, text=text))
