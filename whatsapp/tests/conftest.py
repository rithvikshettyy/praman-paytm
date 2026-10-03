"""Nothing here touches the network: Sarvam and Meta are always faked."""

import pytest

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
    import mongomock

    from app import store

    monkeypatch.setattr(store, "_client", mongomock.MongoClient())
    monkeypatch.setattr(store, "_indexed", set())
