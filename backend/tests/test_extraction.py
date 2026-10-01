"""PRD-PAYTM C3 (per-document-type extraction) and N2 (policy extraction), plus bills."""

import json
from datetime import date

import pytest

from app import config
from app.core import ladder_engine as le
from app.core import ladders
from app.core.ladder_engine import Facts, evaluate
from app.services import documents as docs
from tests.conftest import make_pdf

PDF = "application/pdf"


# --- detect_doc_type (keyword heuristic, no model) ---------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("KEY FACT STATEMENT\nAnnual Percentage Rate (APR): 26%", "kfs"),
        ("Loan offer. Annual percentage rate 24% p.a.", "kfs"),
        ("Policy Schedule\nSum Insured: Rs 5,00,000\nWaiting period for specified diseases: 24 months", "policy"),
        ("FINAL BILL\nRoom charges 24,000\nPharmacy 18,000", "bill"),
        ("Interim bill - pharmacy and room charges", "bill"),
        ("Dear Sir, we regret to inform you that your claim has been repudiated.", "letter"),
        ("Hello", "letter"),
        ("", "letter"),
        ("   ", "letter"),
    ],
)
def test_detect_doc_type(text, expected):
    assert docs.detect_doc_type(text) == expected


def test_detect_doc_type_is_case_insensitive():
    assert docs.detect_doc_type("sum insured and WAITING PERIOD") == "policy"


def test_rejection_letter_that_mentions_a_waiting_period_is_still_a_letter():
    text = (
        "Dear Policyholder, we regret to inform you that your claim no. 123 stands repudiated "
        "as the treatment falls within the waiting period of your policy."
    )
    assert docs.detect_doc_type(text) == "letter"


def test_detect_doc_type_ties_go_to_the_earlier_type():
    # One kfs keyword and one policy keyword: kfs is checked first.
    assert docs.detect_doc_type("annual percentage rate; sum insured") == "kfs"


# --- Registry ----------------------------------------------------------------


def test_registry_covers_the_four_document_types():
    assert set(docs.DOC_TYPES) == {"letter", "policy", "bill", "kfs"}
    assert set(docs.EXTRACT_SCHEMAS) == set(docs.DOC_TYPES)
    assert set(docs.FACT_MAPS) == set(docs.DOC_TYPES)


def _depth(node, level=1):
    children = list((node.get("properties") or {}).values())
    if "items" in node:
        children.append(node["items"])
    return max([level] + [_depth(child, level + 1) for child in children])


@pytest.mark.parametrize("doc_type", ["letter", "policy", "bill", "kfs"])
def test_schemas_meet_doc_ai_constraints(doc_type):
    schema = docs.EXTRACT_SCHEMAS[doc_type]
    assert schema["type"] == "object"
    assert _depth(schema) <= 4

    def described(node):
        assert node.get("description", "").strip(), node
        for child in (node.get("properties") or {}).values():
            described(child)
        if "items" in node:
            described(node["items"])

    for child in schema["properties"].values():
        described(child)


@pytest.mark.parametrize("doc_type", ["letter", "policy", "bill", "kfs"])
def test_every_field_asks_for_a_confidence(doc_type):
    schema = docs.EXTRACT_SCHEMAS[doc_type]
    fields = set(schema["properties"]) - {"confidence"}
    assert set(schema["properties"]["confidence"]["properties"]) == fields


@pytest.mark.parametrize("doc_type", ["letter", "policy", "bill", "kfs"])
def test_fact_maps_point_at_real_facts(doc_type):
    fields = set(docs.EXTRACT_SCHEMAS[doc_type]["properties"])
    for field, fact in docs.FACT_MAPS[doc_type].items():
        assert field in fields
        assert fact in le.FACT_NAMES


def test_policy_schema_has_every_n2_field():
    assert {
        "insurer", "policy_number", "policy_start_date", "continuous_cover_start", "sum_insured",
        "room_rent_limit_amount", "room_rent_limit_percent", "co_pay_percent",
        "specified_disease_wait_months", "ped_wait_months", "named_exclusions", "network_status",
    } <= set(docs.EXTRACT_SCHEMAS["policy"]["properties"])


def test_policy_room_caps_map_to_rupees_and_percent():
    assert docs.FACT_MAPS["policy"]["room_rent_limit_amount"] == "room_cap_per_day"
    assert docs.FACT_MAPS["policy"]["room_rent_limit_percent"] == "room_cap_percent"


# --- Normalising a Doc AI reply ----------------------------------------------


def test_values_are_typed_and_confidences_kept():
    fields = docs.normalise_fields(
        "policy",
        {
            "sum_insured": "₹5,00,000",
            "ped_wait_months": "36",
            "policy_start_date": "01/04/2019",
            "named_exclusions": ["Cosmetic surgery"],
            "confidence": {"sum_insured": 0.95, "ped_wait_months": 0.9, "policy_start_date": 0.9, "named_exclusions": 0.8},
        },
    )
    assert fields["sum_insured"] == docs.Field(500000.0, 0.95)
    assert fields["ped_wait_months"] == docs.Field(36, 0.9)
    assert fields["policy_start_date"] == docs.Field(date(2019, 4, 1), 0.9)
    assert fields["named_exclusions"].value == ["Cosmetic surgery"]


def test_missing_confidence_counts_as_zero_never_as_sure():
    fields = docs.normalise_fields("policy", {"sum_insured": 500000})
    assert fields["sum_insured"].confidence == 0.0


def test_confidence_is_clamped_to_zero_one():
    fields = docs.normalise_fields(
        "policy", {"sum_insured": 1, "ped_wait_months": 2, "confidence": {"sum_insured": 7, "ped_wait_months": "high"}}
    )
    assert fields["sum_insured"].confidence == 1.0
    assert fields["ped_wait_months"].confidence == 0.0


def test_unreadable_values_become_missing():
    fields = docs.normalise_fields(
        "policy",
        {"sum_insured": "five lakh", "policy_start_date": "sometime in 2019", "confidence": {"sum_insured": 0.99, "policy_start_date": 0.99}},
    )
    assert fields["sum_insured"] == docs.Field(None, 0.0)
    assert fields["policy_start_date"] == docs.Field(None, 0.0)


def test_off_list_enum_values_become_missing():
    fields = docs.normalise_fields("letter", {"denial_reason": "bad luck", "confidence": {"denial_reason": 0.9}})
    assert fields["denial_reason"] == docs.Field(None, 0.0)


def test_a_number_not_found_in_the_document_text_drops_below_the_gate():
    text = "Sum Insured: Rs 5,00,000. Pre-existing diseases: 36 months."
    fields = docs.normalise_fields(
        "policy",
        {"sum_insured": 500000, "ped_wait_months": 48, "confidence": {"sum_insured": 0.95, "ped_wait_months": 0.95}},
        text=text,
    )
    assert fields["sum_insured"].confidence == 0.95
    assert fields["ped_wait_months"].confidence < config.CONFIDENCE_GATE


def test_every_schema_field_is_present_after_normalising():
    fields = docs.normalise_fields("bill", {})
    assert set(fields) == set(docs.EXTRACT_SCHEMAS["bill"]["properties"]) - {"confidence"}
    assert all(f == docs.Field(None, 0.0) for f in fields.values())


# --- Confidence gate (Part 6) ------------------------------------------------


def policy_fields(**confidence):
    base = {
        "sum_insured": 500000, "room_rent_limit_percent": 1, "ped_wait_months": 36,
        "policy_start_date": "2019-04-01", "insurer": "Example Health Insurance Company Ltd",
    }
    conf = {name: 0.95 for name in base} | confidence
    return docs.normalise_fields("policy", {**base, "confidence": conf})


def test_confident_mapped_fields_become_facts():
    review = docs.review("policy", policy_fields())
    assert review.facts == {
        "sum_insured": 500000.0, "room_cap_percent": 1.0, "ped_wait_months": 36,
        "policy_start_on": date(2019, 4, 1),
    }
    assert review.to_confirm == ()


def test_low_confidence_field_is_asked_never_assumed():
    review = docs.review("policy", policy_fields(room_rent_limit_percent=0.4))
    assert "room_cap_percent" not in review.facts
    assert review.to_confirm == ("room_rent_limit_percent",)


def test_low_confidence_field_never_reaches_a_verdict_until_confirmed():
    rule = ladders.load("insurance_health_claim").rule("room_cap_breach")
    extracted = policy_fields(room_rent_limit_percent=0.4)

    unconfirmed = evaluate(Facts(**docs.review("policy", extracted).facts, room_quoted_per_day=8000), [rule])
    assert unconfirmed.outcome == le.FACTS_PENDING

    confirmed = docs.review("policy", extracted, confirmed={"room_rent_limit_percent": 1})
    verdict = evaluate(Facts(**confirmed.facts, room_quoted_per_day=8000), [rule])
    assert verdict.outcome == le.FILE_WITH_KNOWN_DEDUCTION
    assert verdict.deductions[0].values["limit"] == 5000


def test_user_correction_replaces_the_extracted_value():
    review = docs.review("policy", policy_fields(room_rent_limit_percent=0.4), confirmed={"room_rent_limit_percent": 2})
    assert review.facts["room_cap_percent"] == 2


def test_missing_mapped_fields_are_listed_to_ask():
    review = docs.review("policy", policy_fields())
    assert review.missing == ("continuous_cover_start",)
    # A percent room cap is stated, so the rupee form is not asked for.
    assert "room_rent_limit_amount" not in review.missing
    # Fields with no fact behind them are not asked for.
    assert "network_status" not in review.missing


def test_room_cap_is_asked_when_neither_form_is_stated():
    fields = docs.normalise_fields("policy", {"sum_insured": 500000, "confidence": {"sum_insured": 0.95}})
    missing = docs.review("policy", fields).missing
    assert "room_rent_limit_amount" in missing
    assert "room_rent_limit_percent" in missing


def test_confirming_an_unknown_field_is_refused():
    with pytest.raises(KeyError):
        docs.review("policy", policy_fields(), confirmed={"favourite_colour": "blue"})


# --- Bill heads --------------------------------------------------------------


def test_bill_heads_file_loads_and_is_badged_unverified():
    heads = docs.load_bill_heads()
    assert heads.verified_by == "UNVERIFIED"
    assert heads.deductible and heads.exempt
    assert not set(heads.deductible) & set(heads.exempt)


@pytest.mark.parametrize(
    "line, head",
    [
        ("Room rent (3 days)", "deductible"),
        ("NURSING CHARGES", "deductible"),
        ("Surgeon fees", "deductible"),
        ("Operation theatre charges", "deductible"),
        ("Pharmacy", "exempt"),
        ("Consumables", "exempt"),
        ("Implant - intraocular lens", "exempt"),
        ("Laboratory investigations", "exempt"),
        ("Food and beverages", None),
        ("Registration charges", None),
        ("Surgical consumables", None),  # matches both heads: asked, not guessed
    ],
)
def test_bill_line_heads(line, head):
    assert docs.head_for(line, docs.load_bill_heads()) == head


LINES = [
    {"description": "Room rent (3 days)", "amount": 24000},
    {"description": "Pharmacy", "amount": 18000},
    {"description": "Registration charges", "amount": 500},
]


def bill_fields(lines=LINES, confidence=0.95):
    return docs.normalise_fields(
        "bill",
        {"line_items": lines, "room_rent_per_day": 8000, "admission_date": "2026-10-05",
         "confidence": {"line_items": confidence, "room_rent_per_day": 0.95, "admission_date": 0.95}},
    )


def test_unmapped_bill_lines_hold_back_both_head_totals():
    review = docs.review("bill", bill_fields())
    assert review.unmapped_lines == ("Registration charges",)
    assert "bill_deductible_heads" not in review.facts
    assert "bill_exempt_heads" not in review.facts
    assert review.facts["room_quoted_per_day"] == 8000
    assert review.facts["treatment_on"] == date(2026, 10, 5)


def test_answered_bill_lines_complete_the_totals():
    review = docs.review("bill", bill_fields(), answers={"Registration charges": "deductible"})
    assert review.unmapped_lines == ()
    assert review.facts["bill_deductible_heads"] == 24500
    assert review.facts["bill_exempt_heads"] == 18000


def test_an_answer_must_name_a_head():
    with pytest.raises(ValueError):
        docs.review("bill", bill_fields(), answers={"Registration charges": "probably fine"})


def test_a_line_with_no_amount_is_asked():
    lines = LINES[:2] + [{"description": "Nursing charges", "amount": None}]
    assert docs.review("bill", bill_fields(lines)).unmapped_lines == ("Nursing charges",)


def test_low_confidence_line_items_are_confirmed_before_totals():
    review = docs.review("bill", bill_fields(LINES[:2], confidence=0.5))
    assert "bill_deductible_heads" not in review.facts
    assert review.to_confirm == ("line_items",)

    confirmed = docs.review("bill", bill_fields(LINES[:2], confidence=0.5), confirmed={"line_items": LINES[:2]})
    assert confirmed.facts["bill_deductible_heads"] == 24000


# --- extract(): live path and fixtures ---------------------------------------


def test_extract_with_a_known_type_submits_its_schema_once(fake_sarvam):
    fake_sarvam.extracted = {"sum_insured": 500000, "confidence": {"sum_insured": 0.95}}
    result = docs.extract(make_pdf(1), "policy.pdf", "policy", mime_type=PDF)

    assert result.doc_type == "policy"
    assert result.detected is False
    assert result.source == "doc_ai"
    assert result.fields["sum_insured"] == docs.Field(500000.0, 0.95)
    assert fake_sarvam.schemas == [docs.EXTRACT_SCHEMAS["policy"]]


def test_extract_without_a_type_detects_it_from_the_text_first(fake_sarvam):
    fake_sarvam.text = "FINAL BILL\nRoom charges 8,000 per day\nPharmacy 18,000"
    fake_sarvam.extracted = {"room_rent_per_day": 8000, "confidence": {"room_rent_per_day": 0.9}}
    result = docs.extract(make_pdf(1), "scan.pdf", mime_type=PDF)

    assert result.doc_type == "bill"
    assert result.detected is True
    assert fake_sarvam.schemas == [docs.EXTRACT_SCHEMAS["bill"]]
    assert result.fields["room_rent_per_day"].value == 8000


def test_extract_gives_up_after_the_timeout(fake_sarvam, monkeypatch):
    import app.clients.sarvam as sarvam_client

    monkeypatch.setattr(sarvam_client, "job_status", lambda job_id: {"status": "running"})
    with pytest.raises(docs.ExtractionFailed):
        docs.extract(make_pdf(1), "policy.pdf", "policy", mime_type=PDF, timeout=0, sleep=lambda s: None)


def test_extract_rejects_an_unknown_type():
    with pytest.raises(docs.UploadRejected):
        docs.extract(make_pdf(1), "x.pdf", "passport", mime_type=PDF)


def test_fixture_flag_returns_the_example_without_calling_sarvam(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("Sarvam must not be called in fixture mode")

    import app.clients.sarvam as sarvam_client

    monkeypatch.setattr(sarvam_client, "digitise", boom)
    monkeypatch.setattr(sarvam_client, "extract", boom)
    monkeypatch.setattr(config, "USE_DOC_FIXTURES", True)

    result = docs.extract(b"%PDF-1.7 anything", "policy.pdf", "policy", mime_type=PDF)
    assert result.source == "fixture"
    assert "Example case" in result.example
    assert result.fields["room_rent_limit_percent"].value == 1


@pytest.mark.parametrize("name", ["policy_demo.json", "bill_demo.json"])
def test_fixtures_are_labelled_examples(name):
    payload = json.loads((config.FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert payload["example"].startswith("Example case")
    assert set(payload["fields"]) <= set(docs.EXTRACT_SCHEMAS[payload["doc_type"]]["properties"])


def test_demo_fixtures_drive_the_room_cap_to_the_rupee(monkeypatch):
    monkeypatch.setattr(config, "USE_DOC_FIXTURES", True)
    policy = docs.review("policy", docs.extract(b"%PDF-", "p.pdf", "policy", mime_type=PDF).fields)
    bill = docs.review("bill", docs.extract(b"%PDF-", "b.pdf", "bill", mime_type=PDF).fields)
    assert policy.to_confirm == bill.to_confirm == bill.unmapped_lines == ()

    verdict = evaluate(
        Facts(**policy.facts, **bill.facts), [ladders.load("insurance_health_claim").rule("room_cap_breach")]
    )
    values = verdict.deductions[0].values
    assert values["limit"] == 5000  # 1% of ₹5,00,000, resolved in the engine
    assert (bill.facts["bill_deductible_heads"], bill.facts["bill_exempt_heads"]) == (95000, 38850)  # gloves and an admission kit are consumables: never cut
    assert values["deduction"] == 35625  # (1 - 5000/8000) * 95,000
    assert values["payable_estimate"] == 98225
