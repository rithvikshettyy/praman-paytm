"""PRD-PAYTM N3: the claim document checklist, on the case."""

import pytest

from app import cases, config, store
from app.core import ladder_engine as le
from app.core import ladders
from app.core.ladder_engine import Facts, evaluate
from app.services import documents

SLOTS = ("discharge_summary", "final_bill", "id_proof", "policy_copy", "prescriptions", "claim_form")
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORIGINALS_DIR", tmp_path / "originals")
    connection = store.connect()
    store.ensure_case(connection, "c1")
    store.record_consent(connection, "c1", "read_documents", True)
    yield connection
    connection.close()


@pytest.fixture
def checklist():
    return cases.load_checklist()


def no_ocr(*args, **kwargs):
    raise AssertionError("OCR must not run when the slot is known")


def ocr(text):
    return lambda *args, **kwargs: text


def attach(conn, checklist, **kwargs):
    kwargs.setdefault("read_text", no_ocr)
    return cases.attach(conn, "c1", PNG, "photo.png", checklist=checklist, **kwargs)


# --- The checklist file ------------------------------------------------------


def test_health_claim_checklist_has_the_six_slots_in_order(checklist):
    assert checklist.slot_ids == SLOTS
    assert checklist.verified_by == "UNVERIFIED"
    assert all(slot.label for slot in checklist.slots)


def test_slots_are_numbered_from_one(checklist):
    assert checklist.by_number(1) == "discharge_summary"
    assert checklist.by_number(6) == "claim_form"
    assert checklist.by_number(0) is None
    assert checklist.by_number(7) is None


# --- Classifying a photo -----------------------------------------------------


@pytest.mark.parametrize(
    "text, slot",
    [
        ("DISCHARGE SUMMARY\nDate of discharge: 08/10/2026\nDiagnosis: cataract", "discharge_summary"),
        ("FINAL BILL   Bill No: 4512\nRoom charges 24,000\nNet amount payable 1,32,000", "final_bill"),
        ("GOVERNMENT OF INDIA\nAadhaar\nDOB: 01/01/1970", "id_proof"),
        ("POLICY SCHEDULE\nPolicy Number: EX-1\nSum Insured: 5,00,000", "policy_copy"),
        ("Rx\nTab. Paracetamol 500 mg twice daily after food", "prescriptions"),
        ("CLAIM FORM - PART A\nDetails of primary insured", "claim_form"),
    ],
)
def test_clear_ocr_text_classifies_into_a_slot(checklist, text, slot):
    assert cases.classify_slot(checklist, text=text) == slot


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Hello",
        "Room charges",  # one weak hint is not enough
        "Discharge summary, date of discharge. Final bill, bill no 12.",  # a tie
    ],
)
def test_unclear_ocr_text_is_not_guessed(checklist, text):
    assert cases.classify_slot(checklist, text=text) is None


@pytest.mark.parametrize(
    "caption, slot",
    [
        ("bill", "final_bill"),
        ("this is my aadhaar", "id_proof"),
        ("Discharge summary", "discharge_summary"),
        ("पॉलिसी", "policy_copy"),
        ("डिस्चार्ज", "discharge_summary"),
    ],
)
def test_a_caption_naming_one_slot_is_enough(checklist, caption, slot):
    assert cases.classify_slot(checklist, caption=caption) == slot


def test_a_caption_naming_two_slots_is_not_guessed(checklist):
    assert cases.classify_slot(checklist, caption="bill and policy") is None


def test_a_clear_caption_wins_over_unclear_text(checklist):
    assert cases.classify_slot(checklist, text="Hello", caption="prescription") == "prescriptions"


# --- Checklist state ---------------------------------------------------------


def test_a_new_case_is_missing_everything(conn, checklist):
    state = cases.checklist_state(conn, "c1", checklist)
    assert state.missing == SLOTS
    assert (state.collected, state.required) == (0, 6)
    assert state.pending_document_id is None


def test_attaching_to_a_named_slot_ticks_it(conn, checklist):
    attached = attach(conn, checklist, slot="final_bill")
    state = cases.checklist_state(conn, "c1", checklist)

    assert attached.slot == "final_bill"
    assert attached.needs_choice is False
    assert state.collected == 1
    assert "final_bill" not in state.missing


def test_a_second_page_of_the_same_slot_counts_once(conn, checklist):
    attach(conn, checklist, slot="final_bill")
    attach(conn, checklist, slot="final_bill")
    assert cases.checklist_state(conn, "c1", checklist).collected == 1


def test_a_clear_photo_is_classified_and_ticked(conn, checklist):
    attached = attach(conn, checklist, read_text=ocr("DISCHARGE SUMMARY\nDate of discharge: 08/10/2026"))
    assert attached.slot == "discharge_summary"
    assert cases.checklist_state(conn, "c1", checklist).collected == 1


def test_an_unclear_photo_waits_for_her_to_pick_the_slot(conn, checklist):
    attached = attach(conn, checklist, read_text=ocr("blurry"))
    state = cases.checklist_state(conn, "c1", checklist)
    assert attached.needs_choice is True
    assert attached.slot is None
    assert state.pending_document_id == attached.document_id
    assert state.collected == 0

    cases.choose_slot(conn, "c1", attached.document_id, "claim_form", checklist)
    state = cases.checklist_state(conn, "c1", checklist)
    assert "claim_form" not in state.missing
    assert state.pending_document_id is None


def test_ocr_failure_falls_back_to_the_numbered_list(conn, checklist):
    def broken(*args, **kwargs):
        raise documents.ExtractionFailed("Doc AI is down")

    assert attach(conn, checklist, read_text=broken).needs_choice is True


def test_without_read_consent_the_photo_is_never_read(conn, checklist):
    store.ensure_case(conn, "c9")
    attached = cases.attach(conn, "c9", PNG, "photo.png", checklist=checklist, read_text=no_ocr)
    assert attached.needs_choice is True


def test_fixture_mode_skips_ocr_and_asks(conn, checklist, monkeypatch):
    monkeypatch.setattr(config, "USE_DOC_FIXTURES", True)
    assert attach(conn, checklist, read_text=no_ocr).needs_choice is True


def test_an_unknown_slot_is_refused(conn, checklist):
    with pytest.raises(cases.UnknownSlot):
        attach(conn, checklist, slot="selfie")


def test_choosing_a_slot_for_another_cases_document_is_refused(conn, checklist):
    store.ensure_case(conn, "c2")
    attached = cases.attach(conn, "c2", PNG, "p.png", checklist=checklist, read_text=no_ocr)
    with pytest.raises(LookupError):
        cases.choose_slot(conn, "c1", attached.document_id, "claim_form", checklist)


def test_the_photo_itself_is_not_kept_without_consent(conn, checklist, tmp_path):
    attached = attach(conn, checklist, slot="id_proof")
    assert store.get_document(conn, attached.document_id)["retained"] == 0
    assert not (tmp_path / "originals").exists()


def test_the_photo_is_kept_with_consent(conn, checklist, tmp_path):
    store.record_consent(conn, "c1", "keep_original", True)
    attached = attach(conn, checklist, slot="id_proof")
    assert store.get_document(conn, attached.document_id)["retained"] == 1


# --- Feeding documents_incomplete --------------------------------------------


def test_checklist_feeds_documents_incomplete(conn, checklist):
    rule = ladders.load("insurance_health_claim").rule("documents_incomplete")
    attach(conn, checklist, slot="final_bill")
    attach(conn, checklist, slot="policy_copy")

    state = cases.checklist_state(conn, "c1", checklist)
    assert cases.checklist_facts(state) == {"documents_collected": 2, "documents_required": 6}
    verdict = evaluate(Facts(**cases.checklist_facts(state)), [rule])
    assert verdict.outcome == le.DO_NOT_FILE_YET
    assert verdict.blocks[0].values["shortfall"] == 4

    for slot in SLOTS:
        attach(conn, checklist, slot=slot)
    full = cases.checklist_state(conn, "c1", checklist)
    assert evaluate(Facts(**cases.checklist_facts(full)), [rule]).outcome == le.FILE


# --- Messages ----------------------------------------------------------------


def test_status_message_names_what_is_missing_in_order(conn, checklist):
    attach(conn, checklist, slot="final_bill")
    attach(conn, checklist, slot="id_proof")
    message = cases.status_message(cases.checklist_state(conn, "c1", checklist), checklist)
    assert message == "Still missing, 4 of 6: Discharge summary, Policy copy, Prescriptions, Claim form."


def test_status_message_when_everything_is_in(conn, checklist):
    for slot in SLOTS:
        attach(conn, checklist, slot=slot)
    message = cases.status_message(cases.checklist_state(conn, "c1", checklist), checklist)
    assert message == "All 6 documents are in. Nothing is missing."
    assert "filed" not in message.lower()


def test_options_message_numbers_every_slot(checklist):
    message = cases.options_message(checklist)
    assert "1. Discharge summary" in message
    assert "6. Claim form" in message
