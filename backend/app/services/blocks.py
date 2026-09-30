"""Turn Sarvam Doc AI output into stable, addressable blocks.

Why this exists: the SDK types a digitised page as just ``page_number`` +
``content``. The API preserves layout inside that content (and may attach
bounding boxes as untyped extra fields), but nothing hands you a block list
with ids you can point at. Anything that cites a document needs exactly that - a citation is
useless in the UI unless it can say "this clause, this page, these characters".

So every digitised page is normalised here into blocks with:

  * ``block_id``   - deterministic, ``p{page}-b{index}``, stable across reruns
  * ``page_number`` - absolute, after batch stitching
  * ``char_start`` / ``char_end`` - offsets into that page's plain text
  * ``bbox``       - carried through when Sarvam supplies one, else None

The plain text a page renders from is built *from the blocks themselves*, so
offsets can never drift out of sync with what the frontend displays.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Block-level elements that become their own highlightable unit.
_BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "caption", "figcaption",
}
# Once inside these, stop splitting - a whole table is one block, not 40 cells.
_ATOMIC_TAGS = {"table", "pre"}
_SKIP_TAGS = {"script", "style", "head", "meta", "link"}

_TAG_KINDS = {
    **{f"h{i}": "heading" for i in range(1, 7)},
    "li": "list_item",
    "table": "table",
    "caption": "caption",
    "figcaption": "caption",
    "blockquote": "quote",
    "pre": "preformatted",
}

_BBOX_KEYS = ("data-bbox", "bbox", "data-bounding-box", "data-box")
_WS = re.compile(r"[ \t ]+")


@dataclass
class Block:
    block_id: str
    page_number: int
    tag: str
    text: str
    char_start: int
    char_end: int
    bbox: list[float] | None = None

    def to_dict(self) -> dict:
        return {
            "block_id": self.block_id,
            "page_number": self.page_number,
            "tag": self.tag,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "bbox": self.bbox,
        }


@dataclass
class NormalizedDocument:
    blocks: list[Block] = field(default_factory=list)
    page_texts: dict[int, str] = field(default_factory=dict)
    page_count: int = 0
    failed_pages: list[int] = field(default_factory=list)

    def to_page_blocks(self) -> list[dict]:
        """Blocks as plain dicts, ready to serialise."""
        return [b.to_dict() for b in self.blocks]

    def markdown(self) -> str:
        """A readable rendering derived from the same blocks the ids point at."""
        out: list[str] = []
        current_page = None
        for block in self.blocks:
            if block.page_number != current_page:
                current_page = block.page_number
                out.append(f"\n<!-- page {current_page} -->\n")
            if block.tag == "heading":
                out.append(f"## {block.text}")
            elif block.tag == "list_item":
                out.append(f"- {block.text}")
            elif block.tag in {"table", "preformatted"}:
                out.append(f"```\n{block.text}\n```")
            elif block.tag == "quote":
                out.append(f"> {block.text}")
            else:
                out.append(block.text)
        return "\n\n".join(part for part in out if part.strip())

    def full_text(self) -> str:
        return "\n\n".join(self.page_texts[p] for p in sorted(self.page_texts))

    def by_id(self) -> dict[str, Block]:
        return {b.block_id: b for b in self.blocks}


class _BlockParser(HTMLParser):
    """Extracts leaf block elements, keeping bounding boxes when present."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str, list[float] | None]] = []
        self._stack: list[dict[str, Any]] = []
        self._skip_depth = 0
        self._atomic_depth = 0

    # -- helpers
    def _top(self) -> dict | None:
        return self._stack[-1] if self._stack else None

    def _emit(self, frame: dict) -> None:
        text = _clean(frame["buf"])
        if text:
            self.results.append((frame["kind"], text, frame["bbox"]))

    # -- parser hooks
    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            if self._top():
                self._top()["buf"].append("\n")
            return
        if self._atomic_depth:
            # Inside a table/pre: keep the text flowing into the atomic frame,
            # but add separators so cells do not run together.
            if tag in {"td", "th"}:
                self._top()["buf"].append(" | ")
            elif tag == "tr":
                self._top()["buf"].append("\n")
            return
        if tag in _BLOCK_TAGS:
            # A nested block ends its parent's run of text; emit what the parent
            # has so far so ordering stays faithful to the document.
            parent = self._top()
            if parent and _clean(parent["buf"]):
                self._emit(parent)
                parent["buf"] = []
            self._stack.append(
                {
                    "tag": tag,
                    "kind": _TAG_KINDS.get(tag, "paragraph"),
                    "buf": [],
                    "bbox": _parse_bbox(attrs),
                }
            )
            if tag in _ATOMIC_TAGS:
                self._atomic_depth = 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if self._atomic_depth:
            if tag in _ATOMIC_TAGS:
                self._atomic_depth = 0
                frame = self._stack.pop() if self._stack else None
                if frame:
                    self._emit(frame)
            return
        if tag in _BLOCK_TAGS and self._stack:
            # Unwind to the matching frame; malformed markup is common in OCR
            # output, so tolerate unclosed tags rather than dropping content.
            while self._stack:
                frame = self._stack.pop()
                self._emit(frame)
                if frame["tag"] == tag:
                    break

    def handle_data(self, data):
        if self._skip_depth or not data:
            return
        frame = self._top()
        if frame is None:
            # Text outside any block still belongs to the page.
            self._stack.append({"tag": "p", "kind": "paragraph", "buf": [], "bbox": None})
            frame = self._top()
        frame["buf"].append(data)

    def close(self):
        super().close()
        while self._stack:
            self._emit(self._stack.pop())


def _parse_bbox(attrs: list[tuple[str, str | None]]) -> list[float] | None:
    """Read a bounding box from whichever attribute Sarvam used, if any."""
    lookup = {k.lower(): (v or "") for k, v in attrs}
    for key in _BBOX_KEYS:
        raw = lookup.get(key)
        if raw:
            nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
            if len(nums) >= 4:
                return [float(n) for n in nums[:4]]
    corners = [lookup.get(k) for k in ("data-x0", "data-y0", "data-x1", "data-y1")]
    if all(corners):
        try:
            return [float(c) for c in corners]
        except ValueError:
            return None
    return None


def _clean(parts: Iterable[str]) -> str:
    text = unescape("".join(parts))
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _blocks_from_html(html: str) -> list[tuple[str, str, list[float] | None]]:
    parser = _BlockParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # never let malformed markup lose a page
        logger.warning("HTML block parsing failed, falling back to text: %s", exc)
        return [("paragraph", _clean([re.sub(r"<[^>]+>", " ", html)]), None)]
    return parser.results


def _blocks_from_markdown(md: str) -> list[tuple[str, str, list[float] | None]]:
    """Fallback when a page came back as markdown rather than HTML."""
    out = []
    for chunk in re.split(r"\n\s*\n", md):
        text = _clean([chunk])
        if not text:
            continue
        if text.startswith("#"):
            kind = "heading"
            text = text.lstrip("#").strip()
        elif re.match(r"^\s*[-*+]\s+", text):
            kind = "list_item"
            text = re.sub(r"^\s*[-*+]\s+", "", text)
        elif text.lstrip().startswith("|"):
            kind = "table"
        else:
            kind = "paragraph"
        out.append((kind, text, None))
    return out


# Doc AI returns layout-level blocks: a whole paragraph, or on simple pages the
# entire page, as one block. That is too coarse for clause analysis - a single
# block holding ten distinct terms yields at most one finding. Blocks are split
# on their own line breaks (Doc AI preserves them), which lands almost exactly
# on clause boundaries in loan documents.
_SPLIT_ABOVE_CHARS = 160


def _extract_bbox(item: dict) -> list[float] | None:
    """Read a bounding box from any of the shapes Doc AI uses."""
    coords = item.get("coordinates")
    if isinstance(coords, dict):
        corners = [coords.get("x1"), coords.get("y1"), coords.get("x2"), coords.get("y2")]
        if all(c is not None for c in corners):
            try:
                return [float(c) for c in corners]
            except (TypeError, ValueError):
                pass

    for key in ("bbox_norm", "bbox", "bounding_box"):
        value = item.get(key)
        if isinstance(value, dict):
            value = [value.get("x0"), value.get("y0"), value.get("x1"), value.get("y1")]
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            try:
                return [float(v) for v in value[:4]]
            except (TypeError, ValueError):
                continue
    return None


def _blocks_from_supplied(raw_blocks: list[dict]) -> list[tuple[str, str, list[float] | None]]:
    """Prefer Sarvam's own block list, splitting coarse blocks into clauses."""
    out = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        raw_text = str(item.get("text") or item.get("content") or "")
        if not raw_text.strip():
            continue

        kind = str(
            item.get("layout_tag") or item.get("tag") or item.get("type") or "paragraph"
        ).lower()
        kind = _TAG_KINDS.get(kind, kind if kind in _KNOWN_KINDS else "paragraph")
        bbox = _extract_bbox(item)

        for piece in _split_clauses(raw_text):
            out.append((kind, piece, bbox))
    return out


_KNOWN_KINDS = {"heading", "table", "list_item", "paragraph", "caption", "quote", "preformatted"}


def _split_clauses(raw_text: str) -> list[str]:
    """One clause per unit, so each can carry its own finding and highlight."""
    lines = [_clean([line]) for line in raw_text.split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return []

    pieces: list[str] = []
    for line in lines:
        if len(line) <= _SPLIT_ABOVE_CHARS:
            pieces.append(line)
            continue
        # A long unbroken line is split on sentence boundaries instead.
        buf = ""
        for sentence in re.split(r"(?<=[.;])\s+", line):
            if len(buf) + len(sentence) + 1 > _SPLIT_ABOVE_CHARS and buf:
                pieces.append(buf.strip())
                buf = sentence
            else:
                buf = f"{buf} {sentence}".strip()
        if buf.strip():
            pieces.append(buf.strip())
    return pieces


def normalize_pages(
    pages: Iterable[dict],
    *,
    page_offset: int = 0,
    output_format: str = "html",
) -> NormalizedDocument:
    """Normalise one job's pages into blocks.

    ``page_offset`` shifts page numbers when a document was split into batches
    to respect the Doc AI per-job page cap - batch 2 page 1 is document page 11.
    """
    doc = NormalizedDocument()

    for page in pages:
        if not isinstance(page, dict):
            continue
        # Doc AI names this `page_num`; `page_number` is kept for the markdown
        # path and older payloads.
        raw_page_no = page.get("page_num", page.get("page_number"))
        try:
            page_no = int(raw_page_no) + page_offset
        except (TypeError, ValueError):
            page_no = len(doc.page_texts) + 1 + page_offset

        content = page.get("content") or ""
        supplied = page.get("blocks") if isinstance(page.get("blocks"), list) else None

        if supplied:
            raw_blocks = _blocks_from_supplied(supplied)
        elif output_format == "md" or (content and "<" not in content[:200]):
            raw_blocks = _blocks_from_markdown(content)
        else:
            raw_blocks = _blocks_from_html(content)

        if not raw_blocks:
            doc.failed_pages.append(page_no)
            doc.page_texts[page_no] = ""
            continue

        cursor = 0
        page_parts: list[str] = []
        for index, (kind, text, bbox) in enumerate(raw_blocks):
            start = cursor
            end = start + len(text)
            doc.blocks.append(
                Block(
                    block_id=f"p{page_no}-b{index:03d}",
                    page_number=page_no,
                    tag=kind,
                    text=text,
                    char_start=start,
                    char_end=end,
                    bbox=bbox,
                )
            )
            page_parts.append(text)
            cursor = end + 2  # the "\n\n" joiner below

        doc.page_texts[page_no] = "\n\n".join(page_parts)

    doc.page_count = len(doc.page_texts)
    return doc


def merge(documents: list[NormalizedDocument]) -> NormalizedDocument:
    """Stitch per-batch normalisations back into one document, in page order."""
    merged = NormalizedDocument()
    for doc in documents:
        merged.blocks.extend(doc.blocks)
        merged.page_texts.update(doc.page_texts)
        merged.failed_pages.extend(doc.failed_pages)
    merged.blocks.sort(key=lambda b: (b.page_number, b.char_start))
    merged.page_count = len(merged.page_texts)
    return merged


def find_span(block: Block, needle: str) -> tuple[int, int]:
    """Locate a verbatim clause inside a block, in page-text coordinates.

    Models paraphrase and re-punctuate, so an exact hit is tried first and a
    whitespace-insensitive match second. Falling back to the whole block is
    correct behaviour: highlighting a slightly wider region beats highlighting
    nothing.
    """
    if not needle:
        return block.char_start, block.char_end
    hay = block.text
    idx = hay.find(needle)
    if idx == -1:
        # re.escape stopped escaping spaces in 3.7, so collapse both escaped
        # and bare whitespace runs into a tolerant \s+ matcher.
        loose = re.sub(r"(?:\\\s|\s)+", r"\\s+", re.escape(needle.strip()))
        match = re.search(loose, hay, re.IGNORECASE)
        if match:
            return block.char_start + match.start(), block.char_start + match.end()
    if idx == -1:
        return block.char_start, block.char_end
    return block.char_start + idx, block.char_start + idx + len(needle)
