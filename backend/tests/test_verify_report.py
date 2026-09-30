"""PRD-PAYTM C5: every legal value is listed for hand verification, and replies carry a badge."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cases, config, store
from app.core import ladders
from app.main import app
from scripts import verify_report as vr

ENTRIES = vr.scan(vr.default_paths())
BY_LABEL = {entry.label: entry for entry in ENTRIES}


def test_every_rule_and_step_is_listed():
    for rule in ladders.load("insurance_health_claim").rules:
        assert BY_LABEL[rule.id].status == "UNVERIFIED"
    for step in ladders.load_steps():
        assert BY_LABEL[step].status == "UNVERIFIED"
    assert {"bill_heads", "insurance_health_claim"} <= {Path(e.file).stem for e in ENTRIES}


def test_every_yaml_that_declares_verified_by_is_scanned():
    scanned = {Path(e.file) for e in ENTRIES}
    for path in vr.default_paths():
        if "verified_by" in path.read_text(encoding="utf-8"):
            assert path in scanned, path


def test_corpus_sources_are_listed_by_file():
    assert BY_LABEL["insurer/example_general/health_policy/policy_wording.md"].status == "UNVERIFIED"


def test_each_entry_points_at_its_verified_by_line():
    for entry in ENTRIES:
        line = Path(entry.file).read_text(encoding="utf-8").splitlines()[entry.line - 1]
        assert "verified_by: UNVERIFIED" in line, (entry.file, entry.line)


def test_entries_show_the_values_to_check_and_their_source():
    assert "limit=36" in BY_LABEL["ped_wait_exceeds_cap"].summary
    assert "limit=60" in BY_LABEL["moratorium_reached"].summary
    assert "denial_reason=non_disclosure" in BY_LABEL["moratorium_reached"].summary
    assert "respond_within_days=14" in BY_LABEL["insurer_grievance_cell"].summary
    assert "respond_within_days=30" in BY_LABEL["lender_grievance"].summary
    assert "IRDAI" in BY_LABEL["room_cap_breach"].source


def test_a_rule_without_verified_by_is_reported_missing(tmp_path):
    path = tmp_path / "ladder.yaml"
    path.write_text("id: t\nverified_by: UNVERIFIED\nrules:\n  - id: quiet\n    kind: flag_false\n", encoding="utf-8")
    [ladder, rule] = vr.scan([path])
    assert (ladder.status, rule.status, rule.label, rule.line) == ("UNVERIFIED", "MISSING", "quiet", 4)


def test_a_verified_value_is_not_listed(tmp_path):
    path = tmp_path / "ladder.yaml"
    path.write_text("id: t\nverified_by: A. Reviewer, 2026-10-01\n", encoding="utf-8")
    assert vr.scan([path]) == []


def test_cli_prints_file_and_line_and_strict_fails(capsys):
    assert vr.main([]) == 0
    out = capsys.readouterr().out
    assert "insurance_health_claim.yaml:" in out
    assert "UNVERIFIED" in out
    assert vr.main(["--strict"]) == 1


def test_cli_fails_on_a_missing_verified_by_even_without_strict(tmp_path):
    path = tmp_path / "ladder.yaml"
    path.write_text("id: t\nverified_by: UNVERIFIED\nrules:\n  - id: quiet\n    kind: flag_false\n", encoding="utf-8")
    assert vr.main([str(path)]) == 1


# --- Replies that use an unverified value carry a badge ----------------------

client = TestClient(app)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")


def test_readiness_reply_is_badged(api):
    body = client.post("/api/readiness", json={"facts": {"policy_in_force": False}}).json()
    assert body["unverified"] is True
    assert all(m["unverified"] for m in body["messages"])


def test_readiness_reply_with_nothing_to_say_carries_no_badge(api):
    body = client.post("/api/readiness", json={"facts": {"policy_in_force": True}}).json()
    assert body["messages"] == []
    assert body["unverified"] is False


def test_checklist_reply_is_badged(api):
    client.post("/api/checklist/c1", files={"file": ("p.png", b"\x89PNG\r\n\x1a\nxxxx", "image/png")}, data={"slot": "final_bill"})
    assert client.get("/api/checklist/c1").json()["unverified"] is True


def test_console_clock_carries_its_verification(api):
    from datetime import date

    from app.services.documents import Extraction, Field

    conn = store.connect()
    store.ensure_case(conn, "c1")
    store.save_document(conn, "c1", Extraction("policy", False, "doc_ai", {"insurer": Field("Example Ltd", 0.95)}))
    cases.route_case(conn, "c1", "health_policy", "insurance/claim_delayed")
    cases.start_case_clock(conn, "c1", date(2026, 10, 1))
    conn.close()
    [row] = client.get("/api/console/cases").json()["cases"]
    assert row["clock"]["verified_by"] == "UNVERIFIED"
