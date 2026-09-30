"""PRD-PAYTM N6 over the API: consent before reading, and delete that deletes."""

import pytest
from fastapi.testclient import TestClient

from app import config, console, store
from app.main import app
from app.services import documents
from app.services.documents import Extraction, Field

client = TestClient(app)
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16
DISCHARGE = "DISCHARGE SUMMARY\nDate of discharge: 08/10/2026"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(documents, "read_text", lambda *a, **k: DISCHARGE)
    return tmp_path


def connect():
    return store.connect()


# --- Consent before reading --------------------------------------------------


def test_without_consent_a_photo_is_never_read(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the photo must not be read without consent")

    monkeypatch.setattr(documents, "read_text", boom)
    body = client.post("/api/checklist/c1", files={"file": ("p.png", PNG, "image/png")}).json()
    assert body["attached"]["needs_choice"] is True


def test_with_consent_a_photo_is_read_and_placed():
    assert client.post("/api/consent", json={"case_id": "c1", "scope": "read_documents", "granted": True}).status_code == 200
    body = client.post("/api/checklist/c1", files={"file": ("p.png", PNG, "image/png")}).json()
    assert body["attached"]["slot"] == "discharge_summary"


def test_revoking_consent_stops_reading():
    client.post("/api/consent", json={"case_id": "c1", "scope": "read_documents", "granted": True})
    client.post("/api/consent", json={"case_id": "c1", "scope": "read_documents", "granted": False})
    body = client.post("/api/checklist/c1", files={"file": ("p.png", PNG, "image/png")}).json()
    assert body["attached"]["needs_choice"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"case_id": "c1", "scope": "sell_data", "granted": True},
        {"case_id": "c1", "scope": "read_documents"},
        {"scope": "read_documents", "granted": True},
        {"case_id": "c1", "scope": "read_documents", "granted": "yes"},
    ],
)
def test_bad_consent_requests_are_refused(payload):
    assert client.post("/api/consent", json=payload).status_code == 400


# --- Delete everything -------------------------------------------------------


@pytest.fixture
def full_case(env):
    conn = connect()
    store.ensure_case(conn, "c1")
    store.ensure_case(conn, "c2")
    store.record_consent(conn, "c1", "keep_original", True)
    store.save_document(conn, "c1", Extraction("policy", False, "doc_ai", {"insurer": Field("Example Ltd", 0.9)}),
                        original=b"%PDF-kept", filename="policy.pdf")
    for case_id, owned in (("c1", False), ("c2", True)):
        store.record_event(conn, case_id, "case_routed", {"respondent": "insurer", "distributor_owned": owned})
        store.record_event(conn, case_id, "readiness_checked", {"outcome": "file"})
    conn.close()
    return env


def test_delete_removes_the_case_everywhere(full_case):
    response = client.delete("/api/case/c1")
    assert response.status_code == 200
    assert response.json()["deleted"] == {"documents": 1, "consents": 1, "events": 2, "drafts": 0}

    conn = connect()
    try:
        assert store.get_case(conn, "c1") is None
        for table in ("documents", "consents", "events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE case_id = 'c1'").fetchone()[0] == 0
        assert store.get_case(conn, "c2") is not None  # other cases untouched
    finally:
        conn.close()
    assert not any((full_case / "originals").rglob("*.pdf"))


def test_delete_removes_the_case_from_the_console(full_case):
    before = client.get("/api/metrics").json()
    assert before["headline"]["cases"] == 2
    assert before["counters"]["readiness_checks_run"] == 2

    client.delete("/api/case/c1")

    after = client.get("/api/metrics").json()
    assert after["headline"]["cases"] == 1
    assert after["counters"]["readiness_checks_run"] == 1
    assert [r["case_id"] for r in client.get("/api/console/cases").json()["cases"]] == ["c2"]


def test_delete_unknown_case_is_404(full_case):
    assert client.delete("/api/case/nobody").status_code == 404
    client.delete("/api/case/c1")
    assert client.delete("/api/case/c1").status_code == 404
