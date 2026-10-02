"""Retrieve passages for a question: her insurer and product, or regulation. Top 6."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache

from app import config
from app.rag import index

logger = logging.getLogger(__name__)

_COMPANY_WORDS = frozenset({"the", "company", "co", "ltd", "limited", "pvt", "private", "inc"})


def _name_key(name: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _COMPANY_WORDS)


@lru_cache(maxsize=1)
def _corpus_insurers() -> dict[str, str]:
    from app.rag.ingest import load_sources

    try:
        return {_name_key(s.insurer): s.insurer for s in load_sources(config.CORPUS_DIR) if s.insurer}
    except Exception as exc:  # a broken sources.yaml must not break the chat
        logger.warning("Could not read corpus insurers: %s", exc)
        return {}


def resolve_insurer(name: str | None) -> str | None:
    """Her insurer's name as the corpus spells it ("… Company Ltd" -> "…"), or None if not in the corpus."""
    return _corpus_insurers().get(_name_key(name)) if name else None


def _same_text(a: str, b: str) -> bool:
    """Near-identical passages (a clause repeated on page after page, differing only in a
    section number): they would crowd out the one clause that answers her."""
    wa, wb = set(re.findall(r"[a-z]+", a.lower())), set(re.findall(r"[a-z]+", b.lower()))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.9


@dataclass(frozen=True)
class Passage:
    id: str
    text: str
    layer: str
    insurer: str
    product: str
    doc_type: str
    page: int
    source_url: str
    verified_by: str
    effective_date: str
    distance: float


def _where(insurer: str | None, product: str | None) -> dict:
    """Regulation and news for everyone; her insurer's documents only when both are known."""
    shared = [{"layer": "regulation"}, {"layer": "news"}]
    if insurer and product:
        return {"$or": [{"$and": [{"insurer": insurer}, {"product": product}]}, *shared]}
    return {"$or": shared}


def retrieve(
    question: str,
    *,
    insurer: str | None = None,
    product: str | None = None,
    k: int | None = None,
    collection=None,
) -> list[Passage]:
    """Nearest chunks from her own insurer's documents for her product, plus regulation.

    ``insurer`` must be written exactly as in sources.yaml. Without both an
    insurer and a product, only regulation is searched: another insurer's
    wording is never used to answer her.
    """
    collection = collection if collection is not None else index.default_collection()
    if not question.strip() or collection.count() == 0:
        return []
    k = k or config.RAG_TOP_K
    result = collection.query(
        query_texts=[question],
        n_results=min(k * 3, collection.count()),  # room to skip near-duplicates
        where=_where(insurer, product),
        include=["documents", "metadatas", "distances"],
    )
    passages = []
    for id_, text, meta, distance in zip(
        result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        passages.append(
            Passage(
                id=id_,
                text=text,
                layer=meta["layer"],
                insurer=meta.get("insurer", ""),
                product=meta.get("product", ""),
                doc_type=meta["doc_type"],
                page=int(meta["page"]),
                source_url=meta.get("source_url", ""),
                verified_by=meta["verified_by"],
                effective_date=meta.get("effective_date", ""),
                distance=float(distance),
            )
        )
    distinct: list[Passage] = []
    for passage in passages:  # nearest first; a near-copy of one already kept adds nothing
        if not any(_same_text(passage.text, kept.text) for kept in distinct):
            distinct.append(passage)
    # News is context, not the policy or the rule: it never crowds the documents out of the sources.
    kept, news = [], 0
    for passage in distinct:
        if passage.layer == "news":
            if news >= config.NEWS_MAX_PASSAGES:
                continue
            news += 1
        kept.append(passage)
    return kept[:k]
