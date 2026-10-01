"""The whole website in her language: POST /api/translate, Sarvam once per string, cached on disk."""

import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app.clients import sarvam
from app.main import app
from app.services import i18n

client = TestClient(app)


@pytest.fixture
def calls(tmp_path, monkeypatch):
    """A fake Sarvam that marks text as Marathi and records every call; a fresh disk cache."""
    seen = []

    def translate(text, target, source_language="auto", **kwargs):
        seen.append(text)
        return f"<{target}>{text}"

    monkeypatch.setattr(sarvam, "translate", translate)
    monkeypatch.setattr(config, "PAGE_TRANSLATIONS_PATH", tmp_path / "page_translations.json")
    monkeypatch.setattr(i18n, "_PAGE_CACHE", None)
    yield seen
    i18n._PAGE_CACHE = None


def ask(texts, language="mr-IN"):
    return client.post("/api/translate", json={"language": language, "texts": texts})


def test_texts_come_back_translated_in_the_same_order(calls):
    body = ask(["Claim readiness", "Use demo files", "Claim readiness"]).json()
    assert body["translations"] == ["<mr-IN>Claim readiness", "<mr-IN>Use demo files", "<mr-IN>Claim readiness"]
    assert sorted(calls) == ["Claim readiness", "Use demo files"]  # each distinct string once


def test_english_is_returned_as_it_is(calls):
    assert ask(["Claim readiness"], "en-IN").json()["translations"] == ["Claim readiness"]
    assert calls == []


def test_amounts_percentages_and_dates_are_never_sent_for_translation(calls):
    [out] = ask(["About ₹35,625 will be cut at 1% from 05/10/2026."]).json()["translations"]
    assert calls == ["About [[0]] will be cut at [[1]] from [[2]]."]
    assert out == "<mr-IN>About ₹35,625 will be cut at 1% from 05/10/2026."


def test_a_lost_amount_keeps_the_english(calls, monkeypatch):
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: "कपात होईल")  # [[0]] dropped
    assert ask(["About ₹35,625 will be cut."]).json()["translations"] == ["About ₹35,625 will be cut."]


def test_a_sarvam_failure_keeps_the_english(calls, monkeypatch):
    def down(*a, **k):
        raise sarvam.SarvamUnavailable("down")

    monkeypatch.setattr(sarvam, "translate", down)
    assert ask(["My policies"]).json()["translations"] == ["My policies"]


def test_a_string_is_translated_once_ever_and_kept_on_disk(calls):
    ask(["Claim readiness"])
    i18n._PAGE_CACHE = None  # as after a restart
    ask(["Claim readiness"])
    assert calls == ["Claim readiness"]
    saved = json.loads(config.PAGE_TRANSLATIONS_PATH.read_text(encoding="utf-8"))
    assert saved == {"mr-IN": {"Claim readiness": "<mr-IN>Claim readiness"}}


@pytest.mark.parametrize("body", [
    {"language": "xx-IN", "texts": ["hi"]},
    {"language": "mr-IN", "texts": "hi"},
    {"language": "mr-IN", "texts": [1]},
    {"language": "mr-IN", "texts": ["x"] * (i18n.MAX_TEXTS + 1)},
    {"language": "mr-IN", "texts": ["x" * (i18n.MAX_CHARS + 1)]},
])
def test_bad_requests_are_refused(calls, body):
    assert client.post("/api/translate", json=body).status_code == 400
    assert calls == []


def test_policy_is_the_insurance_word_not_the_government_one(calls):
    [out] = ask(["Upload your policy"], "hi-IN").json()["translations"]
    assert calls == ["Upload your [[0]]"]
    assert out == "<hi-IN>Upload your पॉलिसी"


def test_every_supported_language_has_a_word_for_policy():
    assert set(i18n.POLICY_WORD) == set(config.SUPPORTED_LANGUAGES) - {config.DEFAULT_LANGUAGE}


def test_a_symbol_the_translator_repeats_is_said_once(calls, monkeypatch):
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: text.replace("[[0]]", "[[0]]%"))
    assert ask(["Limit is 1% a day"]).json()["translations"] == ["Limit is 1% a day"]


def test_the_name_praman_is_never_translated(calls):
    [out] = ask(["Ask Praman about your claim"]).json()["translations"]
    assert calls == ["Ask [[0]] about your claim"]
    assert out == "<mr-IN>Ask Praman about your claim"


def test_case_is_kese_not_a_lawsuit_in_marathi_and_hindi(calls):
    assert ask(["My case"], "mr-IN").json()["translations"] == ["<mr-IN>My केस"]
    assert ask(["My case"], "ta-IN").json()["translations"] == ["<ta-IN>My case"]  # no glossary word: translated as is


def test_a_label_gets_no_full_stop_the_english_did_not_have(calls, monkeypatch):
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: f"{text} चालू करा।")
    assert ask(["Voice on", "Save it."]).json()["translations"] == ["Voice on चालू करा", "Save it. चालू करा।"]


def test_a_rate_limit_is_waited_out_then_retried(calls, monkeypatch):
    attempts, waits = [], []

    def busy_then_fine(text, target, **k):
        attempts.append(text)
        if len(attempts) < 3:
            raise sarvam.SarvamUnavailable("Sarvam API error: [429] Rate limit exceeded")
        return f"<{target}>{text}"

    monkeypatch.setattr(sarvam, "translate", busy_then_fine)
    monkeypatch.setattr(i18n, "_sleep", waits.append)
    assert ask(["Check a claim"]).json()["translations"] == ["<mr-IN>Check a claim"]
    assert len(attempts) == 3 and waits == [1.5, 3.0]


def test_other_failures_are_not_retried(calls, monkeypatch):
    attempts = []

    def broken(text, target, **k):
        attempts.append(text)
        raise sarvam.SarvamBadRequest("400 bad request")

    monkeypatch.setattr(sarvam, "translate", broken)
    monkeypatch.setattr(i18n, "_sleep", lambda s: None)
    assert ask(["Check a claim"]).json()["translations"] == ["Check a claim"]
    assert len(attempts) == 1


def test_chat_replies_keep_the_name_praman_too(monkeypatch):
    sent = []
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: sent.append(text) or f"<{target}>{text}")
    i18n._CACHE.clear()
    assert i18n.translate("Hello, I am Praman.", "mr-IN") == "<mr-IN>Hello, I am Praman."
    assert sent == ["Hello, I am [[90]]."]
    i18n._CACHE.clear()


def test_a_lost_name_token_falls_back_to_the_text_as_written(monkeypatch):
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: "नमस्कार" if "[[90]]" in text else f"<{target}>{text}")
    i18n._CACHE.clear()
    assert i18n.translate("Hello, I am Praman.", "mr-IN") == "<mr-IN>Hello, I am Praman."
    i18n._CACHE.clear()
