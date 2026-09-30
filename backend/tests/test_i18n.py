import pytest

from app.clients import sarvam
from app.services import i18n


@pytest.fixture(autouse=True)
def empty_cache():
    i18n._CACHE.clear()
    yield
    i18n._CACHE.clear()


def test_same_or_unsupported_language_passes_through_without_a_call(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("Sarvam must not be called")

    monkeypatch.setattr(sarvam, "translate", boom)
    assert i18n.translate("hello", "en-IN") == "hello"
    assert i18n.translate("hello", "xx-IN") == "hello"


def test_failure_returns_the_original_text(monkeypatch):
    def fail(*args, **kwargs):
        raise sarvam.SarvamUnavailable("down")

    monkeypatch.setattr(sarvam, "translate", fail)
    assert i18n.translate("hello", "mr-IN") == "hello"


def test_translation_is_cached(monkeypatch):
    calls = []

    def fake(text, target, **kwargs):
        calls.append(text)
        return f"[{target}] {text}"

    monkeypatch.setattr(sarvam, "translate", fake)
    assert i18n.translate("hello", "mr-IN") == "[mr-IN] hello"
    assert i18n.translate("hello", "mr-IN") == "[mr-IN] hello"
    assert calls == ["hello"]
