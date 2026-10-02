"""PRD-PAYTM N8: the Distributor Console. Every number comes from an event row."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import cases, config, console, store
from app.core import ladder_engine as le
from app.core.ladder_engine import Facts
from app.main import app
from app.services.documents import Extraction, Field

client = TestClient(app)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "praman.db")
    monkeypatch.setattr(config, "DISTRIBUTOR_SHORT_NAME", "Paytm")
    connection = store.connect()
    yield connection
    connection.close()


def routed(conn, case_id, respondent, owned, grievance_class="insurance/claim_delayed", name=None):
    store.ensure_case(conn, case_id)
    store.record_event(conn, case_id, "case_routed", {
        "product": "health_policy", "grievance_class": grievance_class, "respondent": respondent,
        "respondent_name": name, "distributor_owned": owned, "ladder": "x",
    })


@pytest.fixture
def seeded(conn):
    """An example event log: five routed cases, one not yet routed."""
    routed(conn, "c1", "insurer", False, name="Example General Insurance Company Ltd")
    routed(conn, "c2", "distributor", True, grievance_class="platform/payment_failed")
    routed(conn, "c3", "insurer", False)
    routed(conn, "c4", "lender", False, grievance_class="lending/wrong_emi")
    routed(conn, "c5", "distributor", True, grievance_class="platform/refund")
    routed(conn, "c5", "insurer", False)  # re-routed: the latest event wins
    store.ensure_case(conn, "c6")  # no routing event yet

    store.record_event(conn, "c1", "readiness_checked", {"outcome": "do_not_file_yet"})
    store.record_event(conn, "c1", "claim_stopped", {"rules": ["waiting_period_unmet"]})
    store.record_event(conn, "c1", "readiness_checked", {"outcome": "do_not_file_yet"})
    store.record_event(conn, "c1", "claim_stopped", {"rules": ["waiting_period_unmet"]})
    store.record_event(conn, "c3", "readiness_checked", {"outcome": "do_not_file_yet"})
    store.record_event(conn, "c3", "claim_stopped", {"rules": ["policy_lapsed", "documents_incomplete"]})
    store.record_event(conn, "c4", "readiness_checked", {"outcome": "file_with_known_deduction"})
    store.record_event(conn, "c4", "deduction_explained", {"rule": "room_cap_breach", "deduction": 45000})
    store.record_event(conn, "c3", "deduction_explained", {"rule": "room_cap_breach", "deduction": None})
    store.record_event(conn, "c1", "coverage_query_drafted", {})
    store.record_event(conn, "c1", "clock_started", {"ladder": "insurance_claim", "step": "insurer_grievance_cell",
                                                      "started_on": "2026-10-01", "respond_by": "2026-10-15",
                                                      "verified_by": "UNVERIFIED"})
    return conn


# --- Headline and counters ---------------------------------------------------


def test_headline_matches_the_seeded_event_log(seeded):
    headline = console.metrics(seeded)["headline"]
    assert headline == {"cases": 5, "needed_distributor": 1, "distributor": "Paytm", "text": "Of 5 cases, 1 needed Paytm."}


def test_routed_away_is_the_cases_whose_latest_route_is_not_the_distributor(seeded):
    # PRD Part 6: the count of distributor_owned=False over total, from the event log.
    counters = console.metrics(seeded)["counters"]
    assert counters["cases_routed_away"] == 4
    assert console.metrics(seeded)["headline"]["cases"] == 5


def test_the_counters(seeded):
    counters = console.metrics(seeded)["counters"]
    assert counters == {
        "readiness_checks_run": 4,
        "claims_stopped": 2,
        "claims_stopped_why": {"waiting_period_unmet": 1, "policy_lapsed": 1, "documents_incomplete": 1},
        "known_deductions_explained": 2,
        "coverage_queries_drafted": 1,
        "escalations_drafted": 0,
        "cases_routed_away": 4,
        "insurer_queries_caught": 0,
        "answers_given": 0,
        "answers_confirmed_solved": 0,
        "asked_for_a_person": 0,
        "cases_marked_resolved": 0,
    }


def test_numbers_come_from_events_not_from_the_cases_table(seeded):
    before = console.metrics(seeded)
    # A cases-table value with no event behind it must not move any number.
    store.set_route(seeded, "c6", "health_policy", "distributor", None, True)
    store.set_route(seeded, "c1", "health_policy", "distributor", None, True)
    assert console.metrics(seeded) == before


def test_empty_log_is_all_zeros(conn):
    result = console.metrics(conn)
    assert result["headline"] == {"cases": 0, "needed_distributor": 0, "distributor": "Paytm", "text": "Of 0 cases, 0 needed Paytm."}
    assert all(v == 0 for k, v in result["counters"].items() if k != "claims_stopped_why")


def test_no_projected_savings_or_cost_figures(seeded):
    result = console.metrics(seeded)
    assert set(result) == {"headline", "counters"}
    flat = str(result).lower()
    for word in ("saving", "cost", "₹", "rupee", "percent", "%", "projected"):
        assert word not in flat


def test_headline_names_the_distributor_from_configuration(seeded, monkeypatch):
    monkeypatch.setattr(config, "DISTRIBUTOR_SHORT_NAME", "the distributor")
    assert console.metrics(seeded)["headline"]["text"] == "Of 5 cases, 1 needed the distributor."


# --- Case list ---------------------------------------------------------------


def test_case_list_has_every_case_with_its_latest_state(seeded):
    store.ensure_case(seeded, "c7")
    store.save_document(seeded, "c7", Extraction("claim_doc", False, "checklist"), slot="final_bill")
    rows = {row["case_id"]: row for row in console.case_list(seeded)}
    # c6 was only opened (a browser visiting the site): nothing to show. c7 has a document.
    assert set(rows) == {"c1", "c2", "c3", "c4", "c5", "c7"}

    c1 = rows["c1"]
    assert c1["respondent"] == "insurer"
    assert c1["respondent_name"] == "Example General Insurance Company Ltd"
    assert c1["distributor_owned"] is False
    assert c1["grievance_class"] == "insurance/claim_delayed"
    assert c1["verdict"] == "do_not_file_yet"
    assert c1["clock"] == {"step": "insurer_grievance_cell", "respond_by": "2026-10-15", "verified_by": "UNVERIFIED"}

    assert rows["c5"]["respondent"] == "insurer"  # latest route
    assert rows["c7"]["distributor_owned"] is None
    assert rows["c7"]["verdict"] is None


def test_filter_needs_paytm(seeded):
    assert [r["case_id"] for r in console.case_list(seeded, "needs_paytm")] == ["c2"]


def test_filter_routed_away(seeded):
    assert sorted(r["case_id"] for r in console.case_list(seeded, "routed_away")) == ["c1", "c3", "c4", "c5"]


def test_unknown_filter_is_refused(seeded):
    with pytest.raises(ValueError):
        console.case_list(seeded, "everyone")


def test_case_list_never_exposes_her_phone_number(conn):
    case = store.case_for_user(conn, "whatsapp:+919999999999")
    store.record_event(conn, case["id"], "case_routed", {"respondent": "insurer", "distributor_owned": False})
    assert "9999999999" not in str(console.case_list(conn))


def test_unknown_event_kind_is_refused(conn):
    store.ensure_case(conn, "c1")
    with pytest.raises(ValueError):
        store.record_event(conn, "c1", "saved_money", {})


# --- Real flows write the events ---------------------------------------------


def test_routing_a_case_writes_a_routing_event(conn):
    store.ensure_case(conn, "c1")
    cases.route_case(conn, "c1", "health_policy", "platform/payment_failed")
    [row] = console.case_list(conn)
    assert (row["respondent"], row["distributor_owned"]) == ("distributor", True)
    assert console.metrics(conn)["headline"]["needed_distributor"] == 1


def test_a_readiness_check_writes_its_events(conn):
    store.ensure_case(conn, "c1")
    stopped = cases.check_readiness(conn, "c1", Facts(policy_in_force=False, months_held=30, wait_months=24,
                                                      exclusion_listed=False, room_cap_per_day=5000,
                                                      room_quoted_per_day=5000, documents_collected=6,
                                                      documents_required=6, ped_wait_months=36))
    assert stopped.outcome == le.DO_NOT_FILE_YET

    deducted = cases.check_readiness(conn, "c1", Facts(policy_in_force=True, months_held=30, wait_months=24,
                                                       exclusion_listed=False, room_cap_per_day=5000,
                                                       room_quoted_per_day=8000, bill_deductible_heads=120000,
                                                       bill_exempt_heads=45000, documents_collected=6,
                                                       documents_required=6, ped_wait_months=36))
    assert deducted.outcome == le.FILE_WITH_KNOWN_DEDUCTION

    counters = console.metrics(conn)["counters"]
    assert counters["readiness_checks_run"] == 2
    assert counters["claims_stopped"] == 1
    assert counters["claims_stopped_why"] == {"policy_lapsed": 1}
    assert counters["known_deductions_explained"] == 1
    assert console.case_list(conn)[0]["verdict"] == le.FILE_WITH_KNOWN_DEDUCTION


def test_starting_a_clock_writes_it_on_the_respondents_ladder(conn):
    store.ensure_case(conn, "c1")
    store.save_document(conn, "c1", Extraction("policy", False, "doc_ai", {"insurer": Field("Example Ltd", 0.95)}))
    cases.route_case(conn, "c1", "health_policy", "insurance/claim_delayed")
    clock = cases.start_case_clock(conn, "c1", date(2026, 10, 1))

    assert clock.respond_by == date(2026, 10, 15)
    assert console.case_list(conn)[0]["clock"] == {
        "step": "insurer_grievance_cell", "respond_by": "2026-10-15", "verified_by": "UNVERIFIED",
    }


def test_a_clock_needs_a_route(conn):
    store.ensure_case(conn, "c1")
    with pytest.raises(LookupError):
        cases.start_case_clock(conn, "c1", date(2026, 10, 1))


# --- HTTP --------------------------------------------------------------------


def test_metrics_endpoint(seeded):
    body = client.get("/api/metrics").json()
    assert body["headline"]["text"] == "Of 5 cases, 1 needed Paytm."
    assert body["counters"]["claims_stopped"] == 2


def test_console_cases_endpoint_with_filters(seeded):
    assert [r["case_id"] for r in client.get("/api/console/cases", params={"filter": "needs_paytm"}).json()["cases"]] == ["c2"]
    assert len(client.get("/api/console/cases").json()["cases"]) == 5  # the empty case is not listed
    assert client.get("/api/console/cases", params={"filter": "everyone"}).status_code == 400


def test_readiness_endpoint_returns_the_verdict_and_records_it(conn):
    store.ensure_case(conn, "c1")
    response = client.post("/api/readiness", json={
        "case_id": "c1",
        "facts": {"months_held": 10, "wait_months": 24, "policy_start_on": "2025-06-15"},
    })
    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "do_not_file_yet"
    assert body["possible_on"] == "2027-06-15"
    assert body["next_action"] == "COVERAGE_QUERY"
    assert any("15 June 2027" in m["text"] for m in body["messages"])
    assert console.metrics(conn)["counters"]["claims_stopped"] == 1


def test_readiness_endpoint_without_a_case_records_nothing(conn):
    response = client.post("/api/readiness", json={"facts": {"policy_in_force": False}})
    assert response.json()["outcome"] == "do_not_file_yet"
    assert console.metrics(conn)["counters"]["readiness_checks_run"] == 0


def test_readiness_endpoint_refuses_unknown_facts(conn):
    assert client.post("/api/readiness", json={"facts": {"vibes": "good"}}).status_code == 400
