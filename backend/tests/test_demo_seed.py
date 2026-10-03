"""The demo seed (PRD-PAYTM Part 9) and the reset that rebuilds it."""

import time

import pytest

from app import cases, config, console, store
from app.core import ladder_engine as le
from scripts import reset_demo, seed_demo

ADMISSION, MORATORIUM, DOUBLE_DEBIT = "demo-admission", "demo-moratorium", "demo-double-debit"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    monkeypatch.setattr(config, "DISTRIBUTOR_SHORT_NAME", "Paytm")
    connection = store.connect()
    seed_demo.seed(connection)
    yield connection
    connection.close()


def latest(conn, case_id, kind):
    return store.latest_event(conn, case_id, kind)["detail"]


def test_exactly_the_three_part_9_cases_all_labelled_examples(conn):
    rows = {r["case_id"]: r for r in console.case_list(conn)}
    assert set(rows) == {ADMISSION, MORATORIUM, DOUBLE_DEBIT}
    assert all(r["example"] for r in rows.values())


def test_admission_case_files_with_a_known_deduction(conn):
    assert latest(conn, ADMISSION, "readiness_checked")["outcome"] == le.FILE_WITH_KNOWN_DEDUCTION
    deduction = latest(conn, ADMISSION, "deduction_explained")
    assert (deduction["rule"], deduction["deduction"]) == ("room_cap_breach", 35625)
    # built from the policy and bill fixtures, not typed-in numbers
    doc_types = {d["doc_type"] for d in store.case_documents(conn, ADMISSION, ("policy", "bill"))}
    assert doc_types == {"policy", "bill"}
    routed = latest(conn, ADMISSION, "case_routed")
    assert (routed["respondent"], routed["distributor_owned"]) == ("insurer", False)


def test_admission_case_catches_the_name_slip_before_filing(conn):
    assert latest(conn, ADMISSION, "papers_checked") == {"fix": ["name_mismatch"], "heads_up": ["non_payable_items"]}
    assert console.metrics(conn)["counters"]["insurer_queries_caught"] == 1


def test_moratorium_case_has_the_ground_and_a_draft_to_the_insurer_by_name(conn):
    facts = cases.facts_from_json(latest(conn, MORATORIUM, "readiness_checked")["facts"])
    assert facts.months_continuous_cover == 72  # six years
    assert facts.denial_reason == "non_disclosure"
    from app.core import ladders

    verdict = le.evaluate(facts, ladders.load("insurance_health_claim").rules)
    assert [h.rule_id for h in verdict.grounds] == ["moratorium_reached"]

    [draft] = store.case_drafts(conn, MORATORIUM)
    assert draft["addressee"] == "Grievance cell, Example General Insurance Company Ltd"
    assert draft["status"] == "drafted"  # she approves it live
    assert "non-disclosure" in draft["text"]


def test_double_debit_is_the_distributors(conn):
    routed = latest(conn, DOUBLE_DEBIT, "case_routed")
    assert (routed["grievance_class"], routed["distributor_owned"]) == ("platform/payment_failed", True)


def test_headline_counts_only_what_was_seeded(conn):
    assert console.metrics(conn)["headline"]["text"] == "Of 3 cases, 1 needed Paytm."


def test_checklists_agree_with_the_facts_the_checks_used(conn):
    for case_id in (ADMISSION, MORATORIUM):
        state = cases.checklist_state(conn, case_id)
        used = latest(conn, case_id, "readiness_checked")["facts"]
        assert (used["documents_collected"], used["documents_required"]) == (state.collected, state.required)


def test_seeding_twice_changes_nothing(conn):
    before = console.metrics(conn)
    seed_demo.seed(conn)
    assert console.metrics(conn) == before
    assert len(store.case_drafts(conn, MORATORIUM)) == 1


def test_reset_wipes_everything_else_and_reseeds_quickly(conn, tmp_path, capsys):
    store.ensure_case(conn, "someone-else")
    store.record_event(conn, "someone-else", "case_routed", {"respondent": "insurer", "distributor_owned": False})
    (tmp_path / "originals" / "someone-else").mkdir(parents=True)
    (tmp_path / "originals" / "someone-else" / "1-kept.pdf").write_bytes(b"%PDF-")

    started = time.perf_counter()
    assert reset_demo.main() == 0
    elapsed = time.perf_counter() - started

    assert elapsed < 5
    rows = {r["case_id"] for r in console.case_list(conn)}
    assert rows == {ADMISSION, MORATORIUM, DOUBLE_DEBIT}
    assert not (tmp_path / "originals" / "someone-else").exists()
    assert "Of 3 cases, 1 needed Paytm." in capsys.readouterr().out
