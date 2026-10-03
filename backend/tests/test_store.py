"""C7 retention: extracted fields are stored; the original is discarded unless she consents."""

from datetime import date

import pytest

from app import store
from app.services.documents import Extraction, Field

EXTRACTION = Extraction(
    doc_type="policy",
    detected=False,
    source="doc_ai",
    fields={
        "sum_insured": Field(500000.0, 0.96),
        "policy_start_date": Field(date(2019, 4, 1), 0.9),
        "continuous_cover_start": Field(None, 0.0),
    },
)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect()
    yield connection
    connection.close()


def test_default_keeps_fields_and_discards_the_original(conn, tmp_path):
    originals = tmp_path / "originals"
    row = store.save_document(conn, "case-1", EXTRACTION, original=b"%PDF-secret", filename="policy.pdf", originals_dir=originals)

    saved = store.get_document(conn, row["id"])
    assert saved["retained"] == 0
    assert saved["fields"]["sum_insured"] == {"value": 500000.0, "confidence": 0.96}
    assert saved["fields"]["policy_start_date"]["value"] == "2019-04-01"
    assert not originals.exists() or not any(originals.rglob("*"))


def test_consent_to_keep_retains_the_original(conn, tmp_path):
    originals = tmp_path / "originals"
    store.record_consent(conn, "case-1", "keep_original", True)
    row = store.save_document(conn, "case-1", EXTRACTION, original=b"%PDF-keep", filename="policy.pdf", originals_dir=originals)

    assert store.get_document(conn, row["id"])["retained"] == 1
    [kept] = [p for p in originals.rglob("*") if p.is_file()]
    assert kept.read_bytes() == b"%PDF-keep"


def test_latest_consent_wins(conn, tmp_path):
    store.record_consent(conn, "case-1", "keep_original", True)
    store.record_consent(conn, "case-1", "keep_original", False)
    row = store.save_document(conn, "case-1", EXTRACTION, original=b"%PDF-", filename="p.pdf", originals_dir=tmp_path)
    assert store.get_document(conn, row["id"])["retained"] == 0


def test_consent_is_per_case(conn, tmp_path):
    store.record_consent(conn, "case-2", "keep_original", True)
    row = store.save_document(conn, "case-1", EXTRACTION, original=b"%PDF-", filename="p.pdf", originals_dir=tmp_path)
    assert store.get_document(conn, row["id"])["retained"] == 0


def test_no_consent_recorded_means_no(conn):
    assert "keep_original" in store.CONSENT_SCOPES
    assert store.has_consent(conn, "case-1", "keep_original") is False


def test_unknown_consent_scope_is_refused(conn):
    with pytest.raises(ValueError):
        store.record_consent(conn, "case-1", "sell_data", True)


def test_stored_confidence_is_the_weakest_field_that_was_read(conn, tmp_path):
    row = store.save_document(conn, "case-1", EXTRACTION, originals_dir=tmp_path)
    assert store.get_document(conn, row["id"])["confidence"] == 0.9


def test_c7_collections_hold_what_a_row_used_to(conn):
    row = store.save_document(conn, "case-1", EXTRACTION, slot="bill")
    assert {"id", "case_id", "doc_type", "slot", "received_at", "fields", "confidence", "retained"} <= set(row)
    assert isinstance(row["fields"], dict)  # a real sub-document, not a JSON string


def test_ids_count_up_and_are_never_reused(conn):
    first = store.record_event(conn, "case-1", "readiness_checked", {"outcome": "file"})
    second = store.record_event(conn, "case-1", "readiness_checked", {"outcome": "file"})
    assert second == first + 1
    conn.db.events.delete_many({})
    assert store.record_event(conn, "case-1", "readiness_checked", {}) == second + 1


def test_many_connections_at_once_open_one_case_per_sender():
    from concurrent.futures import ThreadPoolExecutor

    def open_and_find(_):
        conn = store.connect()
        try:
            return store.case_for_user(conn, "whatsapp:+910000000001")["id"]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(open_and_find, range(64)))) == 1


def test_the_console_rows_read_the_latest_event_of_each_kind(conn):
    store.ensure_case(conn, "c1")
    store.record_event(conn, "c1", "case_routed", {"respondent": "insurer", "distributor_owned": False})
    store.record_event(conn, "c1", "case_routed", {"respondent": "distributor", "distributor_owned": True})
    store.record_event(conn, "c1", "agent_requested", {"reason": "not_solved"})
    [row] = store.console_rows(conn)
    assert row["respondent"] == "distributor" and row["distributor_owned"] is True
    assert row["agent_requested"] is True and row["agent_status"] == "pending"
    store.record_event(conn, "c1", "case_status", {"status": "resolved"})
    assert store.console_rows(conn)[0]["agent_status"] == "resolved"


def test_the_transcript_is_masked_and_goes_with_the_case(conn):
    store.ensure_case(conn, "c1")
    store.record_message(conn, "c1", "user", "my PAN is ABCDE1234F", "en-IN", ("image/png",))
    [turn] = store.case_messages(conn, "c1")
    assert "ABCDE1234F" not in turn["text"] and turn["attachments"] == ["image/png"]
    store.save_pages(conn, "c1", "policy.pdf", "health_policy", [(1, "Aadhaar 1234 5678 9012 sum insured 5,00,000")])
    [doc] = store.case_pages(conn, "c1")
    assert "1234 5678 9012" not in doc["pages"][0][1] and "5,00,000" in doc["pages"][0][1]
    deleted = store.delete_case(conn, "c1")
    assert deleted["messages"] == 1 and deleted["doc_pages"] == 1
    assert store.case_messages(conn, "c1") == [] and store.case_pages(conn, "c1") == []
