import pytest

from app.clients import sarvam
from app.services import documents
from tests.conftest import make_pdf

PDF = "application/pdf"


def test_empty_file_rejected():
    with pytest.raises(documents.UploadRejected):
        documents.validate_upload(b"", "x.pdf", PDF)


def test_executable_rejected():
    with pytest.raises(documents.UploadRejected):
        documents.validate_upload(b"MZ\x90\x00exe", "evil.exe", "application/x-msdownload")


def test_pdf_detected_by_magic_bytes_not_declared_type():
    assert documents.validate_upload(b"%PDF-1.7 body", "doc.bin", "application/octet-stream") == PDF


def test_long_pdf_split_at_the_ten_page_cap():
    batches = documents.split_into_batches(make_pdf(25), "policy.pdf", PDF)
    counts = [documents.count_pages(b.data, PDF) for b in batches]

    assert [b.page_offset for b in batches] == [0, 10, 20]
    assert all(c <= 10 for c in counts)
    assert sum(counts) == 25


def test_short_pdf_not_split():
    assert len(documents.split_into_batches(make_pdf(4), "small.pdf", PDF)) == 1


def test_submit_without_schema_only_digitises(fake_sarvam):
    jobs = documents.submit(make_pdf(1), "bill.pdf", mime_type=PDF)
    assert [j["kind"] for j in jobs] == ["digitise"]


def test_submit_with_schema_extracts_lead_batches_only(fake_sarvam):
    jobs = documents.submit(make_pdf(25), "policy.pdf", mime_type=PDF, schema={"type": "object"})
    kinds = [j["kind"] for j in jobs]
    assert kinds.count("digitise") == 3
    assert kinds.count("extract") == documents.EXTRACT_MAX_BATCHES


def test_submit_turns_a_sarvam_rejection_into_upload_rejected(monkeypatch):
    def reject(*args, **kwargs):
        raise sarvam.SarvamBadRequest("bad schema")

    monkeypatch.setattr(sarvam, "digitise", reject)
    with pytest.raises(documents.UploadRejected):
        documents.submit(make_pdf(1), "bill.pdf", mime_type=PDF)


def test_collect_waits_while_a_job_is_running(fake_sarvam):
    jobs = documents.submit(make_pdf(1), "policy.pdf", mime_type=PDF)
    fake_sarvam.statuses[jobs[0]["job_id"]] = "running"

    assert documents.collect(jobs)["status"] == "processing"


def test_collect_parses_text_blocks_and_typed_fields(fake_sarvam):
    jobs = documents.submit(make_pdf(1), "policy.pdf", mime_type=PDF, schema={"type": "object"})
    result = documents.collect(
        jobs,
        numeric_fields={"sum_insured", "room_cap_per_day"},
        integer_fields={"ped_wait_months"},
    )

    assert result["status"] == "parsed"
    assert "Sum Insured" in result["text"]
    assert result["markdown"]
    assert all(b["page_number"] == 1 for b in result["page_blocks"])
    assert result["fields"]["sum_insured"] == 500000.0
    assert result["fields"]["ped_wait_months"] == 36
    # Unreadable becomes a missing fact, never a guess.
    assert result["fields"]["room_cap_per_day"] is None
    assert result["fields"]["insurer"] == "Example General Insurance Ltd"


def test_collect_fails_when_every_job_failed(fake_sarvam):
    jobs = documents.submit(make_pdf(1), "policy.pdf", mime_type=PDF)
    fake_sarvam.statuses[jobs[0]["job_id"]] = "failed"

    result = documents.collect(jobs)
    assert result["status"] == "failed"
    assert "failed" in result["error"]


def test_merge_keeps_the_first_non_empty_value():
    merged = documents._merge_extracted({"a": "first", "b": ""}, {"a": "second", "b": "filled"})
    assert merged == {"a": "first", "b": "filled"}


def test_extract_payload_unwraps_nested_fields():
    assert documents._extract_payload({"result": {"fields": {"x": 1}}}) == {"x": 1}
    assert documents._extract_payload({"result": '```json\n{"x": 2}\n```'}) == {"x": 2}
