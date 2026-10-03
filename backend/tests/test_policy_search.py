"""Policies found online: options from fetched pages only, nothing invented, nothing promised."""

import pytest

from app import config
from app.clients import firecrawl, sarvam
from app.services import policy_search

PAGE = "https://www.care.example/supreme"
TEXT = "Care Supreme covers 4 members. Sum insured 5,00,000 available. No room rent cap."


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    policy_search._CACHE.clear()
    monkeypatch.setattr(policy_search.i18n, "translate", lambda text, language, **k: text)
    yield
    policy_search._CACHE.clear()


def serve(monkeypatch, options, pages=None):
    sent = []
    pages = [{"title": "t", "url": PAGE, "markdown": TEXT}] if pages is None else pages
    monkeypatch.setattr(firecrawl, "search", lambda q, limit=5: sent.append(q) or pages)
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: {"options": options})
    return sent


def option(**kw):
    return {"policy": "Care Supreme", "insurer": "Care", "features": "Covers 4 members.", "url": PAGE, **kw}


def test_options_come_from_fetched_pages_with_site_names_only(monkeypatch):
    serve(monkeypatch, [option()])
    text = policy_search.suggest("health")
    assert "Care Supreme (Care): Covers 4 members. [care.example]" in text
    assert "https://" not in text and "no order" in text and policy_search.ASK_NEEDS in text


def test_an_invented_url_is_dropped(monkeypatch):
    serve(monkeypatch, [option(url="https://made.up/page")])
    assert policy_search.suggest("health") is None


def test_a_figure_not_on_the_page_is_not_shown(monkeypatch):
    serve(monkeypatch, [option(features="Sum insured 9,99,000."), option(policy="Care Plus", features="Sum insured 5,00,000.")])
    text = policy_search.suggest("health")
    assert "9,99,000" not in text and "5,00,000" in text


def test_a_promise_is_dropped(monkeypatch):
    serve(monkeypatch, [option(features="Your claim will definitely be paid.")])
    assert "definitely" not in policy_search.suggest("health")


def test_her_words_are_masked_and_the_cache_does_not_keep_them(monkeypatch):
    sent = serve(monkeypatch, [option()])
    policy_search.suggest("health", "my PAN ABCDE1234F, family of 4")
    assert sent and "ABCDE1234F" not in sent[0]
    assert all("ABCDE1234F" not in key and "family" not in key for key in policy_search._CACHE)


@pytest.mark.parametrize("pages,reply", [([], {"options": [option()]}), (None, None), (None, {"options": []})])
def test_no_pages_or_no_usable_answer_gives_none(monkeypatch, pages, reply):
    serve(monkeypatch, [], pages=pages)
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: reply)
    assert policy_search.suggest("health") is None


def test_unknown_kind_gives_none(monkeypatch):
    serve(monkeypatch, [option()])
    assert policy_search.suggest("pet") is None


def test_a_good_answer_is_cached_and_a_miss_is_not(monkeypatch):
    sent = serve(monkeypatch, [option()])
    policy_search.suggest("health"), policy_search.suggest("health")
    assert len(sent) == 1
    policy_search._CACHE.clear()
    serve(monkeypatch, [])
    policy_search.suggest("life"), policy_search.suggest("life")


def test_firecrawl_without_a_key_does_nothing(monkeypatch):
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")
    assert firecrawl.search("x") == []


def test_firecrawl_keeps_only_https_pages_with_text_and_masks_the_query(monkeypatch):
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "k")
    seen = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"web": [
            {"url": "https://a.example/x", "markdown": "text"}, {"url": "http://b.example", "markdown": "t"},
            {"url": "https://c.example", "markdown": " "}]}}

    def post(url, json, timeout, headers):
        seen.update(json)
        return Resp()

    monkeypatch.setattr(firecrawl.httpx, "post", post)
    pages = firecrawl.search("PAN ABCDE1234F policy")
    assert [p["url"] for p in pages] == ["https://a.example/x"] and "ABCDE1234F" not in seen["query"]
