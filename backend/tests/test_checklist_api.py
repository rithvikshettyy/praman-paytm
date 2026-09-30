"""PRD-PAYTM Part 4: GET and POST /api/checklist/{case_id}."""

import pytest
from fastapi.testclient import TestClient

from app import config, store
from app.main import app
from app.services import documents

client = TestClient(app)
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(documents, "read_text", lambda *a, **k: "blurry")


def post(case_id="case-1", *, file=True, **data):
    files = {"file": ("photo.png", PNG, "image/png")} if file else None
    return client.post(f"/api/checklist/{case_id}", files=files, data=data)


def test_get_unknown_case_is_404():
    assert client.get("/api/checklist/nobody").status_code == 404


def test_post_with_a_slot_ticks_it():
    response = post(slot="final_bill")
    body = response.json()

    assert response.status_code == 200
    assert body["attached"] == {"document_id": body["attached"]["document_id"], "slot": "final_bill", "needs_choice": False}
    assert body["documents_collected"] == 1
    assert body["documents_required"] == 6
    assert "final_bill" not in body["missing"]
    assert body["slots"][1] == {"number": 2, "slot": "final_bill", "label": "Final bill", "filled": True}


def test_post_without_a_slot_classifies_the_photo(monkeypatch):
    monkeypatch.setattr(documents, "read_text", lambda *a, **k: "DISCHARGE SUMMARY\nDate of discharge: 08/10/2026")
    client.post("/api/consent", json={"case_id": "case-1", "scope": "read_documents", "granted": True})
    assert post().json()["attached"]["slot"] == "discharge_summary"


def test_unclear_photo_returns_numbered_options_then_a_slot_is_chosen():
    body = post().json()
    assert body["attached"]["needs_choice"] is True
    assert body["options"][0] == {"number": 1, "slot": "discharge_summary", "label": "Discharge summary"}
    assert len(body["options"]) == 6

    chosen = post(file=False, document_id=str(body["attached"]["document_id"]), slot="claim_form").json()
    assert "claim_form" not in chosen["missing"]
    assert chosen["documents_collected"] == 1


def test_get_returns_the_checklist():
    post(slot="policy_copy")
    body = client.get("/api/checklist/case-1").json()
    assert body["case_id"] == "case-1"
    assert body["documents_collected"] == 1
    assert body["missing"] == ["discharge_summary", "final_bill", "id_proof", "prescriptions", "claim_form"]
    assert body["verified_by"] == "UNVERIFIED"


def test_unknown_slot_is_400():
    assert post(slot="selfie").status_code == 400


def test_nothing_to_attach_is_400():
    assert post(file=False).status_code == 400


def test_choosing_a_slot_for_a_missing_document_is_404():
    post(slot="final_bill")
    assert post(file=False, document_id="9999", slot="claim_form").status_code == 404


def test_the_photo_is_not_kept_without_consent(tmp_path):
    body = post(slot="id_proof").json()
    conn = store.connect()
    try:
        assert store.get_document(conn, body["attached"]["document_id"])["retained"] == 0
    finally:
        conn.close()
    assert not (tmp_path / "originals").exists()
