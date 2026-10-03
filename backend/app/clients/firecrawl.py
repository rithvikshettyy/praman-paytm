"""Firecrawl web search: the only place that talks to it. Text out is redacted first."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from app import config
from app.services.redact import redact

logger = logging.getLogger(__name__)

URL = "https://api.firecrawl.dev/v2/search"
SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
MAX_PAGE_CHARS = 5000
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _readable(markdown: str) -> str:
    """Page text without images and link targets: the words, in less room."""
    return _LINK.sub(r"\1", _IMAGE.sub("", markdown)).strip()[:MAX_PAGE_CHARS]


def allowed(url: str, domains: tuple[str, ...]) -> bool:
    """Whether the url's host is one of `domains` or under one; any host when `domains` is empty."""
    host = (urlparse(url).hostname or "").lower()
    return not domains or any(host == d or host.endswith("." + d) for d in domains)


def search(query: str, limit: int = 5, domains: tuple[str, ...] = (), scrape: bool = True) -> list[dict]:
    """[{title, url, markdown}] for the query, only from `domains` when given; [] when there is
    no key or the call fails. With scrape=False no page is fetched: "markdown" holds the result's
    description (cheaper, for checking that a page exists)."""
    if not config.FIRECRAWL_API_KEY:
        return []
    body = {"query": redact(query), "limit": limit, "country": "IN"}
    if scrape:
        body["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}
    if domains:
        body["includeDomains"] = list(domains)
    try:
        response = httpx.post(URL, json=body, timeout=config.FIRECRAWL_TIMEOUT,
                              headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"})
        response.raise_for_status()
        found = (response.json().get("data") or {}).get("web") or []
    except (httpx.HTTPError, ValueError) as err:
        logger.warning("Firecrawl search failed: %s", type(err).__name__)
        return []
    pages = []
    for item in found:
        url, text = item.get("url"), (item.get("markdown") if scrape else item.get("description")) or ""
        if isinstance(url, str) and url.startswith("https://") and allowed(url, domains) and text.strip():
            pages.append({"title": str(item.get("title") or ""), "url": url, "markdown": _readable(text)})
    return pages


def scrape(url: str) -> dict | None:
    """One page as {title, url, markdown}; None without a key, off the allowed sites, or on any failure."""
    if not config.FIRECRAWL_API_KEY or not url.startswith("https://"):
        return None
    try:
        response = httpx.post(SCRAPE_URL, json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                              timeout=config.FIRECRAWL_TIMEOUT,
                              headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"})
        response.raise_for_status()
        data = response.json().get("data") or {}
    except (httpx.HTTPError, ValueError) as err:
        logger.warning("Firecrawl scrape failed: %s", type(err).__name__)
        return None
    meta = data.get("metadata") or {}
    text = _readable(data.get("markdown") or "")
    if (meta.get("statusCode") or 200) != 200 or not text:
        return None
    return {"title": str(meta.get("title") or ""), "url": url, "markdown": text}
