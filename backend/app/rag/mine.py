"""Her own documents, sent in the web chat: held in memory so she can ask questions about them.

Nothing here is written to disk. A case's documents go when she says "delete
everything" (``forget``) or when the server restarts. Answers from them cite the
file and page, e.g. ``[Your document, bike policy.pdf, p.2]``, and carry no
"not yet verified" badge: the source is her own paper, not a legal value of ours.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass

import chromadb
from chromadb.config import Settings

from app.rag import index
from app.rag.ingest import chunk_page

YOUR_DOCUMENT = "Your document"  # stands where the insurer goes in a citation
PRODUCT = "her_document"
VERIFIED_BY = "Her own document"
MAX_DOCUMENTS = 5  # per case; the oldest is dropped first
# Smaller than the corpus chunks: a policy schedule packs many facts per page, and a short
# prompt keeps the answer quick.
CHUNK_CHARS, CHUNK_OVERLAP = 1000, 150
TOP_K = 10  # short chunks, so more of them fit: a clause worded differently still gets in


@dataclass(frozen=True)
class Upload:
    name: str
    pages: tuple[tuple[int, str], ...]


_DOCS: dict[str, list[Upload]] = {}
_LOCK = threading.Lock()
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
    return _CLIENT


def _label(filename: str) -> str:
    """A file name safe inside a citation: no brackets, commas or line breaks."""
    cleaned = " ".join(re.sub(r"[\[\],]", " ", filename or "").split())
    return cleaned[:60] or "document"


def add(case_id: str, filename: str, pages: list[tuple[int, str]]) -> str:
    """Hold a document's pages for this case. Returns the name its citations use."""
    name = _label(filename)
    upload = Upload(name, tuple((int(page), text) for page, text in pages if text and text.strip()))
    with _LOCK:
        docs = [d for d in _DOCS.get(case_id, []) if d.name != name] + [upload]
        _DOCS[case_id] = docs[-MAX_DOCUMENTS:]
    return name


def has(case_id: str | None) -> bool:
    with _LOCK:
        return bool(case_id and _DOCS.get(case_id))


def forget(case_id: str | None) -> None:
    with _LOCK:
        _DOCS.pop(case_id, None)


def collection(case_id: str, only: str | None = None, first_chunks: int | None = None):
    """A throwaway in-memory collection of her documents (or one of them), ready for ``answer``.

    ``first_chunks`` keeps only the opening chunks of each document: what a summary needs.
    The caller drops it with ``drop`` when done.
    """
    with _LOCK:
        docs = [d for d in _DOCS.get(case_id, []) if only is None or d.name == only]
    found = _client().create_collection(
        f"mine-{uuid.uuid4().hex}", embedding_function=index.embedding_function(), metadata={"hnsw:space": "cosine"}
    )
    ids, texts, metas = [], [], []
    for doc in docs:
        chunks = [
            (page, n, chunk)
            for page, text in doc.pages
            for n, chunk in enumerate(chunk_page(text, max_chars=CHUNK_CHARS, overlap=CHUNK_OVERLAP))
        ]
        for page, n, chunk in chunks[:first_chunks] if first_chunks else chunks:
            ids.append(f"{doc.name}#p{page}#c{n}")
            texts.append(chunk)
            metas.append({
                "layer": "insurer", "insurer": YOUR_DOCUMENT, "product": PRODUCT, "doc_type": doc.name,
                "page": page, "source_url": "", "verified_by": VERIFIED_BY, "effective_date": "",
            })
    if ids:
        found.add(ids=ids, documents=texts, metadatas=metas)
    return found


def drop(found) -> None:
    _client().delete_collection(found.name)
