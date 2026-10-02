"""News layer: recent headlines and summaries about insurance and rule changes, kept fresh in the background.

    python -m app.rag.news        refresh once (from backend/)

Sources are an allowlist in data/news_sources.yaml: https feeds only, each with a name and a
kind (official for a regulator, outlet for a news site). Only the feed's own title and summary are
read; no article page is fetched. An item is kept when its text mentions one of the allowlist's
keywords, and removed again after NEWS_MAX_AGE_DAYS.

News is context, not law. Its chunks carry layer "news", ``verified_by`` UNVERIFIED and a
doc_type of "news <date>", so a citation reads ``[Economic Times, news 2026-09-12, p.1]`` and the
answer adds a note that it comes from a press report. Nothing here writes to the fact sheet, the
engine or the store, and this module imports none of them.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import logging
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
import yaml

from app import config
from app.rag import index

logger = logging.getLogger(__name__)

LAYER = "news"
KINDS = ("official", "outlet")
MAX_FEED_BYTES = 3 * 1024 * 1024
MAX_TEXT_CHARS = 1500
_BATCH = 256


class NewsError(ValueError):
    """news_sources.yaml is wrong, or a feed is not safe to read."""


@dataclass(frozen=True)
class Feed:
    id: str
    name: str
    kind: str
    feed_url: str


@dataclass(frozen=True)
class Item:
    title: str
    summary: str
    link: str
    published: datetime.date


@dataclass
class RefreshReport:
    added: int = 0
    removed: int = 0
    skipped: int = 0  # feed items that matched no keyword, or were too old
    failed: list[str] = field(default_factory=list)  # feed ids that could not be read


def load_config(path: Path | str | None = None) -> tuple[list[Feed], tuple[str, ...]]:
    raw = yaml.safe_load(Path(path or config.NEWS_SOURCES_FILE).read_text(encoding="utf-8")) or {}
    keywords = tuple(str(k).strip().lower() for k in raw.get("keywords") or [] if str(k).strip())
    if not keywords:
        raise NewsError("news_sources.yaml needs a non-empty keywords list")
    feeds, seen = [], set()
    for item in raw.get("feeds") or []:
        if not isinstance(item, dict) or set(item) != {"id", "name", "kind", "feed_url"}:
            raise NewsError("every feed needs exactly: id, name, kind, feed_url")
        feed = Feed(*(str(item[key]).strip() for key in ("id", "name", "kind", "feed_url")))
        if not re.fullmatch(r"[a-z0-9_]+", feed.id) or feed.id in seen:
            raise NewsError(f"{feed.id!r}: id must be lowercase letters, digits or _, and unique")
        if feed.kind not in KINDS:
            raise NewsError(f"{feed.id}: kind must be one of {KINDS}")
        if urlparse(feed.feed_url).scheme != "https":
            raise NewsError(f"{feed.id}: only https feeds are allowed")
        seen.add(feed.id)
        feeds.append(feed)
    return feeds, keywords


# --- Reading a feed -----------------------------------------------------------


def fetch_text(url: str) -> str:
    """Download one feed. Size-capped; redirects stay on https."""
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Praman-news/1.0"}) as client:
        response = client.get(url)
        response.raise_for_status()
        if urlparse(str(response.url)).scheme != "https":
            raise NewsError(f"{url} redirected away from https")
        if len(response.content) > MAX_FEED_BYTES:
            raise NewsError(f"{url} is larger than {MAX_FEED_BYTES} bytes")
        try:
            return response.content.decode("utf-8").lstrip("﻿")
        except UnicodeDecodeError:
            return response.content.decode("cp1252", errors="replace")


def _clean(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(value or "")).replace("�", "")
    return " ".join(html.unescape(text).split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(value: str | None, today: datetime.date) -> datetime.date:
    if value:
        try:
            return parsedate_to_datetime(value).date()
        except (TypeError, ValueError):
            pass
        try:
            return datetime.date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    return today


def parse_feed(xml_text: str, today: datetime.date | None = None) -> list[Item]:
    """Items from an RSS 2.0 or Atom feed. A feed that declares entities is refused."""
    today = today or datetime.date.today()
    head = xml_text[:4000].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise NewsError("feed declares a DOCTYPE or entities")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NewsError(f"feed is not valid XML: {exc}") from exc
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields = {_local(child.tag): child for child in node}
        title = _clean(fields["title"].text) if "title" in fields else ""
        body = fields.get("description") if "description" in fields else fields.get("summary")
        summary = _clean(body.text) if body is not None else ""
        link_node = fields.get("link")
        link = (link_node.get("href") or link_node.text or "").strip() if link_node is not None else ""
        stamp = fields.get("pubDate") if "pubDate" in fields else fields.get("updated", fields.get("published"))
        if title and link:
            items.append(Item(title, summary, link, _date(stamp.text if stamp is not None else None, today)))
    return items


def relevant(item: Item, keywords: tuple[str, ...]) -> bool:
    text = f"{item.title} {item.summary}".lower()
    return any(re.search(r"\b" + re.escape(k), text) for k in keywords)


# --- Index ----------------------------------------------------------------------


def _id(feed: Feed, item: Item) -> str:
    return f"news/{feed.id}#{hashlib.sha1(item.link.encode('utf-8')).hexdigest()[:12]}"


def refresh(
    collection=None,
    *,
    fetch: Callable[[str], str] = fetch_text,
    today: datetime.date | None = None,
    sources_file: Path | str | None = None,
) -> RefreshReport:
    """Read every allowlisted feed once, add the new relevant items, drop the old ones.

    One feed failing never stops the others. Re-running is safe: an item's id comes from its link.
    """
    collection = collection if collection is not None else index.default_collection()
    today = today or datetime.date.today()
    feeds, keywords = load_config(sources_file)
    oldest = today - datetime.timedelta(days=config.NEWS_MAX_AGE_DAYS)
    report = RefreshReport()

    ids, documents, metadatas = [], [], []
    for feed in feeds:
        try:
            items = parse_feed(fetch(feed.feed_url), today)
        except Exception as exc:  # network, bad XML, oversized: skip this feed, keep the rest
            logger.warning("News feed %s failed: %s", feed.id, exc)
            report.failed.append(feed.id)
            continue
        for item in items:
            if item.published < oldest or item.published > today + datetime.timedelta(days=1) or not relevant(item, keywords):
                report.skipped += 1
                continue
            ids.append(_id(feed, item))
            documents.append(f"{item.title}. {item.summary}".strip()[:MAX_TEXT_CHARS])
            metadatas.append({
                "file": _id(feed, item), "file_hash": "", "layer": LAYER, "insurer": feed.name, "product": "",
                "doc_type": f"news {item.published.isoformat()}", "source_url": item.link,
                "effective_date": item.published.isoformat(), "verified_by": "UNVERIFIED",
                "published_ord": item.published.toordinal(), "kind": feed.kind, "page": 1, "chunk": 0,
            })

    known = set(collection.get(where={"layer": LAYER}, include=[])["ids"])
    for i in range(0, len(ids), _BATCH):
        collection.upsert(ids=ids[i: i + _BATCH], documents=documents[i: i + _BATCH], metadatas=metadatas[i: i + _BATCH])
    report.added = len(set(ids) - known)

    stale = collection.get(where={"$and": [{"layer": LAYER}, {"published_ord": {"$lt": oldest.toordinal()}}]}, include=[])["ids"]
    if stale:
        collection.delete(ids=stale)
    report.removed = len(stale)
    return report


# --- Background refresh -----------------------------------------------------------

_started = threading.Event()


def start_background(minutes: int | None = None) -> bool:
    """Refresh every ``minutes`` (default NEWS_REFRESH_MINUTES) on a daemon thread. 0 means off."""
    minutes = config.NEWS_REFRESH_MINUTES if minutes is None else minutes
    if minutes <= 0 or _started.is_set():
        return False
    _started.set()
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                report = refresh()
                logger.info("News refreshed: %d added, %d removed, failed feeds %s", report.added, report.removed, report.failed)
            except Exception as exc:  # the chat must never depend on this thread
                logger.warning("News refresh failed: %s", exc)
            stop.wait(minutes * 60)

    threading.Thread(target=loop, name="news-refresh", daemon=True).start()
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        report = refresh()
    except NewsError as exc:
        print(f"news_sources.yaml: {exc}")
        return 1
    print(f"{report.added} added, {report.removed} removed, {report.skipped} skipped, failed feeds: {report.failed or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
