"""PRD-PAYTM N1: the claim readiness ladder, against the real YAML."""

import dataclasses
from datetime import date

import pytest

from app.core import ladder_engine as le
from app.core import ladders
from app.core.ladder_engine import Facts, evaluate

LADDER = ladders.load("insurance_health_claim")

N1_RULES = {
    "waiting_period_unmet",
    "procedure_excluded",
    "policy_lapsed",
    "room_cap_breach",
    "documents_incomplete",
    "ped_wait_exceeds_cap",
    "moratorium_reached",
}


def only(rule_id):
    return [LADDER.rule(rule_id)]


def ids(hits):
    return [hit.rule_id for hit in hits]


# --- The ladder file ---------------------------------------------------------


def test_ladder_has_exactly_the_n1_rules():
    assert {rule.id for rule in LADDER.rules} == N1_RULES


def test_every_rule_is_badged_unverified_with_a_message():
    assert LADDER.verified_by == "UNVERIFIED"
    for rule in LADDER.rules:
        entry = LADDER.entry(rule.id)
        assert entry.verified_by == "UNVERIFIED"
        assert entry.message.strip()


def test_rule_facts_registry_matches_what_each_rule_reads():
    assert set(LADDER.rule_facts) == N1_RULES
    for rule in LADDER.rules:
        assert LADDER.rule_facts[rule.id] == rule.facts


# --- waiting_period_unmet ----------------------------------------------------


def test_waiting_period_blocks_with_the_date_it_becomes_possible():
    verdict = evaluate(Facts(months_held=10, wait_months=24, policy_start_on=date(2025, 6, 15)), only("waiting_period_unmet"))
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.possible_on == date(2027, 6, 15)
    assert verdict.next_action == le.COVERAGE_QUERY
    assert verdict.blocks[0].values["remaining"] == 14


def test_waiting_period_clear():
    assert evaluate(Facts(months_held=24, wait_months=24), only("waiting_period_unmet")).outcome == le.FILE


def test_waiting_period_missing_fact():
    verdict = evaluate(Facts(wait_months=24), only("waiting_period_unmet"))
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("months_held",)


def test_waiting_period_months_held_derived_from_policy_start_and_treatment_dates():
    facts = Facts(policy_start_on=date(2025, 6, 15), treatment_on=date(2026, 4, 20), wait_months=24)
    verdict = evaluate(facts, only("waiting_period_unmet"))
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.blocks[0].values["value"] == 10
    assert verdict.possible_on == date(2027, 6, 15)


def test_waiting_period_without_a_start_date_still_blocks_but_has_no_date():
    verdict = evaluate(Facts(months_held=10, wait_months=24), only("waiting_period_unmet"))
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.possible_on is None
    assert verdict.facts_pending == ("policy_start_on",)


# --- procedure_excluded ------------------------------------------------------


def test_procedure_excluded_blocks_with_no_date():
    verdict = evaluate(Facts(exclusion_listed=True), only("procedure_excluded"))
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.possible_on is None
    assert verdict.next_action == le.COVERAGE_QUERY


def test_procedure_excluded_clear():
    assert evaluate(Facts(exclusion_listed=False), only("procedure_excluded")).outcome == le.FILE


def test_procedure_excluded_missing_fact():
    verdict = evaluate(Facts(), only("procedure_excluded"))
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("exclusion_listed",)


# --- policy_lapsed -----------------------------------------------------------


def test_policy_lapsed_blocks():
    verdict = evaluate(Facts(policy_in_force=False), only("policy_lapsed"))
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.next_action == le.COVERAGE_QUERY


def test_policy_lapsed_clear():
    assert evaluate(Facts(policy_in_force=True), only("policy_lapsed")).outcome == le.FILE


def test_policy_lapsed_missing_fact():
    assert evaluate(Facts(), only("policy_lapsed")).facts_pending == ("policy_in_force",)


# --- room_cap_breach ---------------------------------------------------------

ROOM = dict(room_cap_per_day=5000, room_quoted_per_day=8000)


def test_room_cap_deduction_to_the_rupee():
    verdict = evaluate(
        Facts(**ROOM, bill_deductible_heads=120000, bill_exempt_heads=45000), only("room_cap_breach")
    )
    values = verdict.deductions[0].values

    assert verdict.outcome == le.FILE_WITH_KNOWN_DEDUCTION
    assert values["ratio"] == pytest.approx(0.625)
    # deduction = (1 - 5000/8000) * 1,20,000
    assert values["deduction"] == 45000
    assert values["exempt"] == 45000
    assert values["payable_estimate"] == 120000
    assert values["pending"] is False


def test_room_cap_deduction_rounds_to_the_nearest_rupee():
    verdict = evaluate(
        Facts(room_cap_per_day=4500, room_quoted_per_day=7000, bill_deductible_heads=98765, bill_exempt_heads=10000),
        only("room_cap_breach"),
    )
    values = verdict.deductions[0].values
    # (1 - 4500/7000) * 98,765 = 35,273.21
    assert values["deduction"] == 35273
    assert values["payable_estimate"] == 98765 + 10000 - 35273


def test_room_cap_exempt_heads_are_never_reduced():
    def run(exempt):
        facts = Facts(**ROOM, bill_deductible_heads=120000, bill_exempt_heads=exempt)
        return evaluate(facts, only("room_cap_breach")).deductions[0].values

    none_exempt, some_exempt = run(0), run(50000)
    assert none_exempt["deduction"] == some_exempt["deduction"]
    assert some_exempt["payable_estimate"] - none_exempt["payable_estimate"] == 50000


def test_room_cap_never_cuts_the_whole_bill():
    values = evaluate(
        Facts(**ROOM, bill_deductible_heads=120000, bill_exempt_heads=45000), only("room_cap_breach")
    ).deductions[0].values
    assert values["deduction"] < 120000
    assert values["payable_estimate"] >= values["exempt"]


@pytest.mark.parametrize(
    "heads, missing",
    [
        (dict(), ("bill_deductible_heads", "bill_exempt_heads")),
        (dict(bill_exempt_heads=45000), ("bill_deductible_heads",)),
        (dict(bill_deductible_heads=120000), ("bill_exempt_heads",)),
    ],
)
def test_room_cap_with_missing_heads_fires_and_reports_the_deduction_pending(heads, missing):
    verdict = evaluate(Facts(**ROOM, **heads), only("room_cap_breach"))
    values = verdict.deductions[0].values

    assert verdict.outcome == le.FILE_WITH_KNOWN_DEDUCTION
    assert values["pending"] is True
    assert values["deduction"] is None
    assert values["payable_estimate"] is None
    assert verdict.facts_pending == missing


def test_room_cap_clear_when_room_is_within_cap():
    assert evaluate(Facts(room_cap_per_day=5000, room_quoted_per_day=5000), only("room_cap_breach")).outcome == le.FILE


def test_room_cap_missing_fact():
    verdict = evaluate(Facts(room_quoted_per_day=8000), only("room_cap_breach"))
    assert verdict.outcome == le.FACTS_PENDING
    assert verdict.facts_pending == ("room_cap_per_day",)


# --- documents_incomplete ----------------------------------------------------


def test_documents_incomplete_blocks_with_the_shortfall():
    verdict = evaluate(Facts(documents_collected=3, documents_required=5), only("documents_incomplete"))
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.blocks[0].values["shortfall"] == 2


def test_documents_incomplete_clear():
    assert evaluate(Facts(documents_collected=5, documents_required=5), only("documents_incomplete")).outcome == le.FILE


def test_documents_incomplete_missing_fact():
    assert evaluate(Facts(documents_collected=3), only("documents_incomplete")).facts_pending == ("documents_required",)


# --- ped_wait_exceeds_cap (Part 6: 36 clear, 48 flagged) ---------------------


def test_ped_cap_48_is_flagged_as_a_ground():
    verdict = evaluate(Facts(ped_wait_months=48), only("ped_wait_exceeds_cap"))
    assert ids(verdict.grounds) == ["ped_wait_exceeds_cap"]
    assert verdict.grounds[0].values["limit"] == 36
    assert verdict.blocks == ()


def test_ped_cap_36_is_clear():
    assert evaluate(Facts(ped_wait_months=36), only("ped_wait_exceeds_cap")).grounds == ()


def test_ped_cap_missing_fact():
    assert evaluate(Facts(), only("ped_wait_exceeds_cap")).facts_pending == ("ped_wait_months",)


# --- moratorium_reached (Part 6) ---------------------------------------------


def test_moratorium_59_months_non_disclosure_is_no_ground():
    verdict = evaluate(Facts(months_continuous_cover=59, denial_reason="non_disclosure"), only("moratorium_reached"))
    assert verdict.grounds == ()


def test_moratorium_60_months_non_disclosure_attaches_the_ground():
    verdict = evaluate(Facts(months_continuous_cover=60, denial_reason="non_disclosure"), only("moratorium_reached"))
    assert ids(verdict.grounds) == ["moratorium_reached"]


def test_moratorium_60_months_exclusion_denial_is_no_ground():
    # Moratorium does not override permanent exclusions.
    verdict = evaluate(Facts(months_continuous_cover=60, denial_reason="exclusion"), only("moratorium_reached"))
    assert verdict.grounds == ()
    assert verdict.facts_pending == ()


def test_moratorium_missing_fact():
    verdict = evaluate(Facts(denial_reason="non_disclosure"), only("moratorium_reached"))
    assert verdict.facts_pending == ("months_continuous_cover",)


# --- Three outcomes, whole ladder (Part 6) -----------------------------------

READY = Facts(
    months_held=30,
    wait_months=24,
    policy_start_on=date(2023, 3, 1),
    exclusion_listed=False,
    policy_in_force=True,
    room_cap_per_day=5000,
    room_quoted_per_day=5000,
    bill_deductible_heads=120000,
    bill_exempt_heads=45000,
    documents_collected=5,
    documents_required=5,
    ped_wait_months=36,
)


def test_three_outcomes():
    clean = evaluate(READY, LADDER.rules)
    not_yet = evaluate(dataclasses.replace(READY, months_held=10, policy_start_on=date(2025, 6, 15)), LADDER.rules)
    deducted = evaluate(dataclasses.replace(READY, room_quoted_per_day=8000), LADDER.rules)

    assert clean.outcome == le.FILE
    assert clean.next_action is None

    assert not_yet.outcome == le.DO_NOT_FILE_YET
    assert not_yet.possible_on == date(2027, 6, 15)
    assert not_yet.next_action == le.COVERAGE_QUERY

    assert deducted.outcome == le.FILE_WITH_KNOWN_DEDUCTION
    assert deducted.deductions[0].values["deduction"] == 45000


def test_lower_room_turns_a_known_deduction_into_a_clean_file():
    quoted = evaluate(dataclasses.replace(READY, room_quoted_per_day=8000), LADDER.rules)
    lower = evaluate(dataclasses.replace(READY, room_quoted_per_day=4800), LADDER.rules)
    assert quoted.outcome == le.FILE_WITH_KNOWN_DEDUCTION
    assert lower.outcome == le.FILE


def test_blocks_with_no_date_leave_possible_on_empty():
    verdict = evaluate(
        dataclasses.replace(READY, months_held=10, policy_start_on=date(2025, 6, 15), policy_in_force=False),
        LADDER.rules,
    )
    assert ids(verdict.blocks) == ["waiting_period_unmet", "policy_lapsed"]
    assert verdict.possible_on is None


# --- Plain-language messages -------------------------------------------------


def explain(facts, rule_id):
    verdict = evaluate(facts, only(rule_id))
    return ladders.explain(verdict, LADDER)


def test_room_cap_message_names_the_amount_and_what_is_not_cut():
    [message] = explain(Facts(**ROOM, bill_deductible_heads=120000, bill_exempt_heads=45000), "room_cap_breach")
    assert "About ₹45,000 will be cut" in message["text"]
    assert "Medicines, tests and implants are not cut" in message["text"]


def test_room_cap_message_without_the_bill_says_pending_and_names_no_amount():
    [message] = explain(Facts(**ROOM), "room_cap_breach")
    assert "once the bill is shared" in message["text"]
    assert "₹" not in message["text"]


def test_waiting_period_message_gives_the_date():
    [message] = explain(
        Facts(months_held=10, wait_months=24, policy_start_on=date(2025, 6, 15)), "waiting_period_unmet"
    )
    assert "24-month waiting period" in message["text"]
    assert "10 months old" in message["text"]
    assert "15 June 2027" in message["text"]


def test_waiting_period_message_without_a_start_date_counts_months():
    [message] = explain(Facts(months_held=10, wait_months=24), "waiting_period_unmet")
    assert "14 more months" in message["text"]


def test_every_message_carries_its_unverified_badge():
    facts = dataclasses.replace(
        READY, policy_in_force=False, documents_collected=3, ped_wait_months=48,
        room_quoted_per_day=8000, months_continuous_cover=72, denial_reason="non_disclosure",
    )
    messages = ladders.explain(evaluate(facts, LADDER.rules), LADDER)

    assert {m["rule_id"] for m in messages} == {
        "policy_lapsed", "documents_incomplete", "room_cap_breach", "ped_wait_exceeds_cap", "moratorium_reached",
    }
    assert all(m["verified_by"] == "UNVERIFIED" and m["unverified"] for m in messages)
    assert all("{" not in m["text"] for m in messages)


@pytest.mark.parametrize(
    "amount, text", [(999, "₹999"), (45000, "₹45,000"), (120000, "₹1,20,000"), (12345678, "₹1,23,45,678")]
)
def test_rupees_use_indian_grouping(amount, text):
    assert ladders.inr(amount) == text


# --- Loader refuses a broken ladder ------------------------------------------

GOOD_RULE = """
  - id: policy_lapsed
    kind: flag_false
    value: policy_in_force
    facts: [policy_in_force]
    message: The policy was not in force.
    verified_by: UNVERIFIED
"""


def write(tmp_path, rules, header="verified_by: UNVERIFIED\n"):
    path = tmp_path / "ladder.yaml"
    path.write_text(f"id: test\n{header}rules:{rules}", encoding="utf-8")
    return path


def test_loader_accepts_a_good_rule(tmp_path):
    assert [r.id for r in ladders.load_path(write(tmp_path, GOOD_RULE)).rules] == ["policy_lapsed"]


@pytest.mark.parametrize(
    "rules, header",
    [
        (GOOD_RULE.replace("facts: [policy_in_force]", "facts: [policy_start_on]"), None),
        (GOOD_RULE.replace("    verified_by: UNVERIFIED\n", ""), None),
        (GOOD_RULE, ""),
        (GOOD_RULE.replace("The policy was not in force.", "Lapsed on {lapse_date}."), None),
        (GOOD_RULE + "    colour: red\n", None),
        (GOOD_RULE + GOOD_RULE, None),
        (
            GOOD_RULE.replace("kind: flag_false", "kind: threshold_breach\n    limit: 1\n    calculation: proportionate_deduction")
            .replace("value: policy_in_force", "value: room_quoted_per_day")
            .replace("facts: [policy_in_force]", "facts: [room_quoted_per_day]"),
            None,
        ),
    ],
    ids=["facts-mismatch", "rule-unverified-missing", "ladder-unverified-missing", "unknown-placeholder",
         "unknown-key", "duplicate-id", "calculation-without-pending-message"],
)
def test_loader_rejects_a_broken_ladder(tmp_path, rules, header):
    path = write(tmp_path, rules) if header is None else write(tmp_path, rules, header)
    with pytest.raises(ladders.LadderError):
        ladders.load_path(path)
