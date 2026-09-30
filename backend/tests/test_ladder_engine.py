import ast
import dataclasses
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.core import ladder_engine as le
from app.core.ladder_engine import Facts, Rule, evaluate

C2_FIELDS = {
    # respondent routing
    "respondent", "distributor_owned",
    # insurance
    "policy_start_on", "procedure", "wait_months", "ped_wait_months", "months_held",
    "months_continuous_cover", "sum_insured", "room_cap_per_day", "room_quoted_per_day",
    "bill_deductible_heads", "bill_exempt_heads", "exclusion_listed", "policy_in_force",
    "denial_reason", "documents_collected", "documents_required",
    # lending
    "sanctioned_amount", "processing_fee", "net_disbursal", "instalment_amount",
    "instalment_count", "total_repayable", "kfs_supplied", "insurance_bundled",
    "insurance_consented",
    # motor
    "tp_cover_valid", "zero_dep_addon", "idv", "claim_amount",
}

# Example rules shaped like PRD-PAYTM N1, used to exercise each kind.
WAITING = Rule("waiting_period_unmet", le.DURATION_UNMET, value="months_held", limit="wait_months")
WAITING_BY_DATE = Rule(
    "waiting_period_unmet_by_date", le.DURATION_UNMET,
    since="policy_start_on", until="treatment_on", limit="wait_months",
)
MORATORIUM = Rule(
    "moratorium_reached", le.DURATION_MET, value="months_continuous_cover", limit=60,
    when=(("denial_reason", "non_disclosure"),),
)
ROOM_CAP = Rule(
    "room_cap_breach", le.THRESHOLD_BREACH, value="room_quoted_per_day",
    limit="room_cap_per_day", effect=le.DEDUCTION,
)
PED_CAP = Rule("ped_wait_exceeds_cap", le.THRESHOLD_BREACH, value="ped_wait_months", limit=36)
DOCUMENTS = Rule(
    "documents_incomplete", le.THRESHOLD_SHORT, value="documents_collected", limit="documents_required"
)
LAPSED = Rule("policy_lapsed", le.FLAG_FALSE, value="policy_in_force")


def ids(hits):
    return [hit.rule_id for hit in hits]


# --- C2: Facts and Verdict ---------------------------------------------------


def test_facts_has_every_c2_field_defaulting_to_none():
    fields = {f.name: f for f in dataclasses.fields(Facts)}
    assert C2_FIELDS <= set(fields)
    assert all(getattr(Facts(), name) is None for name in fields)


def test_facts_rejects_unknown_fields():
    with pytest.raises(TypeError):
        Facts(not_a_fact=1)


def test_verdict_carries_respondent_and_distributor_owned():
    verdict = evaluate(Facts(respondent="insurer", distributor_owned=False, policy_in_force=True), [LAPSED])
    assert verdict.respondent == "insurer"
    assert verdict.distributor_owned is False


def test_verdict_respondent_is_none_when_unknown():
    verdict = evaluate(Facts(policy_in_force=True), [LAPSED])
    assert verdict.respondent is None
    assert verdict.distributor_owned is None


# --- C4: duration_unmet ------------------------------------------------------


def test_duration_unmet_fires_when_elapsed_is_short():
    verdict = evaluate(Facts(months_held=10, wait_months=24), [WAITING])
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert ids(verdict.blocks) == ["waiting_period_unmet"]
    assert verdict.blocks[0].values == {"value": 10, "limit": 24, "remaining": 14}


def test_duration_unmet_clear_at_exactly_the_required_months():
    verdict = evaluate(Facts(months_held=24, wait_months=24), [WAITING])
    assert verdict.outcome == le.FILE
    assert verdict.blocks == ()


def test_duration_unmet_missing_fact_is_pending_not_clear():
    verdict = evaluate(Facts(wait_months=24), [WAITING])
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("months_held",)


def test_duration_unmet_from_two_dates_counts_whole_months():
    rule = WAITING_BY_DATE
    short = evaluate(Facts(policy_start_on=date(2025, 1, 15), treatment_on=date(2026, 1, 14), wait_months=12), [rule])
    met = evaluate(Facts(policy_start_on=date(2025, 1, 15), treatment_on=date(2026, 1, 15), wait_months=12), [rule])

    assert short.outcome == le.DO_NOT_FILE_YET
    assert short.blocks[0].values == {"value": 11, "limit": 12, "remaining": 1}
    assert met.outcome == le.FILE


def test_duration_unmet_from_dates_missing_a_date_is_pending():
    verdict = evaluate(Facts(policy_start_on=date(2025, 1, 15), wait_months=12), [WAITING_BY_DATE])
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("treatment_on",)


# --- C4: duration_met --------------------------------------------------------


def test_duration_met_fires_as_a_ground_never_a_file_on_its_own():
    verdict = evaluate(Facts(months_continuous_cover=72, denial_reason="non_disclosure"), [MORATORIUM])
    assert ids(verdict.grounds) == ["moratorium_reached"]
    assert verdict.outcome == le.NO_VERDICT
    assert verdict.outcome != le.FILE


def test_duration_met_at_exactly_the_threshold_fires():
    verdict = evaluate(Facts(months_continuous_cover=60, denial_reason="non_disclosure"), [MORATORIUM])
    assert ids(verdict.grounds) == ["moratorium_reached"]


def test_duration_met_does_not_fire_below_the_threshold():
    verdict = evaluate(Facts(months_continuous_cover=59, denial_reason="non_disclosure"), [MORATORIUM])
    assert verdict.grounds == ()


def test_duration_met_missing_fact_is_pending():
    verdict = evaluate(Facts(denial_reason="non_disclosure"), [MORATORIUM])
    assert verdict.grounds == ()
    assert verdict.facts_pending == ("months_continuous_cover",)


def test_duration_met_does_not_apply_when_its_condition_is_false():
    # Moratorium does not override a permanent exclusion.
    verdict = evaluate(Facts(months_continuous_cover=72, denial_reason="exclusion"), [MORATORIUM])
    assert verdict.grounds == ()
    assert verdict.facts_pending == ()


def test_duration_met_condition_unknown_is_pending():
    verdict = evaluate(Facts(months_continuous_cover=72), [MORATORIUM])
    assert verdict.grounds == ()
    assert verdict.facts_pending == ("denial_reason",)


def test_ground_attaches_to_whatever_the_gate_rules_decide():
    facts = Facts(months_continuous_cover=72, denial_reason="non_disclosure", policy_in_force=True)
    verdict = evaluate(facts, [LAPSED, MORATORIUM])
    assert verdict.outcome == le.FILE
    assert ids(verdict.grounds) == ["moratorium_reached"]

    blocked = evaluate(dataclasses.replace(facts, policy_in_force=False), [LAPSED, MORATORIUM])
    assert blocked.outcome == le.DO_NOT_FILE_YET
    assert ids(blocked.grounds) == ["moratorium_reached"]


def test_missing_ground_fact_is_asked_but_does_not_hold_the_verdict():
    verdict = evaluate(Facts(policy_in_force=True, denial_reason="non_disclosure"), [LAPSED, MORATORIUM])
    assert verdict.outcome == le.FILE
    assert verdict.facts_pending == ("months_continuous_cover",)


# --- C4: threshold_breach ----------------------------------------------------


def test_threshold_breach_fires_when_actual_exceeds_allowed():
    verdict = evaluate(Facts(ped_wait_months=48), [PED_CAP])
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.blocks[0].values == {"value": 48, "limit": 36, "excess": 12}


def test_threshold_breach_clear_at_exactly_the_limit():
    assert evaluate(Facts(ped_wait_months=36), [PED_CAP]).outcome == le.FILE


def test_threshold_breach_missing_fact_is_pending():
    verdict = evaluate(Facts(), [PED_CAP])
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("ped_wait_months",)


def test_threshold_breach_as_deduction_files_with_a_known_deduction():
    verdict = evaluate(Facts(room_quoted_per_day=8000, room_cap_per_day=5000), [ROOM_CAP])
    assert verdict.outcome == le.FILE_WITH_KNOWN_DEDUCTION
    assert ids(verdict.deductions) == ["room_cap_breach"]
    assert verdict.blocks == ()


def test_threshold_breach_as_deduction_missing_limit_is_pending():
    verdict = evaluate(Facts(room_quoted_per_day=8000), [ROOM_CAP])
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("room_cap_per_day",)


# --- C4: threshold_short -----------------------------------------------------


def test_threshold_short_fires_when_actual_is_below_required():
    verdict = evaluate(Facts(documents_collected=3, documents_required=5), [DOCUMENTS])
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.blocks[0].values == {"value": 3, "limit": 5, "shortfall": 2}


def test_threshold_short_clear_when_requirement_met():
    assert evaluate(Facts(documents_collected=5, documents_required=5), [DOCUMENTS]).outcome == le.FILE


def test_threshold_short_missing_fact_is_pending():
    verdict = evaluate(Facts(documents_collected=3), [DOCUMENTS])
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("documents_required",)


# --- C4: flag_false ----------------------------------------------------------


def test_flag_false_fires_on_false():
    verdict = evaluate(Facts(policy_in_force=False), [LAPSED])
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert ids(verdict.blocks) == ["policy_lapsed"]


def test_flag_false_clear_on_true():
    assert evaluate(Facts(policy_in_force=True), [LAPSED]).outcome == le.FILE


def test_flag_false_none_is_nobody_asked_not_no():
    verdict = evaluate(Facts(), [LAPSED])
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.blocks == ()
    assert verdict.facts_pending == ("policy_in_force",)


# --- Outcome precedence ------------------------------------------------------


def test_a_known_block_wins_over_pending_facts():
    verdict = evaluate(Facts(policy_in_force=False), [LAPSED, PED_CAP])
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.facts_pending == ("ped_wait_months",)


def test_a_deduction_never_clears_while_a_gate_fact_is_missing():
    verdict = evaluate(Facts(room_quoted_per_day=8000, room_cap_per_day=5000), [ROOM_CAP, LAPSED])
    assert verdict.outcome == le.FACTS_PENDING


def test_no_rules_is_no_verdict():
    assert evaluate(Facts(), []).outcome == le.NO_VERDICT


def test_facts_pending_lists_each_fact_once_in_rule_order():
    other = Rule("other_wait", le.DURATION_UNMET, value="months_held", limit=12)
    verdict = evaluate(Facts(), [WAITING, other, LAPSED])
    assert verdict.facts_pending == ("months_held", "wait_months", "policy_in_force")


# --- Rule definitions and RULE_FACTS -----------------------------------------


def test_rule_facts_lists_what_the_rule_reads():
    assert WAITING.facts == ("months_held", "wait_months")
    assert WAITING_BY_DATE.facts == ("policy_start_on", "treatment_on", "wait_months")
    assert MORATORIUM.facts == ("denial_reason", "months_continuous_cover")
    assert PED_CAP.facts == ("ped_wait_months",)


def test_rule_facts_registry_is_built_from_the_rules():
    assert le.rule_facts([WAITING, LAPSED]) == {
        "waiting_period_unmet": ("months_held", "wait_months"),
        "policy_lapsed": ("policy_in_force",),
    }


def test_duplicate_rule_ids_are_refused():
    with pytest.raises(ValueError):
        le.rule_facts([LAPSED, LAPSED])


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(id="typo", kind=le.FLAG_FALSE, value="policy_in_forse"),
        dict(id="no_kind", kind="threshold_maybe", value="idv", limit=1),
        dict(id="no_limit", kind=le.THRESHOLD_BREACH, value="claim_amount"),
        dict(id="flag_with_limit", kind=le.FLAG_FALSE, value="kfs_supplied", limit=1),
        dict(id="both_forms", kind=le.DURATION_UNMET, value="months_held", since="policy_start_on",
             until="treatment_on", limit=12),
        dict(id="half_dates", kind=le.DURATION_UNMET, since="policy_start_on", limit=12),
        dict(id="ground_on_gate", kind=le.THRESHOLD_SHORT, value="documents_collected", limit=1,
             effect=le.GROUND),
        dict(id="deduction_on_met", kind=le.DURATION_MET, value="months_held", limit=1,
             effect=le.DEDUCTION),
        dict(id="bool_limit", kind=le.THRESHOLD_BREACH, value="claim_amount", limit=True),
        dict(id="bad_when", kind=le.FLAG_FALSE, value="kfs_supplied", when=(("no_such_fact", 1),)),
        dict(id="bad_calc", kind=le.THRESHOLD_BREACH, value="claim_amount", limit="idv",
             calculation="guesswork"),
        dict(id="true_with_limit", kind=le.FLAG_TRUE, value="exclusion_listed", limit=1),
    ],
)
def test_invalid_rules_are_rejected_at_definition(kwargs):
    with pytest.raises(ValueError):
        Rule(**kwargs)


# --- Purity contract ---------------------------------------------------------

ALLOWED_IMPORTS = {"__future__", "calendar", "dataclasses", "datetime", "decimal", "typing"}
FORBIDDEN_CALLS = {"now", "today", "utcnow", "open", "random", "time", "getenv"}


def test_engine_is_pure():
    tree = ast.parse(Path(le.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {a.name.split(".")[0] for a in node.names} <= ALLOWED_IMPORTS
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in ALLOWED_IMPORTS
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in FORBIDDEN_CALLS, f"engine calls {name}()"


# --- flag_true (N1: procedure_excluded reads exclusion_listed) --------------

EXCLUDED = Rule("procedure_excluded", le.FLAG_TRUE, value="exclusion_listed")


def test_flag_true_fires_on_true():
    verdict = evaluate(Facts(exclusion_listed=True), [EXCLUDED])
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert ids(verdict.blocks) == ["procedure_excluded"]


def test_flag_true_clear_on_false():
    assert evaluate(Facts(exclusion_listed=False), [EXCLUDED]).outcome == le.FILE


def test_flag_true_none_is_pending():
    verdict = evaluate(Facts(), [EXCLUDED])
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("exclusion_listed",)


def test_threshold_breach_can_be_a_ground():
    rule = Rule("cap", le.THRESHOLD_BREACH, value="ped_wait_months", limit=36, effect=le.GROUND)
    verdict = evaluate(Facts(ped_wait_months=48), [rule])
    assert ids(verdict.grounds) == ["cap"]
    assert verdict.outcome == le.NO_VERDICT


def test_next_action_on_a_block_is_a_coverage_query():
    assert evaluate(Facts(policy_in_force=False), [LAPSED]).next_action == le.COVERAGE_QUERY
    assert evaluate(Facts(policy_in_force=True), [LAPSED]).next_action is None


# --- Calendar arithmetic -----------------------------------------------------


@pytest.mark.parametrize(
    "start, months, expected",
    [
        (date(2025, 6, 15), 24, date(2027, 6, 15)),
        (date(2025, 1, 31), 1, date(2025, 2, 28)),
        (date(2024, 1, 31), 1, date(2024, 2, 29)),
        (date(2025, 11, 30), 3, date(2026, 2, 28)),
    ],
)
def test_add_months_clamps_to_the_month_end(start, months, expected):
    assert le.add_months(start, months) == expected


@pytest.mark.parametrize("start", [date(2025, 1, 31), date(2025, 6, 15), date(2024, 2, 29), date(2025, 8, 31)])
@pytest.mark.parametrize("months", [1, 6, 12, 24, 36])
def test_whole_months_agrees_with_add_months(start, months):
    anniversary = le.add_months(start, months)
    assert le.whole_months_between(start, anniversary) == months
    assert le.whole_months_between(start, anniversary - timedelta(days=1)) == months - 1


# --- Derived facts (N2): percent room caps, continuous cover -----------------


def test_percent_room_cap_resolves_against_sum_insured():
    facts = le.derive(Facts(sum_insured=500000, room_cap_percent=1))
    assert facts.room_cap_per_day == 5000


def test_percent_room_cap_without_sum_insured_stays_unknown():
    facts = le.derive(Facts(room_cap_percent=1))
    assert facts.room_cap_per_day is None
    verdict = evaluate(Facts(room_cap_percent=1, room_quoted_per_day=8000), [ROOM_CAP])
    assert verdict.outcome == le.FACTS_PENDING


def test_a_stated_rupee_room_cap_wins_over_a_percent():
    facts = le.derive(Facts(sum_insured=500000, room_cap_percent=1, room_cap_per_day=4000))
    assert facts.room_cap_per_day == 4000


def test_percent_room_cap_feeds_the_room_cap_rule():
    verdict = evaluate(Facts(sum_insured=500000, room_cap_percent=1, room_quoted_per_day=8000), [ROOM_CAP])
    assert verdict.outcome == le.FILE_WITH_KNOWN_DEDUCTION
    assert verdict.deductions[0].values["limit"] == 5000


def test_continuous_cover_months_derived_from_cover_start_and_treatment():
    facts = le.derive(Facts(continuous_cover_since=date(2020, 3, 10), treatment_on=date(2026, 3, 10)))
    assert facts.months_continuous_cover == 72


# --- Bill head totals (N1 inputs, summed here, never in a prompt) ------------


def test_bill_head_totals_sum_each_head():
    totals = le.bill_head_totals([("deductible", 24000), ("exempt", 18000.5), ("deductible", 71000), ("exempt", 19000)])
    assert totals == {"deductible": 95000, "exempt": 37000.5}


def test_bill_head_totals_reject_an_unknown_head():
    with pytest.raises(ValueError):
        le.bill_head_totals([("maybe", 100)])
