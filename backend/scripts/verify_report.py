"""List every legal or regulatory value still marked UNVERIFIED (PRD-PAYTM C5).

Scans every YAML under backend/data and prints, per value, the file and line of
its ``verified_by``, what it says, and the source to check it against. Verify
by hand, then replace UNVERIFIED with the checker's name and date.

Rules and escalation steps must each carry their own ``verified_by``; one
without it is reported as MISSING.

    python scripts/verify_report.py              # everything under data/
    python scripts/verify_report.py FILE ...     # just these files
    python scripts/verify_report.py --strict     # exit 1 while anything is UNVERIFIED

Exit code: 1 if anything is MISSING (always) or UNVERIFIED (with --strict).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"
UNVERIFIED = "UNVERIFIED"

# Collections whose every item must carry its own verified_by.
ITEM_COLLECTIONS = {"rules", "steps"}
# Keys that are not the value being verified.
_NOT_VALUES = {"id", "verified_by", "source", "message", "message_pending", "facts"}


@dataclass(frozen=True)
class Entry:
    status: str  # UNVERIFIED | MISSING
    file: str
    line: int  # 1-based: the verified_by line, or the item's first line when MISSING
    label: str
    summary: str
    source: str | None = None


def default_paths() -> list[Path]:
    return sorted(DATA_DIR.rglob("*.yaml"))


def _scalar(node) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _summary(node: yaml.MappingNode) -> str:
    parts = []
    for key, value in node.value:
        if key.value in _NOT_VALUES:
            continue
        if isinstance(value, yaml.ScalarNode):
            parts.append(f"{key.value}={value.value}")
        elif isinstance(value, yaml.SequenceNode):
            parts.append(f"{key.value}=[{len(value.value)} items]")
        elif isinstance(value, yaml.MappingNode):
            if all(isinstance(v, yaml.ScalarNode) for _, v in value.value):
                inner = ",".join(f"{k.value}={v.value}" for k, v in value.value)
                parts.append(f"{key.value}={{{inner}}}")
            else:
                parts.append(f"{key.value}=[{len(value.value)} items]")
    return " ".join(parts)


def _walk(node, file: str, label: str, is_item: bool, out: list[Entry]) -> None:
    if isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _walk(child, file, label, is_item, out)
        return
    if not isinstance(node, yaml.MappingNode):
        return

    pairs = {key.value: value for key, value in node.value}
    own = _scalar(pairs.get("id")) or _scalar(pairs.get("file")) or label
    verified = pairs.get("verified_by")
    if verified is not None:
        if _scalar(verified) == UNVERIFIED:
            source = _scalar(pairs.get("source"))
            out.append(Entry(UNVERIFIED, file, verified.start_mark.line + 1, own, _summary(node),
                             " ".join(source.split()) if source else None))
    elif is_item:
        out.append(Entry("MISSING", file, node.start_mark.line + 1, own, _summary(node)))

    for key, value in node.value:
        if key.value in ITEM_COLLECTIONS and isinstance(value, yaml.MappingNode):
            for item_key, item in value.value:
                _walk(item, file, item_key.value, True, out)
        elif key.value in ITEM_COLLECTIONS:
            _walk(value, file, own, True, out)
        else:
            _walk(value, file, own, False, out)


def scan(paths) -> list[Entry]:
    entries: list[Entry] = []
    for path in paths:
        root = yaml.compose(Path(path).read_text(encoding="utf-8"))
        _walk(root, str(path), Path(path).stem, False, entries)
    return entries


def _display(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(BACKEND_DIR)).replace("\\", "/")
    except ValueError:
        return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="YAML files (default: everything under backend/data)")
    parser.add_argument("--strict", action="store_true", help="exit 1 while anything is UNVERIFIED")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    entries = scan([Path(p) for p in args.paths] or default_paths())
    for entry in entries:
        print(f"{entry.status:<10}  {_display(entry.file)}:{entry.line}  {entry.label}")
        if entry.summary:
            print(f"{'':<12}{entry.summary}")
        if entry.source:
            print(f"{'':<12}source: {entry.source}")
    unverified = sum(e.status == UNVERIFIED for e in entries)
    missing = sum(e.status == "MISSING" for e in entries)
    print(f"\n{unverified} unverified, {missing} missing verified_by")

    if missing or (args.strict and unverified):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
