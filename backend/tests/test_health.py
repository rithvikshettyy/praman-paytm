from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_health_degraded_without_a_sarvam_key(monkeypatch):
    monkeypatch.setattr(config, "SARVAM_API_KEY", "")
    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["sarvam_configured"] is False


def test_api_health_never_echoes_the_key(monkeypatch):
    monkeypatch.setattr(config, "SARVAM_API_KEY", "sk-secret-value")
    response = client.get("/api/health")

    assert response.status_code == 200
    assert "sk-secret-value" not in response.text
    assert "mr-IN" in response.json()["supported_languages"]
