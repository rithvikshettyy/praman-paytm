"""Policies found online: options from fetched pages only, nothing invented, nothing promised."""

import pytest

from app import config
from app.clients import firecrawl, sarvam
from app.services import policy_search

PAGE = "https://www.care.example/supreme"
TEXT = "Care Supreme covers 4 members. Sum insured 5,00,000 available. No room rent cap."
REAL_SCRAPE, REAL_CHECK = firecrawl.scrape, policy_search._insurer_check  # the fixture stubs them


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    policy_search._CACHE.clear()
    policy_search._PAGES.clear()
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")
    monkeypatch.setattr(config, "POLICY_SEARCH_DOMAINS", ("care.example",))  # whatever backend/.env says
    monkeypatch.setattr(firecrawl, "scrape", lambda url: None)  # no insurer page loads: the search is used
    monkeypatch.setattr(policy_search, "_insurer_check", lambda option, sites: "unchecked")
    monkeypatch.setattr(policy_search.i18n, "translate", lambda text, language, **k: text)
    yield
    policy_search._CACHE.clear()


def serve(monkeypatch, options, pages=None):
    sent = []
    pages = [{"title": "t", "url": PAGE, "markdown": TEXT}] if pages is None else pages
    monkeypatch.setattr(firecrawl, "search", lambda q, limit=5, domains=(), scrape=True: sent.append(q) or pages)
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
    serve(monkeypatch, [option(features="Sum insured 9,99,000."), option(features="Sum insured 5,00,000 available.")])
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


def test_the_search_is_limited_to_the_configured_sites(monkeypatch):
    monkeypatch.setattr(config, "POLICY_SEARCH_DOMAINS", ("insure.example",))
    seen = {}
    monkeypatch.setattr(firecrawl, "search", lambda q, limit=5, domains=(), scrape=True: seen.update(domains=domains) or [])
    policy_search.suggest("health")
    assert seen["domains"] == ("insure.example",)


def test_firecrawl_drops_pages_from_other_sites_and_sends_the_allowlist(monkeypatch):
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "k")
    sent = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"web": [
            {"url": "https://www.insure.example/a", "markdown": "x"}, {"url": "https://sub.insure.example/b", "markdown": "x"},
            {"url": "https://other.example/c", "markdown": "x"}, {"url": "https://notinsure.example/d", "markdown": "x"}]}}

    monkeypatch.setattr(firecrawl.httpx, "post", lambda url, json, timeout, headers: sent.update(json) or Resp())
    pages = firecrawl.search("q", domains=("insure.example",))
    assert [p["url"] for p in pages] == ["https://www.insure.example/a", "https://sub.insure.example/b"]
    assert sent["includeDomains"] == ["insure.example"]


SITE = "insure.example"


def insurer_pages(monkeypatch, loads=lambda url: True):
    """Insurer pages on SITE; `loads` says which of them come back."""
    monkeypatch.setattr(config, "POLICY_SEARCH_DOMAINS", (SITE,))
    scraped = []

    def scrape(url):
        scraped.append(url)
        return {"title": "t", "url": url, "markdown": TEXT} if loads(url) else None

    monkeypatch.setattr(firecrawl, "scrape", scrape)
    monkeypatch.setattr(firecrawl, "search", lambda *a, **k: pytest.fail("fell back to search"))
    return scraped


def test_the_insurer_pages_of_the_site_are_read_before_any_search(monkeypatch):
    scraped = insurer_pages(monkeypatch)
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: {"options": [option(url=scraped[0])]})
    text = policy_search.suggest("health")
    assert text and f"[{SITE}]" in text
    assert 1 <= len(scraped) <= policy_search._TRY and all(u.startswith(f"https://{SITE}/health-insurance/") for u in scraped)


def test_pages_that_do_not_load_are_skipped_and_at_most_four_are_read(monkeypatch):
    insurer_pages(monkeypatch, loads=lambda url: "care" not in url)
    pages = policy_search._insurer_pages("health")
    assert 0 < len(pages) <= policy_search._KEEP and not any("care" in p["url"] for p in pages)


def test_the_insurer_pages_are_kept_for_a_while(monkeypatch):
    scraped = insurer_pages(monkeypatch)
    policy_search._insurer_pages("life")
    n = len(scraped)
    policy_search._insurer_pages("life")
    assert len(scraped) == n


def test_when_no_insurer_page_loads_the_search_is_used(monkeypatch):
    insurer_pages(monkeypatch, loads=lambda url: False)
    sent, url = [], f"https://{SITE}/found"
    monkeypatch.setattr(firecrawl, "search", lambda q, limit=5, domains=(), scrape=True: sent.append(domains) or [{"title": "t", "url": url, "markdown": TEXT}])
    monkeypatch.setattr(sarvam, "chat_json", lambda *a, **k: {"options": [option(url=url)]})
    assert policy_search.suggest("health") and sent == [(SITE,)]


def test_no_site_set_means_no_insurer_pages(monkeypatch):
    monkeypatch.setattr(config, "POLICY_SEARCH_DOMAINS", ())
    monkeypatch.setattr(firecrawl, "scrape", lambda url: pytest.fail("scraped"))
    assert policy_search._insurer_pages("health") == []


def test_every_kind_has_pages_listed():
    import yaml
    kinds = yaml.safe_load(config.POLICY_PAGES_FILE.read_text(encoding="utf-8"))["kinds"]
    assert set(kinds) == set(policy_search.KINDS) and all(len(v) >= policy_search._KEEP for v in kinds.values())


def test_scrape_returns_readable_text_and_skips_a_404(monkeypatch):
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "k")
    replies = iter([
        {"data": {"markdown": "![x](http://i/p.png) See [Care Joy](http://a/b) plan", "metadata": {"statusCode": 200, "title": "T"}}},
        {"data": {"markdown": "# 404", "metadata": {"statusCode": 404}}},
    ])

    class Resp:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    monkeypatch.setattr(firecrawl.httpx, "post", lambda *a, **k: Resp(next(replies)))
    monkeypatch.setattr(firecrawl, "scrape", REAL_SCRAPE)
    page = firecrawl.scrape("https://insure.example/a")
    assert page["markdown"] == "See Care Joy plan" and page["title"] == "T"
    assert firecrawl.scrape("https://insure.example/b") is None
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")
    assert firecrawl.scrape("https://insure.example/a") is None


def test_the_option_text_is_translated_but_names_and_sites_are_not(monkeypatch):
    serve(monkeypatch, [option()])
    monkeypatch.setattr(policy_search.i18n, "translate", lambda text, language, **k: f"[{language}] {text}")
    text = policy_search.suggest("health", language="hi-IN")
    assert "Care Supreme (Care): [hi-IN] Covers 4 members. [care.example]" in text
    assert text.startswith("[hi-IN] Options found online")


# --- Fail closed and verification -------------------------------------------------


def test_no_site_set_means_no_suggestions_and_no_open_web_search(monkeypatch):
    monkeypatch.setattr(config, "POLICY_SEARCH_DOMAINS", ())
    monkeypatch.setattr(firecrawl, "search", lambda *a, **k: pytest.fail("searched the open web"))
    monkeypatch.setattr(firecrawl, "scrape", lambda *a, **k: pytest.fail("scraped"))
    assert policy_search.suggest("health") is None


def test_an_option_off_the_site_is_dropped_even_when_its_page_was_fetched(monkeypatch):
    off = "https://www.other.example/plan"
    serve(monkeypatch, [option(url=off)], pages=[{"title": "t", "url": off, "markdown": TEXT}])
    assert policy_search.suggest("health") is None


def test_a_name_not_on_the_page_is_dropped_and_an_insurer_not_on_it_is_blanked(monkeypatch):
    serve(monkeypatch, [option(policy="Care Platinum"), option(insurer="Star Health")])
    text = policy_search.suggest("health")
    assert "Care Platinum" not in text and "Star Health" not in text
    assert "1. Care Supreme: Covers 4 members. [care.example]" in text


def test_a_paraphrased_sentence_is_not_shown_but_the_name_is(monkeypatch):
    serve(monkeypatch, [option(features="A plan that protects the whole family.")])
    text = policy_search.suggest("health")
    assert "1. Care Supreme (Care) [care.example]" in text and "protects" not in text


def test_matching_ignores_case_and_punctuation():
    page = policy_search._normalise(TEXT)
    assert policy_search._quoted("covers 4 MEMBERS!", page) and not policy_search._quoted("covers 5 members", page)


def insurer_search(monkeypatch, found):
    monkeypatch.setattr(policy_search, "_insurer_check", REAL_CHECK)
    calls = []

    def search(q, limit=5, domains=(), scrape=True):
        calls.append((q, domains, scrape))
        return found

    monkeypatch.setattr(firecrawl, "search", search)
    return calls


SITES = [("care", "careinsurance.com"), ("lic", "licindia.in")]


def test_the_insurer_site_confirms_a_plan_by_name(monkeypatch):
    calls = insurer_search(monkeypatch, [{"title": "Care Supreme | Care Health", "url": "https://careinsurance.com/x", "markdown": ""}])
    assert REAL_CHECK({"policy": "Care Supreme", "insurer": "Care"}, SITES) == "confirmed"
    assert calls == [('"Care Supreme"', ("careinsurance.com",), False)]


def test_a_plan_the_insurer_site_does_not_show_is_marked_not_confirmed(monkeypatch):
    insurer_search(monkeypatch, [{"title": "Other plan", "url": "https://careinsurance.com/y", "markdown": "something else"}])
    assert REAL_CHECK({"policy": "Care Supreme", "insurer": "Care"}, SITES) == "not_confirmed"


def test_an_unknown_insurer_is_unchecked_and_lic_is_not_found_inside_policy(monkeypatch):
    insurer_search(monkeypatch, [])
    assert REAL_CHECK({"policy": "Family Policy", "insurer": "Acme"}, SITES) == "unchecked"


def test_the_marks_are_shown_and_translated(monkeypatch):
    serve(monkeypatch, [option()])
    monkeypatch.setattr(policy_search, "_insurer_check", lambda o, sites: "confirmed")
    assert f"[care.example] ✓ {policy_search.CONFIRMED}" in policy_search.suggest("health")
    policy_search._CACHE.clear()
    monkeypatch.setattr(policy_search, "_insurer_check", lambda o, sites: "not_confirmed")
    assert f"[care.example] · {policy_search.NOT_CONFIRMED}" in policy_search.suggest("health")


def test_every_listed_insurer_has_a_domain():
    import yaml
    insurers = yaml.safe_load(config.POLICY_PAGES_FILE.read_text(encoding="utf-8"))["insurers"]
    assert insurers and all("." in d and "/" not in d for d in insurers.values())


def test_health_shows_the_policy_sites_but_never_the_key(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "secret-key")
    body = TestClient(app).get("/api/health").json()
    assert body["policy_search"] == {"firecrawl": True, "sites": ["care.example"]}
    assert "secret-key" not in str(body)
