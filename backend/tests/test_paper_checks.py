"""Paper checks: catch what an insurer would query (names, dates, totals, unpaid items) before she files."""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import config, console, readiness, store
from app.core import ladder_engine as le
from app.core.ladder_engine import Papers, check_papers, mentions, same_person
from app.main import app
from app.services import documents

NON_PAYABLE = ("glove", "mask", "admission kit")
CLEAN = Papers(
    insured_names=("Ramesh Shankar Patil", "Sunita Ramesh Patil"),
    policy_start_on=date(2019, 4, 1),
    policy_end_on=date(2027, 3, 31),
    patient_name="Ramesh Shankar Patil",
    admission_on=date(2026, 10, 5),
    discharge_on=date(2026, 10, 8),
    bill_total=30000,
    bill_lines=(("Room rent", 24000), ("Nursing charges", 6000)),
)


def problems(papers: Papers) -> list[str]:
    return [f.problem for f in check_papers(papers, NON_PAYABLE).findings]


def test_clean_papers_pass_every_check():
    result = check_papers(CLEAN, NON_PAYABLE)
    assert result.findings == ()
    assert set(result.passed) == set(le.PAPER_CHECKS) and result.skipped == ()


# --- Names ---------------------------------------------------------------------------


@pytest.mark.parametrize("a, b, same", [
    ("Ramesh Shankar Patil", "Ramesh Shankar Patil", True),
    ("R. S. Patil", "Mr Ramesh Shankar Patil", True),  # initials and a title
    ("Ramesh Patil", "Ramesh Shankar Patil", True),  # middle name missing
    ("Patil Ramesh Shankar", "Ramesh Shankar Patil", True),  # surname first
    ("Sunita", "Sunita Ramesh Patil", True),  # one name only
    ("Ramesh S. Patel", "Ramesh Shankar Patil", False),  # the surname must match exactly
    ("Suresh Patil", "Ramesh Patil", False),
    ("Ramesh Shankar Patil", "Ramesh Suresh Patil", False),  # a different middle name
])
def test_same_person(a, b, same):
    assert same_person(a, b) is same


@pytest.mark.parametrize("a, b", [("", "Ramesh Patil"), ("Mr", "Ramesh Patil"), ("रमेश पाटील", "Ramesh Patil")])
def test_names_that_cannot_be_compared_are_not_a_mismatch(a, b):
    assert same_person(a, b) is None


def test_a_patient_not_on_the_policy_is_caught():
    papers = Papers(**{**CLEAN.__dict__, "patient_name": "Ramesh S. Patel"})
    [finding] = check_papers(papers, NON_PAYABLE).findings
    assert finding.problem == "name_mismatch" and finding.values["patient"] == "Ramesh S. Patel"


def test_any_insured_member_may_be_the_patient():
    assert problems(Papers(**{**CLEAN.__dict__, "patient_name": "Smt. Sunita R. Patil"})) == []


@pytest.mark.parametrize("change", [{"patient_name": None}, {"insured_names": None}, {"patient_name": "रमेश पाटील"}])
def test_the_name_check_is_skipped_when_a_name_is_missing_or_unreadable(change):
    result = check_papers(Papers(**{**CLEAN.__dict__, **change}), NON_PAYABLE)
    assert "patient_named_on_policy" in result.skipped and result.findings == ()


# --- Dates ---------------------------------------------------------------------------


def test_admission_before_the_policy_is_caught():
    assert problems(Papers(**{**CLEAN.__dict__, "admission_on": date(2019, 3, 1), "discharge_on": date(2019, 3, 3)})) == [
        "admission_before_policy"]


def test_admission_after_the_period_ends_is_caught():
    assert problems(Papers(**{**CLEAN.__dict__, "admission_on": date(2027, 4, 2), "discharge_on": date(2027, 4, 4)})) == [
        "admission_after_policy"]


def test_admission_on_the_last_day_is_inside():
    assert problems(Papers(**{**CLEAN.__dict__, "admission_on": date(2027, 3, 31), "discharge_on": date(2027, 4, 2)})) == []


@pytest.mark.parametrize("change", [{"admission_on": None}, {"policy_end_on": None}, {"policy_start_on": None}])
def test_the_period_check_needs_the_admission_and_both_ends(change):
    result = check_papers(Papers(**{**CLEAN.__dict__, **change}), NON_PAYABLE)
    assert "admission_in_policy_period" in result.skipped


def test_discharge_before_admission_is_caught():
    assert problems(Papers(**{**CLEAN.__dict__, "discharge_on": date(2026, 10, 4)})) == ["discharge_before_admission"]


def test_the_discharge_check_needs_both_dates():
    assert "discharge_after_admission" in check_papers(Papers(**{**CLEAN.__dict__, "discharge_on": None})).skipped


# --- Totals and unpaid items ------------------------------------------------------------


def test_a_total_that_does_not_add_up_is_caught():
    [finding] = check_papers(Papers(**{**CLEAN.__dict__, "bill_total": 32000}), NON_PAYABLE).findings
    assert finding.problem == "bill_total_mismatch"
    assert finding.values == {"lines_total": 30000, "bill_total": 32000}


def test_paise_rounding_is_not_a_mismatch():
    assert problems(Papers(**{**CLEAN.__dict__, "bill_total": 30000.5})) == []


@pytest.mark.parametrize("change", [{"bill_total": None}, {"bill_lines": (("Room rent", None),)}])
def test_the_total_check_needs_the_total_and_every_amount(change):
    assert "bill_adds_up" in check_papers(Papers(**{**CLEAN.__dict__, **change}), NON_PAYABLE).skipped


def test_items_insurers_usually_do_not_pay_are_listed_with_their_sum():
    lines = CLEAN.bill_lines + (("Admission kit", 1200), ("Gloves and masks", 650))
    result = check_papers(Papers(**{**CLEAN.__dict__, "bill_lines": lines, "bill_total": 31850}), NON_PAYABLE)
    [finding] = result.findings
    assert finding.problem == "non_payable_items"
    assert finding.values == {"items": (("Admission kit", 1200), ("Gloves and masks", 650)), "amount": 1850}


def test_without_a_list_or_lines_the_unpaid_items_check_is_skipped():
    assert "non_payable_items" in check_papers(CLEAN, ()).skipped
    assert "non_payable_items" in check_papers(Papers(**{**CLEAN.__dict__, "bill_lines": None}), NON_PAYABLE).skipped


@pytest.mark.parametrize("text, keyword, found", [
    ("Gloves and masks", "glove", True),
    ("ADMISSION KIT", "admission kit", True),
    ("Kit for admission", "admission kit", False),
    ("Glovebox fee", "glove", False),
])
def test_mentions_is_whole_words_with_plurals(text, keyword, found):
    assert mentions(text, keyword) is found


# --- In her words, and on the console ------------------------------------------------------


def test_findings_are_worded_from_the_data_file_and_the_list_carries_its_badge():
    result = check_papers(Papers(**{
        **CLEAN.__dict__, "patient_name": "Ramesh S. Patel",
        "bill_lines": CLEAN.bill_lines + (("Admission kit", 1200),), "bill_total": 31200,
    }), tuple(readiness.paper_words()["non_payable"]["items"]))
    view = readiness.papers_view(result)
    name, items = view["findings"]
    assert name["severity"] == "fix" and "Ramesh S. Patel" in name["message"] and name["unverified"] is False
    assert items["severity"] == "heads_up" and "Admission kit (₹1,200)" in items["message"]
    assert items["unverified"] is True  # the non-payable list is UNVERIFIED in data/claim_checks.yaml
    assert view["to_fix"] == 1


def test_low_confidence_values_never_reach_the_checks():
    extraction = documents.fixture_extraction("bill")
    fields = {**extraction.fields, "patient_name": documents.Field("Ramesh S. Patel", 0.4)}
    assert "patient_name" not in documents.trusted_values(fields)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    return TestClient(app)


def test_the_demo_documents_show_the_name_slip_and_the_unpaid_items(client):
    case_id = client.post("/api/session", json={"session_id": "papers-test-0001"}).json()["case_id"]
    body = client.post("/api/readiness/documents", data={"case_id": case_id, "demo": "true"}).json()
    papers = body["papers"]
    assert [f["problem"] for f in papers["findings"]] == ["name_mismatch", "non_payable_items"]
    assert {p["check"] for p in papers["passed"]} == {
        "admission_in_policy_period", "discharge_after_admission", "bill_adds_up"}
    assert papers["to_fix"] == 1


def test_a_caught_query_is_counted_on_the_console_without_names(client):
    case_id = client.post("/api/session", json={"session_id": "papers-test-0002"}).json()["case_id"]
    client.post("/api/readiness/documents", data={"case_id": case_id, "demo": "true"})
    conn = store.connect()
    try:
        [event] = [e for e in conn.execute("SELECT detail FROM events WHERE kind = 'papers_checked'")]
        assert json.loads(event["detail"]) == {"fix": ["name_mismatch"], "heads_up": ["non_payable_items"]}
        assert "Patel" not in event["detail"]  # no names or amounts in the audit trail
        assert console.metrics(conn)["counters"]["insurer_queries_caught"] == 1
    finally:
        conn.close()
