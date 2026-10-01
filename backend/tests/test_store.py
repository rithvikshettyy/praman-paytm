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
    connection = store.connect(tmp_path / "praman.db")
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


def test_c7_tables_exist(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"documents", "consents"} <= tables
    columns = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"id", "case_id", "doc_type", "slot", "received_at", "fields", "confidence", "retained"} <= columns


def test_many_connections_at_once_do_not_trip_over_the_console_view(tmp_path):
    """The console fetches counters and cases together; their connections must not race."""
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "praman.db"
    store.connect(path).close()

    def open_and_read(_):
        conn = store.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM console_cases").fetchone()[0]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(open_and_read, range(64))) == [0] * 64
