"""PRD-PAYTM N5: who owes her an answer."""

import ast
from datetime import date
from pathlib import Path

import pytest

from app import cases, config, store
from app.core import agent, ladders, routing
from app.core.routing import route
from app.services import drafts
from app.services.documents import Extraction, Field

INSURER_NAME = "Example General Insurance Company Ltd"
LENDER_NAME = "Example Finance Ltd"
DISTRIBUTOR_NAME = "Example Broking Pvt Ltd"
NAMES = {"insurer": INSURER_NAME, "lender": LENDER_NAME, "distributor": DISTRIBUTOR_NAME}
LENDING = [c for c in agent.GRIEVANCE_CLASSES if c.startswith("lending/")]


# --- The N5 table ------------------------------------------------------------


def test_claim_delay_routes_to_the_insurer_not_the_distributor():
    r = route("health_policy", "insurance/claim_delayed", names=NAMES)
    assert r.respondent == "insurer"
    assert r.respondent_name == INSURER_NAME
    assert r.distributor_owned is False
    assert r.steps == ("insurer_grievance_cell", "irdai_grievance", "insurance_ombudsman")
    assert r.first_step == "insurer_grievance_cell"


def test_claim_denied_takes_the_same_ladder():
    assert route("health_policy", "insurance/claim_denied").ladder == route("health_policy", "insurance/claim_delayed").ladder


def test_double_debit_routes_to_the_distributor():
    r = route("health_policy", "platform/payment_failed", names=NAMES)
    assert r.respondent == "distributor"
    assert r.respondent_name == DISTRIBUTOR_NAME
    assert r.distributor_owned is True
    assert r.steps == ("distributor_support_ticket",)


@pytest.mark.parametrize("grievance_class", ["platform/refund", "platform/app_issue"])
def test_refunds_and_app_issues_are_the_distributors_ticket(grievance_class):
    r = route("health_policy", grievance_class)
    assert (r.respondent, r.distributor_owned, r.first_step) == ("distributor", True, "distributor_support_ticket")


def test_mis_sold_goes_to_the_distributor_first_then_insurer_then_regulator():
    r = route("health_policy", "insurance/mis_sold")
    assert r.respondent == "distributor"
    assert r.distributor_owned is True
    assert r.steps == ("distributor_grievance", "insurer_grievance_cell", "irdai_grievance")


def test_bundled_insurance_on_a_loan_is_still_mis_selling():
    assert route("merchant_loan", "insurance/mis_sold").respondent == "distributor"


def test_coverage_question_is_answered_in_chat_then_asked_of_the_insurer():
    r = route("health_policy", None)
    assert (r.respondent, r.distributor_owned) == ("insurer", False)
    assert r.steps == ("answered_in_chat", "coverage_query")


@pytest.mark.parametrize("grievance_class", LENDING)
def test_loan_servicing_goes_to_the_lender(grievance_class):
    r = route("merchant_loan", grievance_class, names=NAMES)
    assert (r.respondent, r.respondent_name, r.distributor_owned) == ("lender", LENDER_NAME, False)
    assert r.steps == ("lender_grievance", "lender_nodal_officer", "rbi_ombudsman")


def test_motor_claim_starts_at_the_claims_desk():
    r = route("motor_policy", "insurance/claim_denied")
    assert (r.respondent, r.distributor_owned) == ("insurer", False)
    assert r.steps == ("insurer_claims_desk", "insurer_grievance_cell", "irdai_grievance")


@pytest.mark.parametrize(
    "product, grievance_class",
    [
        ("health_policy", "banking/service_deficiency"),
        ("health_policy", "rti/no_response"),
        ("health_policy", "other"),
        ("health_policy", "insurance/policy_mismatch"),  # not in the N5 table: asked, not guessed
        (None, "insurance/claim_denied"),  # which policy? asked, not guessed
        ("merchant_loan", None),
        (None, None),
    ],
)
def test_situations_outside_the_table_are_not_routed(product, grievance_class):
    assert route(product, grievance_class) is None


@pytest.mark.parametrize("grievance_class", agent.GRIEVANCE_CLASSES + (None,))
@pytest.mark.parametrize("product", agent.PRODUCTS + (None,))
def test_distributor_owns_exactly_platform_and_mis_selling(product, grievance_class):
    r = route(product, grievance_class)
    if r is not None:
        owned = grievance_class is not None and (grievance_class.startswith("platform/") or grievance_class == "insurance/mis_sold")
        assert r.distributor_owned is owned


@pytest.mark.parametrize("grievance_class", agent.GRIEVANCE_CLASSES + (None,))
@pytest.mark.parametrize("product", agent.PRODUCTS + (None,))
def test_every_situation_matches_at_most_one_row(product, grievance_class):
    assert len(routing.matching_rows(product, grievance_class)) <= 1


def test_claim_delay_and_double_debit_never_share_a_ladder():
    claim = route("health_policy", "insurance/claim_delayed")
    debit = route("health_policy", "platform/payment_failed")
    assert not set(claim.steps) & set(debit.steps)


def test_unknown_name_stays_unknown():
    assert route("health_policy", "insurance/claim_delayed").respondent_name is None
    assert route("health_policy", "insurance/claim_delayed", names={"lender": LENDER_NAME}).respondent_name is None


# --- Steps, windows and clocks -----------------------------------------------


def test_every_step_on_every_ladder_has_a_badged_window():
    steps = ladders.load_steps()
    for row in routing.TABLE:
        for step in row.steps:
            assert step in steps, step
            assert steps[step].verified_by == "UNVERIFIED"
            assert steps[step].label


def test_the_clock_started_is_the_insurers_for_a_claim():
    clock = routing.start_clock(route("health_policy", "insurance/claim_delayed"), date(2026, 10, 1), ladders.load_steps())
    assert (clock.ladder, clock.step) == ("insurance_claim", "insurer_grievance_cell")
    assert clock.respond_by == date(2026, 10, 15)
    assert clock.verified_by == "UNVERIFIED"


def test_the_clock_started_is_the_lenders_for_loan_servicing():
    clock = routing.start_clock(route("merchant_loan", "lending/wrong_emi"), date(2026, 10, 1), ladders.load_steps())
    assert clock.step == "lender_grievance"
    assert clock.respond_by == date(2026, 10, 31)


def test_a_step_with_no_known_window_starts_no_deadline():
    clock = routing.start_clock(route("health_policy", "platform/payment_failed"), date(2026, 10, 1), ladders.load_steps())
    assert clock.step == "distributor_support_ticket"
    assert clock.respond_by is None


def test_a_clock_cannot_run_on_another_respondents_ladder():
    with pytest.raises(ValueError):
        routing.start_clock(
            route("health_policy", "platform/payment_failed"), date(2026, 10, 1), ladders.load_steps(),
            step="insurance_ombudsman",
        )


# --- Drafts are addressed by name --------------------------------------------


def test_a_draft_is_addressed_to_the_respondent_by_name():
    to = drafts.addressee(route("health_policy", "insurance/claim_delayed", names=NAMES), ladders.load_steps())
    assert to == f"Grievance cell, {INSURER_NAME}"
    assert "the company" not in to.lower()


def test_a_draft_without_the_legal_name_is_refused_not_generic():
    with pytest.raises(drafts.RespondentUnknown):
        drafts.addressee(route("health_policy", "insurance/claim_delayed"), ladders.load_steps())


def test_a_case_with_no_route_cannot_be_drafted():
    with pytest.raises(drafts.RespondentUnknown):
        drafts.addressee(None, ladders.load_steps())


# --- Purity ------------------------------------------------------------------

ALLOWED = {"__future__", "dataclasses", "datetime", "typing", "app.core.ladder_engine"}


def test_router_is_pure():
    tree = ast.parse(Path(routing.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {a.name for a in node.names} <= ALLOWED
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module in ALLOWED or module.split(".")[0] in {"__future__", "dataclasses", "datetime", "typing"} or (
                module == "app.core" and {a.name for a in node.names} <= {"ladder_engine"}
            )


# --- Stored on the case (C7) -------------------------------------------------


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISTRIBUTOR_LEGAL_NAME", DISTRIBUTOR_NAME)
    connection = store.connect()
    store.ensure_case(connection, "c1")
    yield connection
    connection.close()


def policy_with_insurer(confidence):
    return Extraction("policy", False, "doc_ai", {"insurer": Field(INSURER_NAME, confidence)})


def test_routing_a_case_stores_product_respondent_name_and_ownership(conn):
    store.save_document(conn, "c1", policy_with_insurer(0.95))
    r = cases.route_case(conn, "c1", "health_policy", "insurance/claim_delayed")
    saved = store.get_case(conn, "c1")

    assert r.respondent_name == INSURER_NAME
    assert saved["product"] == "health_policy"
    assert saved["respondent"] == "insurer"
    assert saved["respondent_name"] == INSURER_NAME
    assert saved["distributor_owned"] == 0


def test_a_low_confidence_insurer_name_is_not_used(conn):
    store.save_document(conn, "c1", policy_with_insurer(0.4))
    assert cases.route_case(conn, "c1", "health_policy", "insurance/claim_delayed").respondent_name is None
    assert store.get_case(conn, "c1")["respondent_name"] is None


def test_the_distributor_name_comes_from_configuration(conn):
    r = cases.route_case(conn, "c1", "health_policy", "platform/payment_failed")
    assert r.respondent_name == DISTRIBUTOR_NAME
    assert store.get_case(conn, "c1")["distributor_owned"] == 1


def test_an_unrouted_case_stores_the_product_and_clears_the_respondent(conn):
    cases.route_case(conn, "c1", "health_policy", "platform/payment_failed")
    assert cases.route_case(conn, "c1", "health_policy", "other") is None
    saved = store.get_case(conn, "c1")
    assert saved["product"] == "health_policy"
    assert saved["respondent"] is None
    assert saved["distributor_owned"] is None


@pytest.mark.parametrize("product", ["life_policy", "travel_policy", "home_policy", "other_insurance"])
def test_any_insurance_claim_goes_to_the_insurer_ladder(product):
    r = route(product, "insurance/claim_delayed")
    assert (r.respondent, r.distributor_owned) == ("insurer", False)
    assert r.steps == ("insurer_grievance_cell", "irdai_grievance", "insurance_ombudsman")


@pytest.mark.parametrize("product", agent.INSURANCE_PRODUCTS)
def test_a_coverage_question_on_any_insurance_is_answered_then_asked_of_the_insurer(product):
    assert route(product, None).steps == ("answered_in_chat", "coverage_query")


def test_the_router_knows_every_insurance_the_classifier_does():
    assert routing._INSURANCE == frozenset(agent.INSURANCE_PRODUCTS)
