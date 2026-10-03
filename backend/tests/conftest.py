"""Shared fixtures. Nothing here touches the network: Sarvam is always faked."""

from __future__ import annotations

import io

import mongomock
import pytest
from pypdf import PdfWriter

from app import config
from app.clients import sarvam


@pytest.fixture(autouse=True)
def no_live_sarvam(monkeypatch):
    """A real key in backend/.env must never reach a test: any call a test forgot to fake fails here."""
    monkeypatch.setattr(config, "SARVAM_API_KEY", "")
    monkeypatch.setattr(sarvam, "_client", None)


@pytest.fixture(autouse=True)
def fresh_mongo(monkeypatch):
    """Every test gets its own empty in-memory MongoDB; none touches a server."""
    from app import store

    monkeypatch.setattr(store, "_client", mongomock.MongoClient())
    monkeypatch.setattr(store, "_indexed", set())


POLICY_TEXT = """HEALTH INSURANCE POLICY SCHEDULE
Policy Number: EX-000-0000 (example case, not a real policy)
Sum Insured: Rs 5,00,000
Room rent limit: 1% of sum insured per day.
Pre-existing diseases are covered after a waiting period of 36 months.
Cataract surgery has a specified waiting period of 24 months."""

EXTRACTED = {
    "sum_insured": "₹5,00,000",   # deliberately messy - must be coerced
    "ped_wait_months": "36",
    "room_cap_per_day": "not stated",
    "insurer": "Example General Insurance Ltd",
}


def make_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class FakeSarvam:
    """Deterministic stand-in for the Doc AI job lifecycle."""

    def __init__(self):
        self.jobs: dict[str, str] = {}
        self.statuses: dict[str, str] = {}
        self.counter = 0
        # What the next digitise / extract jobs return; tests may replace these.
        self.text = POLICY_TEXT
        self.extracted: dict = EXTRACTED
        self.schemas: list[dict] = []  # every schema an extract job was submitted with

    def digitise(self, data, filename, **kwargs):
        return self._new("digitise")

    def extract(self, data, filename, schema, **kwargs):
        self.schemas.append(schema)
        return self._new("extract")

    def _new(self, kind: str) -> str:
        self.counter += 1
        job_id = f"{kind[:3]}-{self.counter}"
        self.jobs[job_id] = kind
        self.statuses[job_id] = "completed"
        return job_id

    def job_status(self, job_id):
        return {"job_id": job_id, "status": self.statuses[job_id], "usage": {"pages_total": 1}}

    def job_results(self, job_id):
        if self.jobs.get(job_id) == "digitise":
            # The real Doc AI shape: pages use `page_num`, carry no `content`,
            # and hold `blocks` with `layout_tag` / `coordinates`.
            return {
                "documents": [
                    {
                        "pages": [
                            {
                                "page_num": 1,
                                "blocks": [
                                    {
                                        "text": self.text,
                                        "layout_tag": "paragraph",
                                        "coordinates": {"x1": 0, "y1": 0, "x2": 2550, "y2": 3300},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        return {"result": self.extracted}


@pytest.fixture
def fake_sarvam(monkeypatch) -> FakeSarvam:
    fake = FakeSarvam()
    for name in ("digitise", "extract", "job_status", "job_results"):
        monkeypatch.setattr(sarvam, name, getattr(fake, name))
    return fake
