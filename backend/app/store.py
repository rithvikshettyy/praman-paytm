"""SQLite store (PRD-PAYTM C7): cases, documents and consents so far.

Retention default: we keep the fields read from a document, not the document.
The original is written to disk only when the case has an unrevoked
``keep_original`` consent; otherwise it is never stored. The documents in
scope are medical bills and loan agreements, so this is a product decision,
not a nicety.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import config
from app.services.documents import Extraction

CONSENT_SCOPES = (
    "read_documents",  # read any document she sends (OCR and extraction); asked before the first one
    "read_policy",
    "read_bill",
    "read_kfs",
    "store_fields",
    "contact_insurer",
    "keep_original",  # keep the uploaded file itself, not just the fields read from it
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id                TEXT PRIMARY KEY,
    channel_user      TEXT UNIQUE,      -- e.g. whatsapp:+91...; NULL for cases opened over the API
    language          TEXT,             -- e.g. mr-IN; NULL until known
    product           TEXT,             -- health_policy | merchant_loan | motor_policy
    respondent        TEXT,             -- insurer | lender | distributor
    respondent_name   TEXT,             -- e.g. the insurer's legal name
    distributor_owned INTEGER,          -- 1 = the distributor owes the answer
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL,
    doc_type    TEXT NOT NULL,      -- policy | bill | kfs | letter | claim_doc
    slot        TEXT,               -- discharge_summary | bill | id_proof | ...
    received_at TEXT NOT NULL,
    fields      TEXT NOT NULL DEFAULT '{}',
    confidence  REAL,
    retained    INTEGER NOT NULL DEFAULT 0   -- 0 = fields kept, original discarded
);

CREATE TABLE IF NOT EXISTS consents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    scope      TEXT NOT NULL,       -- see CONSENT_SCOPES
    granted    INTEGER NOT NULL,
    at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,       -- coverage_query | escalation
    addressee   TEXT NOT NULL,
    text        TEXT NOT NULL,
    unverified  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'drafted',   -- drafted | approved (approved and ready to send; nothing is sent)
    created_at  TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,       -- see EVENT_KINDS
    detail     TEXT NOT NULL DEFAULT '{}',
    at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS documents_case_idx ON documents (case_id);
CREATE INDEX IF NOT EXISTS consents_case_scope_idx ON consents (case_id, scope);
CREATE INDEX IF NOT EXISTS events_case_kind_idx ON events (case_id, kind);
"""

# The audit trail. Every number on the Distributor Console (N8) is counted
# from these rows; nothing is projected or estimated.
EVENT_KINDS = (
    "case_routed",  # detail: product, grievance_class, respondent, respondent_name, distributor_owned, ladder
    "readiness_checked",  # detail: ladder, outcome
    "claim_stopped",  # detail: rules (the blocks that stopped it), possible_on
    "deduction_explained",  # detail: rule, deduction (rupees or null while pending), pending
    "coverage_query_drafted",
    "escalation_drafted",
    "clock_started",  # detail: ladder, step, started_on, respond_by, verified_by
    "draft_approved",  # detail: draft_id, kind. Approved and ready to send; nothing is sent
    "papers_checked",  # detail: fix (problems an insurer would query), heads_up. No names or amounts
    "answer_given",  # detail: question, status (answered|not_found), source (her_document|sources), pages, answer_en. Redacted
    "answer_feedback",  # detail: answer_id, solved. "Did this solve it?"; the first answer to each question stands
    "agent_requested",  # detail: reason (not_solved|not_found), answer_id. She asked for a person after an answer
    "grievance_reported",  # detail: text (redacted), grievance_class, product. What she wrote when something went wrong
    "case_status",  # detail: status (resolved|pending). An agent's mark on the console; the latest one stands
)
CASE_STATUSES = ("pending", "resolved")

# One row per case for the console: its latest route, verdict and clock, each
# read from the latest event of that kind (never from columns an event did
# not write).
CONSOLE_VIEW = """
DROP VIEW IF EXISTS console_cases;
CREATE VIEW console_cases AS
WITH latest AS (
    SELECT case_id, kind, MAX(id) AS id FROM events GROUP BY case_id, kind
)
SELECT
    c.id                                            AS case_id,
    c.created_at                                    AS created_at,
    COALESCE(c.is_example, 0)                       AS is_example,
    json_extract(r.detail, '$.product')             AS product,
    json_extract(r.detail, '$.grievance_class')     AS grievance_class,
    json_extract(r.detail, '$.respondent')          AS respondent,
    json_extract(r.detail, '$.respondent_name')     AS respondent_name,
    json_extract(r.detail, '$.distributor_owned')   AS distributor_owned,
    json_extract(v.detail, '$.outcome')             AS verdict,
    json_extract(k.detail, '$.step')                AS clock_step,
    json_extract(k.detail, '$.respond_by')          AS clock_respond_by,
    json_extract(k.detail, '$.verified_by')         AS clock_verified_by,
    (SELECT MAX(at) FROM events e WHERE e.case_id = c.id) AS last_event_at,
    EXISTS (SELECT 1 FROM documents d WHERE d.case_id = c.id) AS has_documents,
    EXISTS (SELECT 1 FROM events a WHERE a.case_id = c.id AND a.kind = 'agent_requested') AS agent_requested,
    COALESCE(json_extract(s.detail, '$.status'), 'pending') AS agent_status
FROM cases c
LEFT JOIN latest lr ON lr.case_id = c.id AND lr.kind = 'case_routed'
LEFT JOIN events r  ON r.id = lr.id
LEFT JOIN latest lv ON lv.case_id = c.id AND lv.kind = 'readiness_checked'
LEFT JOIN events v  ON v.id = lv.id
LEFT JOIN latest lk ON lk.case_id = c.id AND lk.kind = 'clock_started'
LEFT JOIN events k  ON k.id = lk.id
LEFT JOIN latest ls ON ls.case_id = c.id AND ls.kind = 'case_status'
LEFT JOIN events s  ON s.id = ls.id;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Columns added to tables after they first shipped (PRD-PAYTM C7). A store
# created before a column existed gets it on the next connect.
MIGRATIONS = {
    "cases": (
        ("product", "TEXT"),
        ("respondent", "TEXT"),
        ("respondent_name", "TEXT"),
        ("distributor_owned", "INTEGER"),
        ("is_example", "INTEGER"),  # 1 = seeded example case for the demo
    ),
}


# Two requests setting up the schema at once (the console fetches its counters and its case list
# together) failed with "view already exists", and a request reading while another dropped the view
# failed with "no such table: console_cases". So the view is only rebuilt when its definition has
# changed, and then as one atomic step, so a reader sees the old view or the new one, never none.
_SETUP_LOCK = threading.Lock()
_VIEW_SQL = "CREATE VIEW " + CONSOLE_VIEW.split("CREATE VIEW", 1)[1].strip().rstrip(";").strip()


def _view_is_current(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'console_cases'").fetchone()
    return row is not None and " ".join(row["sql"].split()) == " ".join(_VIEW_SQL.split())


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the store, creating the file and tables if needed and migrating older ones."""
    path = Path(path or config.STORE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _SETUP_LOCK:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if not _view_is_current(conn):
            conn.executescript(f"BEGIN IMMEDIATE;{CONSOLE_VIEW}COMMIT;")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


# --- Cases -------------------------------------------------------------------


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    return dict(row) if row else None


def ensure_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    """The case with this id, opening it if it does not exist yet."""
    conn.execute("INSERT OR IGNORE INTO cases (id, created_at) VALUES (?, ?)", (case_id, _now()))
    conn.commit()
    return get_case(conn, case_id)


def find_case_for_user(conn: sqlite3.Connection, channel_user: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM cases WHERE channel_user = ?", (channel_user,)).fetchone()
    return dict(row) if row else None


def delete_case(conn: sqlite3.Connection, case_id: str, originals_dir: Path | None = None) -> dict[str, int] | None:
    """Delete everything held for a case: documents, kept originals, consents, events, the case.

    Returns how many rows went from each table, or None if there was no such
    case. Events go too, so the case disappears from every console number.
    """
    if get_case(conn, case_id) is None:
        return None
    deleted = {
        table: conn.execute(f"DELETE FROM {table} WHERE case_id = ?", (case_id,)).rowcount
        for table in ("documents", "consents", "events", "drafts")
    }
    conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
    conn.commit()
    shutil.rmtree(Path(originals_dir or config.ORIGINALS_DIR) / _safe_name(case_id), ignore_errors=True)
    return deleted


def wipe_all(conn: sqlite3.Connection, originals_dir: Path | None = None) -> int:
    """Delete every case and every row that belongs to one, and every kept original.
    For resetting a demo store only. Returns how many cases there were."""
    count = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    for table in ("documents", "consents", "events", "drafts", "cases"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    shutil.rmtree(Path(originals_dir or config.ORIGINALS_DIR), ignore_errors=True)
    return count


def case_for_user(conn: sqlite3.Connection, channel_user: str) -> dict[str, Any]:
    """The case for a messaging user (one open case per sender), opening it on first contact."""
    row = conn.execute("SELECT * FROM cases WHERE channel_user = ?", (channel_user,)).fetchone()
    if row:
        return dict(row)
    case_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO cases (id, channel_user, created_at) VALUES (?, ?, ?)", (case_id, channel_user, _now())
    )
    conn.commit()
    return get_case(conn, case_id)


def set_language(conn: sqlite3.Connection, case_id: str, language: str) -> None:
    conn.execute("UPDATE cases SET language = ? WHERE id = ?", (language, case_id))
    conn.commit()


def set_example(conn: sqlite3.Connection, case_id: str) -> None:
    """Label a case as a seeded example, so every screen can say so."""
    conn.execute("UPDATE cases SET is_example = 1 WHERE id = ?", (case_id,))
    conn.commit()


def set_route(
    conn: sqlite3.Connection,
    case_id: str,
    product: str | None,
    respondent: str | None,
    respondent_name: str | None,
    distributor_owned: bool | None,
) -> None:
    """Record who owes this case an answer. All None clears a previous route."""
    conn.execute(
        "UPDATE cases SET product = ?, respondent = ?, respondent_name = ?, distributor_owned = ? WHERE id = ?",
        (product, respondent, respondent_name, None if distributor_owned is None else int(distributor_owned), case_id),
    )
    conn.commit()


# --- Events ------------------------------------------------------------------


def record_event(
    conn: sqlite3.Connection, case_id: str, kind: str, detail: dict | None = None, at: str | None = None
) -> int:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    cursor = conn.execute(
        "INSERT INTO events (case_id, kind, detail, at) VALUES (?, ?, ?, ?)",
        (case_id, kind, json.dumps(detail or {}, default=str), at or _now()),
    )
    conn.commit()
    return cursor.lastrowid


def case_events(conn: sqlite3.Connection, case_id: str, kind: str, limit: int | None = None) -> list[dict[str, Any]]:
    """A case's events of one kind, oldest first; with ``limit``, the latest few (still oldest first)."""
    rows = conn.execute(
        "SELECT * FROM events WHERE case_id = ? AND kind = ? ORDER BY id DESC" + (" LIMIT ?" if limit else ""),
        (case_id, kind, limit) if limit else (case_id, kind),
    ).fetchall()
    out = []
    for row in reversed(rows):
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        out.append(item)
    return out


def latest_event(conn: sqlite3.Connection, case_id: str, kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM events WHERE case_id = ? AND kind = ? ORDER BY id DESC LIMIT 1", (case_id, kind)
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["detail"] = json.loads(out["detail"])
    return out


# --- Consents ----------------------------------------------------------------


def record_consent(conn: sqlite3.Connection, case_id: str, scope: str, granted: bool, at: str | None = None) -> None:
    """Append a grant or revocation. The latest row for a scope wins."""
    if scope not in CONSENT_SCOPES:
        raise ValueError(f"unknown consent scope {scope!r}")
    conn.execute(
        "INSERT INTO consents (case_id, scope, granted, at) VALUES (?, ?, ?, ?)",
        (case_id, scope, int(bool(granted)), at or _now()),
    )
    conn.commit()


def has_consent(conn: sqlite3.Connection, case_id: str, scope: str) -> bool:
    """True only if the latest row for this scope is a grant. No row means no."""
    row = conn.execute(
        "SELECT granted FROM consents WHERE case_id = ? AND scope = ? ORDER BY id DESC LIMIT 1",
        (case_id, scope),
    ).fetchone()
    return bool(row and row["granted"])


# --- Documents ---------------------------------------------------------------


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "document"


def save_document(
    conn: sqlite3.Connection,
    case_id: str,
    extraction: Extraction,
    *,
    original: bytes | None = None,
    filename: str | None = None,
    slot: str | None = None,
    originals_dir: Path | None = None,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Store what was read from a document; keep the file only with consent.

    ``confidence`` is the weakest confidence among the fields that were read,
    so one number says how much of the row still needs confirming.
    """
    fields = {name: {"value": f.value, "confidence": f.confidence} for name, f in extraction.fields.items()}
    read = [f.confidence for f in extraction.fields.values() if f.value is not None]
    keep = original is not None and has_consent(conn, case_id, "keep_original")

    cursor = conn.execute(
        "INSERT INTO documents (case_id, doc_type, slot, received_at, fields, confidence, retained) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            case_id,
            extraction.doc_type,
            slot,
            received_at or _now(),
            json.dumps(fields, default=str),
            min(read) if read else None,
            int(keep),
        ),
    )
    doc_id = cursor.lastrowid
    if keep:
        folder = Path(originals_dir or config.ORIGINALS_DIR) / _safe_name(case_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{doc_id}-{_safe_name(filename or 'document')}").write_bytes(original)
    conn.commit()
    return get_document(conn, doc_id)


def get_document(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["fields"] = json.loads(out["fields"])
    return out


def case_documents(conn: sqlite3.Connection, case_id: str, doc_types: tuple[str, ...]) -> list[dict[str, Any]]:
    """Documents of these types on the case, newest first, with fields decoded."""
    marks = ",".join("?" for _ in doc_types)
    rows = conn.execute(
        f"SELECT id FROM documents WHERE case_id = ? AND doc_type IN ({marks}) ORDER BY id DESC",
        (case_id, *doc_types),
    ).fetchall()
    return [get_document(conn, row["id"]) for row in rows]


def filled_slots(conn: sqlite3.Connection, case_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT slot FROM documents WHERE case_id = ? AND slot IS NOT NULL", (case_id,)
    ).fetchall()
    return {row["slot"] for row in rows}


def pending_document(conn: sqlite3.Connection, case_id: str, doc_type: str) -> int | None:
    """The latest document of this type still waiting to be placed in a slot."""
    row = conn.execute(
        "SELECT id FROM documents WHERE case_id = ? AND doc_type = ? AND slot IS NULL ORDER BY id DESC LIMIT 1",
        (case_id, doc_type),
    ).fetchone()
    return row["id"] if row else None


def assign_slot(conn: sqlite3.Connection, case_id: str, doc_id: int, slot: str) -> bool:
    """Place a document in a slot. False if no such document belongs to this case."""
    cursor = conn.execute(
        "UPDATE documents SET slot = ? WHERE id = ? AND case_id = ?", (slot, doc_id, case_id)
    )
    conn.commit()
    return cursor.rowcount == 1


# --- Drafts ------------------------------------------------------------------


def save_draft(conn: sqlite3.Connection, case_id: str, kind: str, addressee: str, text: str, unverified: bool) -> dict[str, Any]:
    cursor = conn.execute(
        "INSERT INTO drafts (case_id, kind, addressee, text, unverified, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (case_id, kind, addressee, text, int(unverified), _now()),
    )
    conn.commit()
    return get_draft(conn, case_id, cursor.lastrowid)


def get_draft(conn: sqlite3.Connection, case_id: str, draft_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM drafts WHERE id = ? AND case_id = ?", (draft_id, case_id)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["unverified"] = bool(out["unverified"])
    return out


def case_drafts(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id FROM drafts WHERE case_id = ? ORDER BY id DESC", (case_id,)).fetchall()
    return [get_draft(conn, case_id, row["id"]) for row in rows]


def approve_draft(conn: sqlite3.Connection, case_id: str, draft_id: int) -> dict[str, Any] | None:
    """Mark a draft approved and ready to send. Nothing is sent anywhere."""
    cursor = conn.execute(
        "UPDATE drafts SET status = 'approved', approved_at = COALESCE(approved_at, ?) WHERE id = ? AND case_id = ?",
        (_now(), draft_id, case_id),
    )
    conn.commit()
    return get_draft(conn, case_id, draft_id) if cursor.rowcount == 1 else None
