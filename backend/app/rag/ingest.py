"""Ingest the corpus: sources.yaml -> pages -> clause chunks -> Chroma.

    python -m app.rag.ingest        (from backend/)

Every file must be listed in backend/data/corpus/sources.yaml with its layer,
insurer, product, doc_type, source_url, effective_date and verified_by. Each
file is hashed together with its sources.yaml entry: a re-run re-ingests only
files whose bytes or metadata changed (deleting their old chunks first), and
removes chunks of files no longer listed. Chunk ids are stable:
``<file>#p<page>#c<n>``.

PDFs are read page by page with PyMuPDF. Text and Markdown files are one page
unless they mark pages with ``<!-- page N -->`` lines. A scanned PDF with no
text layer yields no chunks and a warning; it needs OCR first.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf
import yaml

from app import config
from app.core.agent import PRODUCTS
from app.rag import index

logger = logging.getLogger(__name__)

LAYERS = ("regulation", "insurer", "paytm")  # the corpus folders
_SOURCE_KEYS = {"file", "layer", "insurer", "product", "doc_type", "source_url", "effective_date", "verified_by"}
_BATCH = 256


class SourceError(ValueError):
    """sources.yaml is wrong about a file."""


@dataclass(frozen=True)
class Source:
    file: str  # relative to the corpus folder, with forward slashes
    layer: str
    doc_type: str
    verified_by: str
    insurer: str = ""
    product: str = ""
    source_url: str = ""
    effective_date: str = ""


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    chunks: int = 0


def load_sources(corpus_dir: Path) -> list[Source]:
    corpus_dir = Path(corpus_dir)
    raw = yaml.safe_load((corpus_dir / "sources.yaml").read_text(encoding="utf-8")) or {}
    sources, seen = [], set()
    for item in raw.get("sources") or []:
        if not isinstance(item, dict):
            raise SourceError("every entry in sources.yaml must be a mapping")
        where = item.get("file", "?")
        unknown = set(item) - _SOURCE_KEYS
        if unknown:
            raise SourceError(f"{where}: unknown keys {sorted(unknown)}")
        values = {key: _text(item.get(key)) for key in _SOURCE_KEYS}
        file = values["file"].replace("\\", "/")
        parts = file.split("/")
        if not file or file in seen:
            raise SourceError(f"{where}: file missing or listed twice")
        if not (corpus_dir / file).is_file():
            raise SourceError(f"{file}: no such file in the corpus")
        if values["layer"] not in LAYERS or parts[0] != values["layer"]:
            raise SourceError(f"{file}: layer must be one of {LAYERS} and match the file's top folder")
        if values["layer"] == "insurer":
            if not values["insurer"] or values["product"] not in PRODUCTS:
                raise SourceError(f"{file}: an insurer document needs an insurer and a product from {PRODUCTS}")
            if len(parts) < 4 or parts[2] != values["product"]:
                raise SourceError(f"{file}: insurer documents live in insurer/<insurer>/<product>/")
        if not values["doc_type"]:
            raise SourceError(f"{file}: doc_type is required")
        if not values["verified_by"]:
            raise SourceError(f"{file}: verified_by is required (UNVERIFIED until checked)")
        seen.add(file)
        sources.append(Source(**{**values, "file": file}))
    return sources


def _text(value) -> str:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return "" if value is None else str(value).strip()


# --- Pages and chunks ----------------------------------------------------------

_PAGE_MARK = re.compile(r"^\s*<!--\s*page\s+(\d+)\s*-->\s*$", re.IGNORECASE | re.MULTILINE)
# A clause starts a line: a Markdown heading, "Section 4", "Clause 5", "Part II",
# or a clause number such as "3.", "3.2" or "4.1.3)" followed by a capitalised word.
_HEADING = re.compile(
    r"^(?=[ \t]*(?:#{1,6}[ \t]+\S"
    r"|(?i:section|clause|article|chapter|part|schedule)[ \t]+[\dIVXLC]+\b"
    r"|\d{1,2}(?:\.\d{1,2}){0,3}[.)]?[ \t]+[A-Z]))",
    re.MULTILINE,
)


def read_pages(path: Path) -> list[tuple[int, str]]:
    """[(page number, text)] for a PDF, text or Markdown file."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as doc:
            return [(number, page.get_text("text")) for number, page in enumerate(doc, start=1)]
    text = path.read_text(encoding="utf-8")
    marks = list(_PAGE_MARK.finditer(text))
    if not marks:
        return [(1, text)]
    pages = [(1, text[: marks[0].start()])] if text[: marks[0].start()].strip() else []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        pages.append((int(mark.group(1)), text[mark.end():end]))
    return pages


def _windows(text: str, max_chars: int, overlap: int) -> list[str]:
    step = max(1, max_chars - overlap)
    return [text[i: i + max_chars] for i in range(0, max(len(text) - overlap, 1), step) if text[i: i + max_chars].strip()]


def chunk_page(
    text: str,
    max_chars: int | None = None,
    overlap: int | None = None,
    min_chars: int = 40,
) -> list[str]:
    """Split one page on clause headings; windows of ``max_chars`` with ``overlap`` when a
    page has no headings or a clause is too long. A fragment under ``min_chars`` (a bare
    heading such as "PART A") joins the clause after it."""
    max_chars = max_chars or config.RAG_CHUNK_CHARS
    overlap = config.RAG_CHUNK_OVERLAP if overlap is None else overlap
    text = text.strip()
    if not text:
        return []

    starts = [m.start() for m in _HEADING.finditer(text)]
    if not starts or starts[0] != 0:
        starts = [0] + starts
    pieces = [text[a:b].strip() for a, b in zip(starts, starts[1:] + [len(text)])]

    merged: list[str] = []
    carry = ""
    for piece in pieces:
        piece = f"{carry}\n{piece}".strip() if carry else piece
        if len(piece) < min_chars:
            carry = piece
            continue
        carry = ""
        merged.append(piece)
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]}\n{carry}"
        else:
            merged.append(carry)

    chunks: list[str] = []
    for piece in merged:
        chunks.extend([piece] if len(piece) <= max_chars else _windows(piece, max_chars, overlap))
    return chunks


# --- Ingest ----------------------------------------------------------------------


def _digest(path: Path, source: Source) -> str:
    h = hashlib.sha256(path.read_bytes())
    h.update(json.dumps(asdict(source), sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def ingest(corpus_dir: Path | None = None, collection=None) -> IngestReport:
    corpus_dir = Path(corpus_dir or config.CORPUS_DIR)
    collection = collection if collection is not None else index.default_collection()
    sources = load_sources(corpus_dir)
    report = IngestReport()

    indexed: dict[str, str] = {}
    existing = collection.get(include=["metadatas"])
    for meta in existing["metadatas"] or []:
        indexed.setdefault(meta["file"], meta.get("file_hash", ""))

    for source in sources:
        path = corpus_dir / source.file
        digest = _digest(path, source)
        if indexed.get(source.file) == digest:
            report.unchanged.append(source.file)
            continue
        if source.file in indexed:
            collection.delete(where={"file": source.file})
            report.updated.append(source.file)
        else:
            report.added.append(source.file)

        ids, documents, metadatas = [], [], []
        for page, text in read_pages(path):
            for n, chunk in enumerate(chunk_page(text)):
                ids.append(f"{source.file}#p{page}#c{n}")
                documents.append(chunk)
                metadatas.append({**asdict(source), "file_hash": digest, "page": page, "chunk": n})
        if not ids:
            logger.warning("%s has no text (a scanned PDF needs OCR first); nothing indexed", source.file)
        for i in range(0, len(ids), _BATCH):
            collection.upsert(
                ids=ids[i: i + _BATCH], documents=documents[i: i + _BATCH], metadatas=metadatas[i: i + _BATCH]
            )
        report.chunks += len(ids)

    listed = {source.file for source in sources}
    for file in sorted(set(indexed) - listed):
        collection.delete(where={"file": file})
        report.removed.append(file)
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        report = ingest()
    except SourceError as exc:
        print(f"sources.yaml: {exc}")
        return 1
    except Exception as exc:  # most often: the embedding model could not be downloaded
        print(f"Ingest failed: {exc!r}")
        print("The default embeddings download a model on first use. Retry, or set RAG_EMBEDDINGS=hashing to build an offline index.")
        return 1
    for label in ("added", "updated", "unchanged", "removed"):
        for file in getattr(report, label):
            print(f"{label:<10} {file}")
    print(f"\n{report.chunks} chunks written to {config.RAG_INDEX_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
