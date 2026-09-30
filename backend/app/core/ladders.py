"""Ladders: rule sets and their plain-language messages, loaded from YAML.

The engine never reads a file. This module does, checks every rule strictly
at load time, and hands the engine plain ``Rule`` objects. A ladder that
declares the wrong facts, names an unknown placeholder or omits
``verified_by`` refuses to load rather than misleading someone later.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app import config
from app.core import ladder_engine as le
from app.core.routing import StepWindow

UNVERIFIED = "UNVERIFIED"

_LADDER_KEYS = {"id", "title", "product", "verified_by", "source", "rules"}
_ENGINE_KEYS = {"id", "kind", "value", "limit", "since", "until", "effect", "when", "calculation"}
_TEXT_KEYS = {"facts", "message", "message_pending", "letter", "verified_by", "source"}
_REQUIRED_RULE_KEYS = {"id", "kind", "facts", "message", "verified_by"}
_FORMAT_SPECS = {"", "inr", "date"}


class LadderError(ValueError):
    """A ladder file is malformed. Raised at load time, never at render time."""


@dataclass(frozen=True)
class Entry:
    """The words that go with one rule."""

    message: str
    message_pending: str | None
    verified_by: str
    source: str | None = None
    letter: str | None = None  # first-person line for a draft to the respondent


@dataclass(frozen=True)
class Ladder:
    id: str
    verified_by: str
    rules: tuple[le.Rule, ...]
    entries: dict[str, Entry]
    rule_facts: dict[str, tuple[str, ...]]  # RULE_FACTS for this ladder
    title: str | None = None
    product: str | None = None

    def rule(self, rule_id: str) -> le.Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    def entry(self, rule_id: str) -> Entry:
        return self.entries[rule_id]


@lru_cache(maxsize=None)
def load(name: str) -> Ladder:
    """Load ``backend/data/ladders/<name>.yaml``."""
    return load_path(config.LADDERS_DIR / f"{name}.yaml")


def load_path(path: Path) -> Ladder:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LadderError(f"{path}: not valid YAML ({exc})") from exc

    if not isinstance(raw, dict):
        raise LadderError(f"{path}: expected a mapping at the top level")
    _no_unknown_keys(raw, _LADDER_KEYS, str(path))
    ladder_id = raw.get("id")
    if not isinstance(ladder_id, str) or not ladder_id:
        raise LadderError(f"{path}: ladder needs an id")
    verified_by = _verified_by(raw, ladder_id)
    items = raw.get("rules")
    if not isinstance(items, list) or not items:
        raise LadderError(f"{ladder_id}: ladder has no rules")

    rules: list[le.Rule] = []
    entries: dict[str, Entry] = {}
    for item in items:
        rule, entry = _load_rule(item, ladder_id)
        rules.append(rule)
        entries[rule.id] = entry

    try:
        registry = le.rule_facts(rules)
    except ValueError as exc:
        raise LadderError(f"{ladder_id}: {exc}") from exc

    return Ladder(
        id=ladder_id,
        verified_by=verified_by,
        rules=tuple(rules),
        entries=entries,
        rule_facts=registry,
        title=raw.get("title"),
        product=raw.get("product"),
    )


def _load_rule(item: Any, ladder_id: str) -> tuple[le.Rule, Entry]:
    if not isinstance(item, dict):
        raise LadderError(f"{ladder_id}: every rule must be a mapping")
    where = f"{ladder_id}/{item.get('id', '?')}"
    _no_unknown_keys(item, _ENGINE_KEYS | _TEXT_KEYS, where)
    missing = _REQUIRED_RULE_KEYS - set(item)
    if missing:
        raise LadderError(f"{where}: missing {sorted(missing)}")

    fields = {key: item[key] for key in _ENGINE_KEYS if key in item}
    if "when" in fields:
        if not isinstance(fields["when"], dict):
            raise LadderError(f"{where}: when must map facts to required values")
        fields["when"] = tuple(fields["when"].items())
    try:
        rule = le.Rule(**fields)
    except (TypeError, ValueError) as exc:
        raise LadderError(f"{where}: {exc}") from exc

    declared = item["facts"]
    if not isinstance(declared, list) or set(declared) != set(rule.facts) or len(declared) != len(set(declared)):
        raise LadderError(f"{where}: declares facts {declared}, but the rule reads {list(rule.facts)}")

    message, pending = item["message"], item.get("message_pending")
    if rule.calculation and not pending:
        raise LadderError(f"{where}: a rule with a calculation needs message_pending")
    outputs = set(rule.outputs) - {"pending"}
    _check_template(message, outputs, where)
    if pending is not None:
        _check_template(pending, set(le.KIND_VALUES[rule.kind]), f"{where} (pending)")
    letter = item.get("letter")
    if letter is not None:
        _check_template(letter, set(le.KIND_VALUES[rule.kind]), f"{where} (letter)")

    entry = Entry(
        message=message,
        message_pending=pending,
        verified_by=_verified_by(item, where),
        source=item.get("source"),
        letter=letter,
    )
    return rule, entry


def _verified_by(raw: dict, where: str) -> str:
    value = raw.get("verified_by")
    if not isinstance(value, str) or not value.strip():
        raise LadderError(f"{where}: verified_by is required (use {UNVERIFIED} until checked)")
    return value.strip()


def _no_unknown_keys(raw: dict, allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise LadderError(f"{where}: unknown keys {sorted(unknown)}")


def _check_template(template: Any, allowed: set[str], where: str) -> None:
    if not isinstance(template, str) or not template.strip():
        raise LadderError(f"{where}: message must be non-empty text")
    for _, name, spec, _ in string.Formatter().parse(template):
        if name is None:
            continue
        if name not in allowed:
            raise LadderError(f"{where}: message uses {{{name}}}, which this rule never computes")
        if spec not in _FORMAT_SPECS:
            raise LadderError(f"{where}: unknown format :{spec}")


@lru_cache(maxsize=None)
def load_steps(path: Path | None = None) -> dict[str, StepWindow]:
    """Escalation steps and their response windows (data/ladders/escalation_steps.yaml)."""
    path = Path(path or config.LADDERS_DIR / "escalation_steps.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), dict):
        raise LadderError(f"{path}: expected a steps mapping")
    _verified_by(raw, str(path))
    steps = {}
    for step, item in raw["steps"].items():
        where = f"escalation_steps/{step}"
        if not isinstance(item, dict):
            raise LadderError(f"{where}: expected a mapping")
        _no_unknown_keys(item, {"label", "respond_within_days", "verified_by", "source"}, where)
        days = item.get("respond_within_days")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days <= 0):
            raise LadderError(f"{where}: respond_within_days must be a positive whole number or null")
        if not isinstance(item.get("label"), str) or not item["label"].strip():
            raise LadderError(f"{where}: label is required")
        steps[step] = StepWindow(step, item["label"].strip(), days, _verified_by(item, where), item.get("source"))
    return steps


# --- Rendering ---------------------------------------------------------------


def inr(amount: int | float) -> str:
    """Rupees with Indian digit grouping: 120000 -> ₹1,20,000."""
    whole = int(amount)
    digits = str(abs(whole))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{'-' if whole < 0 else ''}₹{digits}"


class _Formatter(string.Formatter):
    def format_field(self, value, format_spec):
        if format_spec == "inr":
            return inr(value)
        if format_spec == "date":
            return f"{value.day} {value:%B %Y}"
        return super().format_field(value, format_spec)


_FORMATTER = _Formatter()


def render(hit: le.Hit, ladder: Ladder) -> str:
    entry = ladder.entry(hit.rule_id)
    template = entry.message_pending if hit.values.get("pending") else entry.message
    return " ".join(_FORMATTER.format(template, **hit.values).split())


def explain(verdict: le.Verdict, ladder: Ladder) -> list[dict[str, Any]]:
    """Plain-language messages for every rule that fired: blocks, then deductions, then grounds."""
    out = []
    for hit in (*verdict.blocks, *verdict.deductions, *verdict.grounds):
        entry = ladder.entry(hit.rule_id)
        out.append(
            {
                "rule_id": hit.rule_id,
                "effect": hit.effect,
                "text": render(hit, ladder),
                "verified_by": entry.verified_by,
                "unverified": entry.verified_by == UNVERIFIED,
            }
        )
    return out


def letter_lines(verdict: le.Verdict, ladder: Ladder) -> tuple[list[str], bool]:
    """Draft lines for the grounds and blocks a verdict found, and whether any rests on an UNVERIFIED rule."""
    lines, unverified = [], False
    for hit in (*verdict.grounds, *verdict.blocks):
        entry = ladder.entry(hit.rule_id)
        if not entry.letter:
            continue
        lines.append(" ".join(_FORMATTER.format(entry.letter, **hit.values).split()))
        unverified = unverified or entry.verified_by == UNVERIFIED
    return lines, unverified
