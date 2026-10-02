"""News layer: allowlisted feeds -> labelled, expiring passages. No network: feeds are strings."""

import datetime

import pytest
import yaml

from app import config
from app.clients import sarvam
from app.rag import answer as rag_answer
from app.rag import index, ingest, news, retrieve
from app.services import i18n

TODAY = datetime.date(2026, 10, 2)


def rss(*items):
    body = "".join(
        f"<item><title>{t}</title><description>{d}</description><link>{l}</link><pubDate>{p}</pubDate></item>"
        for t, d, l, p in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


INSURANCE = ("IRDAI issues new rule on health claim settlement", "Insurers must settle cashless claims faster.",
             "https://news.example/a", "Thu, 01 Oct 2026 10:00:00 +0530")
OFFTOPIC = ("Cricket final tonight", "Match preview.", "https://news.example/b", "Thu, 01 Oct 2026 10:00:00 +0530")
OLD = ("Insurance rule from long ago", "Policyholders were told.", "https://news.example/c", "Mon, 01 Jan 2026 10:00:00 +0530")


@pytest.fixture
def sources(tmp_path):
    path = tmp_path / "news_sources.yaml"
    path.write_text(yaml.safe_dump({
        "keywords": ["insurance", "insurer", "claim settlement"],
        "feeds": [
            {"id": "reg", "name": "Example Regulator", "kind": "official", "feed_url": "https://reg.example/rss"},
            {"id": "paper", "name": "Example Daily", "kind": "outlet", "feed_url": "https://paper.example/rss"},
        ],
    }), encoding="utf-8")
    return path


@pytest.fixture
def collection(tmp_path):
    return index.open_collection(tmp_path / "index", index.HashingEmbedding())


def run(collection, sources, feeds):
    return news.refresh(collection, fetch=lambda url: feeds[url], today=TODAY, sources_file=sources)


def test_relevant_items_are_indexed_with_news_metadata(collection, sources):
    report = run(collection, sources, {"https://reg.example/rss": rss(INSURANCE, OFFTOPIC),
                                       "https://paper.example/rss": rss()})
    assert (report.added, report.skipped, report.failed) == (1, 1, [])
    meta = collection.get(include=["metadatas"])["metadatas"][0]
    assert meta["layer"] == "news" and meta["insurer"] == "Example Regulator" and meta["kind"] == "official"
    assert meta["doc_type"] == "news 2026-10-01" and meta["verified_by"] == "UNVERIFIED" and meta["page"] == 1
    assert meta["source_url"] == "https://news.example/a"


def test_a_second_run_adds_nothing_and_old_items_are_removed(collection, sources):
    feeds = {"https://reg.example/rss": rss(INSURANCE), "https://paper.example/rss": rss()}
    run(collection, sources, feeds)
    assert run(collection, sources, feeds).added == 0 and collection.count() == 1
    later = news.refresh(collection, fetch=lambda url: rss(),
                         today=TODAY + datetime.timedelta(days=config.NEWS_MAX_AGE_DAYS + 5), sources_file=sources)
    assert later.removed == 1 and collection.count() == 0


def test_items_older_than_the_window_are_not_indexed(collection, sources):
    report = run(collection, sources, {"https://reg.example/rss": rss(OLD), "https://paper.example/rss": rss()})
    assert report.added == 0 and collection.count() == 0


def test_one_failing_feed_does_not_stop_the_others(collection, sources):
    def fetch(url):
        if "reg." in url:
            raise OSError("down")
        return rss(INSURANCE)

    report = news.refresh(collection, fetch=fetch, today=TODAY, sources_file=sources)
    assert report.failed == ["reg"] and report.added == 1


@pytest.mark.parametrize("xml", [
    '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>',
    "not xml at all",
])
def test_a_feed_with_entities_or_bad_xml_is_refused(xml):
    with pytest.raises(news.NewsError):
        news.parse_feed(xml, TODAY)


def test_atom_feeds_are_read():
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Insurance update</title>'
            '<link href="https://a.example/1"/><updated>2026-09-30T08:00:00Z</updated>'
            "<summary>&lt;p&gt;Hello&lt;/p&gt;</summary></entry></feed>")
    [item] = news.parse_feed(atom, TODAY)
    assert (item.title, item.link, item.summary, item.published) == (
        "Insurance update", "https://a.example/1", "Hello", datetime.date(2026, 9, 30))


@pytest.mark.parametrize("change", [{"feed_url": "http://reg.example/rss"}, {"kind": "blog"}, {"id": "Bad Id"}])
def test_the_allowlist_only_accepts_https_known_kinds_and_clean_ids(tmp_path, change):
    feed = {"id": "reg", "name": "R", "kind": "official", "feed_url": "https://reg.example/rss", **change}
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump({"keywords": ["insurance"], "feeds": [feed]}), encoding="utf-8")
    with pytest.raises(news.NewsError):
        news.load_config(path)


def test_the_repo_allowlist_is_valid():
    feeds, keywords = news.load_config()
    assert feeds and keywords and all(f.feed_url.startswith("https://") for f in feeds)


def test_ingesting_the_corpus_leaves_news_alone(collection, sources, tmp_path):
    run(collection, sources, {"https://reg.example/rss": rss(INSURANCE), "https://paper.example/rss": rss()})
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    report = ingest.ingest(corpus, collection)
    assert report.removed == [] and collection.count() == 1


def test_news_is_searched_for_everyone_but_capped(collection, sources):
    topics = ["cashless", "ombudsman", "premium refunds", "portability", "network hospitals", "grievance cells"]
    many = [(f"Insurance claim settlement update on {t}", f"Insurers announce {t} changes this week.",
             f"https://news.example/{n}", INSURANCE[3]) for n, t in enumerate(topics)]
    run(collection, sources, {"https://reg.example/rss": rss(*many), "https://paper.example/rss": rss()})
    passages = retrieve.retrieve("insurance claim settlement", collection=collection)
    assert passages and {p.layer for p in passages} == {"news"}
    assert len(passages) == config.NEWS_MAX_PASSAGES


@pytest.fixture
def model(monkeypatch):
    state = type("Model", (), {"reply": None, "prompts": [], "systems": []})()

    def chat_json(messages, **kwargs):
        state.systems.append(messages[0]["content"])
        state.prompts.append(messages[-1]["content"])
        return state.reply

    monkeypatch.setattr(sarvam, "chat_json", chat_json)
    i18n._CACHE.clear()
    yield state
    i18n._CACHE.clear()


def test_an_answer_from_news_is_labelled_flagged_and_carries_the_note(collection, sources, model):
    run(collection, sources, {"https://reg.example/rss": rss(INSURANCE), "https://paper.example/rss": rss()})
    model.reply = {"answer": "IRDAI has issued a rule on faster cashless settlement [S1].", "sources": ["S1"]}
    result = rag_answer.answer("What is the new claim settlement rule?", intent="question", collection=collection)
    assert result.status == rag_answer.ANSWERED and result.news and result.unverified
    assert "[Example Regulator, news 2026-10-01, p.1]" in result.text_en
    assert rag_answer.NEWS_NOTE in result.text_en
    assert "press report" in model.systems[-1]
    assert "(Example Regulator, news 2026-10-01, p.1)" in model.prompts[-1]


def test_an_answer_from_documents_alone_has_no_news_note(tmp_path, model):
    col = index.open_collection(tmp_path / "docs", index.HashingEmbedding())
    corpus = tmp_path / "corpus"
    (corpus / "regulation").mkdir(parents=True)
    (corpus / "regulation" / "r.md").write_text(
        "Section 1 Moratorium\nAfter 60 months the insurer cannot reject for non-disclosure.", encoding="utf-8")
    (corpus / "sources.yaml").write_text(yaml.safe_dump({"sources": [
        {"file": "regulation/r.md", "layer": "regulation", "doc_type": "circular", "verified_by": "UNVERIFIED"}]}),
        encoding="utf-8")
    ingest.ingest(corpus, col)
    model.reply = {"answer": "The moratorium is 60 months [S1].", "sources": ["S1"]}
    result = rag_answer.answer("What is the moratorium period?", intent="question", collection=col)
    assert result.status == rag_answer.ANSWERED and not result.news and rag_answer.NEWS_NOTE not in result.text_en
