"""Options in the "Find a policy" list: a blank line between them, and a link to the distributor's own page
for the insurer when one is listed in data/policy_pages.yaml (never a made-up link)."""

import pytest

from app import config
from app.clients import sarvam
from app.services import i18n, policy_search

SITE = "insure.example"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "POLICY_SEARCH_DOMAINS", (SITE,))
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: text)
    i18n._CACHE.clear()
    yield
    i18n._CACHE.clear()


@pytest.mark.parametrize("insurer, kind, expected", [
    ("Go Digit General Insurance", None, f"https://{SITE}/general-insurance/companies/digit/"),
    ("SBI General Insurance", "health", f"https://{SITE}/general-insurance/companies/sbi-general/"),
    ("Star Health and Allied Insurance", "health", f"https://{SITE}/health-insurance/companies/star/"),
    ("Care Health Insurance", "health", f"https://{SITE}/health-insurance/companies/care/"),
    ("HDFC ERGO General Insurance", "health", f"https://{SITE}/health-insurance/companies/hdfc-ergo/"),
    ("HDFC ERGO General Insurance", None, f"https://{SITE}/general-insurance/companies/hdfc-ergo/"),
    ("Max Life Insurance", "life", f"https://{SITE}/term-insurance/companies/max/"),
    ("National Insurance", "motor", f"https://{SITE}/car-insurance/companies/national/"),
])
def test_a_listed_insurer_gets_its_page(insurer, kind, expected):
    assert policy_search.site_link(insurer, kind) == expected


@pytest.mark.parametrize("insurer", ["Digital Insurance Co", "Some Unknown Insurer", "", None])
def test_an_insurer_that_is_not_listed_gets_no_link(insurer):
    assert policy_search.site_link(insurer, "health") is None


def test_no_site_means_no_link(monkeypatch):
    monkeypatch.setattr(config, "POLICY_SEARCH_DOMAINS", ())
    assert policy_search.site_link("Go Digit General Insurance") is None


OPTIONS = [
    {"policy": "Plan A", "insurer": "Go Digit General Insurance", "features": "Covers hospital stays.", "url": "https://godigit.com/a"},
    {"policy": "Plan B", "insurer": "Unlisted Insurer", "features": "", "url": "https://unlisted.example/b"},
    {"policy": "Plan C", "insurer": "Star Health", "features": "Senior plan.", "url": f"https://{SITE}/health-insurance/companies/star/"},
]


def test_each_option_is_its_own_block_with_a_blank_line_between():
    text = policy_search._render(OPTIONS, "health insurance", "en-IN", "health")
    blocks = text.split("\n\n")
    firsts = [b for b in blocks if b[:2] in ("1.", "2.", "3.")]
    assert len(firsts) == 3 and [b[:2] for b in firsts] == ["1.", "2.", "3."]


def test_a_mapped_insurer_gets_a_link_line_and_an_unmapped_one_does_not():
    text = policy_search._render(OPTIONS, "health insurance", "en-IN", "health")
    one, two, three = [b for b in text.split("\n\n") if b[:2] in ("1.", "2.", "3.")]
    assert f"Compare and buy: https://{SITE}/general-insurance/companies/digit/" in one
    assert "Compare and buy" not in two
    assert f"Compare and buy: https://{SITE}/health-insurance/companies/star/" in three  # the page it was read from


def test_nothing_changes_without_a_site(monkeypatch):
    monkeypatch.setattr(config, "POLICY_SEARCH_DOMAINS", ())
    text = policy_search._render(OPTIONS, "health insurance", "en-IN", "health")
    assert "Compare and buy" not in text and "[godigit.com]" in text
