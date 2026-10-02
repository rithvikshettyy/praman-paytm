"""Nothing here touches the network: Sarvam and Meta are always faked."""

import pytest

from app import config
from app.clients import sarvam


@pytest.fixture(autouse=True)
def no_live_sarvam(monkeypatch):
    """A real key in backend/.env must never reach a test: any call a test forgot to fake fails here."""
    monkeypatch.setattr(config, "SARVAM_API_KEY", "")
    monkeypatch.setattr(sarvam, "_client", None)
