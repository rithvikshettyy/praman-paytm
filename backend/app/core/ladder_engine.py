"""The rule engine: a fact sheet in, a verdict out.

Purity contract (tested in tests/test_ladder_engine.py): no network, no LLM,
no randomness, no I/O, no clock. Everything the engine needs arrives in
``Facts``; anything the engine does not know is ``None``, and ``None`` means
"nobody asked", never "no". A rule whose facts are missing lands in
``facts_pending`` instead of producing a confident wrong verdict.

Rules are data (see backend/data/ladders/). Each has a kind that says how it
reads its facts:

    kind              reads                           fires when           effect
    duration_unmet    a month count, or two dates     elapsed < required   block
    duration_met      a month count, or two dates     elapsed >= limit     ground
    threshold_breach  two numbers                     actual > allowed     block, deduction or ground
    threshold_short   two numbers                     actual < required    block
    flag_false        one boolean                     the fact is False    block or deduction
    flag_true         one boolean                     the fact is True     block or deduction

A limit is either another fact or a constant (a regulatory value from a
ladder YAML, with its own ``verified_by``). Grounds help rather than block:
they attach to the verdict and never decide the outcome on its own.

All money and date arithmetic lives here: the room-cap deduction, the date a
claim becomes possible, months between two dates. Never in a prompt.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field, fields, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

# --- Facts -------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    # respondent routing
    respondent: str | None = None  # insurer | lender | distributor | bank
    distributor_owned: bool | None = None  # True when the distributor owes the answer
    # insurance
    policy_start_on: date | None = None
    treatment_on: date | None = None  # admission or treatment date; the engine never reads a clock
    procedure: str | None = None
    wait_months: int | None = None
    ped_wait_months: int | None = None  # pre-existing disease waiting period
    months_held: int | None = None
    months_continuous_cover: int | None = None  # incl. portability/renewals, for moratorium
    continuous_cover_since: date | None = None  # start of unbroken cover; derives months_continuous_cover
    sum_insured: float | None = None
    room_cap_per_day: float | None = None
    room_cap_percent: float | None = None  # room cap stated as % of sum insured per day
    room_quoted_per_day: float | None = None
    bill_deductible_heads: float | None = None  # room, nursing, surgeon, OT, etc.
    bill_exempt_heads: float | None = None  # pharmacy, consumables, implants, devices, diagnostics
    exclusion_listed: bool | None = None  # True when the procedure is on the exclusions list
    policy_in_force: bool | None = None
    denial_reason: str | None = None  # non_disclosure | waiting_period | exclusion | documents | other
    documents_collected: int | None = None
    documents_required: int | None = None
    # lending
    sanctioned_amount: float | None = None
    processing_fee: float | None = None
    net_disbursal: float | None = None
    instalment_amount: float | None = None
    instalment_count: int | None = None
    total_repayable: float | None = None
    kfs_supplied: bool | None = None
    insurance_bundled: bool | None = None
    insurance_consented: bool | None = None
    # motor (N9, stretch)
    tp_cover_valid: bool | None = None
    zero_dep_addon: bool | None = None
    idv: float | None = None
    claim_amount: float | None = None


FACT_NAMES = frozenset(f.name for f in fields(Facts))


def derive(facts: Facts) -> Facts:
    """Fill facts that follow from other known facts. Never fills from a default,
    and never overwrites a fact that is already known."""
    derived: dict[str, Any] = {}
    if facts.months_held is None and facts.policy_start_on and facts.treatment_on:
        derived["months_held"] = whole_months_between(facts.policy_start_on, facts.treatment_on)
    if facts.months_continuous_cover is None and facts.continuous_cover_since and facts.treatment_on:
        derived["months_continuous_cover"] = whole_months_between(facts.continuous_cover_since, facts.treatment_on)
    if facts.room_cap_per_day is None and facts.room_cap_percent is not None and facts.sum_insured is not None:
        # A percent room cap is a percent of the sum insured, per day.
        derived["room_cap_per_day"] = float(_dec(facts.sum_insured) * _dec(facts.room_cap_percent) / 100)
    return replace(facts, **derived) if derived else facts


# --- Calendar and money arithmetic -------------------------------------------


def add_days(start: date, days: int) -> date:
    """A response deadline: ``days`` calendar days after ``start``."""
    return start + timedelta(days=days)


def add_months(start: date, months: int) -> date:
    """The same day ``months`` later, clamped to the month's end (31 Jan + 1 = 28 Feb)."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def whole_months_between(start: date, end: date) -> int:
    """Completed months from ``start`` to ``end``.

    A month completes on the same day-of-month, or on the last day of a
    shorter month, so this always agrees with ``add_months``.
    """
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day and end.day != calendar.monthrange(end.year, end.month)[1]:
        months -= 1
    return months


def _dec(value: float | int) -> Decimal:
    return Decimal(str(value))


def _rupees(amount: Decimal) -> int:
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))


BILL_HEADS = ("deductible", "exempt")


def bill_head_totals(lines) -> dict[str, float]:
    """Sum bill lines already placed in a head: [(head, amount), ...] -> {head: total}.

    Deductible heads (room, nursing, surgeon, OT) are what a room-cap
    deduction applies to; exempt heads (pharmacy, consumables, implants,
    devices, diagnostics) are never reduced.
    """
    totals = {head: Decimal(0) for head in BILL_HEADS}
    for head, amount in lines:
        if head not in totals:
            raise ValueError(f"unknown bill head {head!r}")
        totals[head] += _dec(amount)
    return {head: float(total) for head, total in totals.items()}


# --- Calculations a rule can attach ------------------------------------------


@dataclass(frozen=True)
class Calculation:
    """Arithmetic run when a rule fires.

    ``reads`` are facts the calculation needs beyond the rule's own. If any is
    missing the rule still fires, every output is ``None`` with
    ``pending=True``, and the missing facts are listed in ``facts_pending``
    without holding the verdict.
    """

    reads: tuple[str, ...]
    outputs: tuple[str, ...]
    run: Callable[[Facts, dict[str, Any]], dict[str, Any]]


def _proportionate_deduction(facts: Facts, values: dict[str, Any]) -> dict[str, Any]:
    """PRD-PAYTM N1 room-cap arithmetic.

    ratio     = room_cap_per_day / room_quoted_per_day
    deduction = (1 - ratio) * bill_deductible_heads
    exempt heads are never reduced; the whole bill is never cut.
    """
    ratio = _dec(facts.room_cap_per_day) / _dec(facts.room_quoted_per_day)
    deductible = _dec(facts.bill_deductible_heads)
    exempt = _dec(facts.bill_exempt_heads)
    deduction = _rupees((1 - ratio) * deductible)
    return {
        "ratio": float(ratio),
        "deduction": deduction,
        "exempt": _rupees(exempt),
        "payable_estimate": _rupees(deductible + exempt) - deduction,
    }


def _claimable_from(facts: Facts, values: dict[str, Any]) -> dict[str, Any]:
    """The date a waiting period ends: policy start plus the required months."""
    return {"claimable_from": add_months(facts.policy_start_on, int(values["limit"]))}


CALCULATIONS: dict[str, Calculation] = {
    "proportionate_deduction": Calculation(
        reads=("bill_deductible_heads", "bill_exempt_heads"),
        outputs=("ratio", "deduction", "exempt", "payable_estimate"),
        run=_proportionate_deduction,
    ),
    "claimable_from": Calculation(
        reads=("policy_start_on",),
        outputs=("claimable_from",),
        run=_claimable_from,
    ),
}

# --- Rules -------------------------------------------------------------------

DURATION_UNMET = "duration_unmet"
DURATION_MET = "duration_met"
THRESHOLD_BREACH = "threshold_breach"
THRESHOLD_SHORT = "threshold_short"
FLAG_FALSE = "flag_false"
FLAG_TRUE = "flag_true"

BLOCK = "block"
DEDUCTION = "deduction"
GROUND = "ground"

_DURATION_KINDS = {DURATION_UNMET, DURATION_MET}
_FLAG_KINDS = {FLAG_FALSE, FLAG_TRUE}

# The effects each kind may carry; the first is its default.
_EFFECTS = {
    DURATION_UNMET: (BLOCK,),
    DURATION_MET: (GROUND,),
    THRESHOLD_BREACH: (BLOCK, DEDUCTION, GROUND),
    THRESHOLD_SHORT: (BLOCK,),
    FLAG_FALSE: (BLOCK, DEDUCTION),
    FLAG_TRUE: (BLOCK, DEDUCTION),
}

# Values each kind reports when it fires, beyond any calculation's outputs.
KIND_VALUES = {
    DURATION_UNMET: ("value", "limit", "remaining"),
    DURATION_MET: ("value", "limit"),
    THRESHOLD_BREACH: ("value", "limit", "excess"),
    THRESHOLD_SHORT: ("value", "limit", "shortfall"),
    FLAG_FALSE: ("value",),
    FLAG_TRUE: ("value",),
}


@dataclass(frozen=True)
class Rule:
    """One check over the fact sheet.

    ``value`` names the fact being tested (a month count, an amount, a flag).
    Duration kinds may instead name two date facts, ``since`` and ``until``,
    and count the whole months between them. ``limit`` is a fact name or a
    numeric constant. ``when`` holds equality conditions that must all hold
    for the rule to apply at all. ``calculation`` names arithmetic from
    ``CALCULATIONS`` to run when the rule fires.
    """

    id: str
    kind: str
    value: str | None = None
    limit: str | int | float | None = None
    since: str | None = None
    until: str | None = None
    effect: str | None = None
    when: tuple[tuple[str, Any], ...] = ()
    calculation: str | None = None

    def __post_init__(self):
        if self.kind not in _EFFECTS:
            raise ValueError(f"{self.id}: unknown rule kind {self.kind!r}")

        allowed = _EFFECTS[self.kind]
        if self.effect is None:
            object.__setattr__(self, "effect", allowed[0])
        elif self.effect not in allowed:
            raise ValueError(f"{self.id}: {self.kind} cannot have effect {self.effect!r}")

        dated = self.since is not None or self.until is not None
        if self.kind in _DURATION_KINDS:
            if dated and (self.since is None or self.until is None):
                raise ValueError(f"{self.id}: date form needs both since and until")
            if dated == (self.value is not None):
                raise ValueError(f"{self.id}: give a month count or two dates, not both or neither")
        elif dated:
            raise ValueError(f"{self.id}: only duration kinds read dates")
        elif self.value is None:
            raise ValueError(f"{self.id}: {self.kind} needs a value fact")

        if self.kind in _FLAG_KINDS:
            if self.limit is not None:
                raise ValueError(f"{self.id}: {self.kind} takes no limit")
        elif self.limit is None:
            raise ValueError(f"{self.id}: {self.kind} needs a limit")
        elif isinstance(self.limit, bool) or not isinstance(self.limit, (str, int, float)):
            raise ValueError(f"{self.id}: limit must be a fact name or a number")

        if self.calculation is not None and self.calculation not in CALCULATIONS:
            raise ValueError(f"{self.id}: unknown calculation {self.calculation!r}")

        unknown = [name for name in self.facts if name not in FACT_NAMES]
        if unknown:
            raise ValueError(f"{self.id}: unknown facts {unknown}")

    @property
    def facts(self) -> tuple[str, ...]:
        """Every fact this rule needs before it can decide, conditions first."""
        names = [name for name, _ in self.when]
        names += [n for n in (self.value, self.since, self.until) if n is not None]
        if isinstance(self.limit, str):
            names.append(self.limit)
        return tuple(dict.fromkeys(names))

    @property
    def outputs(self) -> tuple[str, ...]:
        """Every value a fired hit of this rule carries."""
        extra = ("pending", *CALCULATIONS[self.calculation].outputs) if self.calculation else ()
        return KIND_VALUES[self.kind] + extra


def rule_facts(rules) -> dict[str, tuple[str, ...]]:
    """RULE_FACTS for a rule set: rule id -> the facts it needs. Ids must be unique."""
    registry: dict[str, tuple[str, ...]] = {}
    for rule in rules:
        if rule.id in registry:
            raise ValueError(f"duplicate rule id {rule.id!r}")
        registry[rule.id] = rule.facts
    return registry


# --- Verdict -----------------------------------------------------------------

FILE = "file"
DO_NOT_FILE_YET = "do_not_file_yet"
FILE_WITH_KNOWN_DEDUCTION = "file_with_known_deduction"
FACTS_PENDING = "facts_pending"
NO_VERDICT = "no_verdict"  # the rule set has nothing that gates filing

# A block is never "give up": the next step is a written question to the
# insurer, which starts a clock.
COVERAGE_QUERY = "COVERAGE_QUERY"


@dataclass(frozen=True)
class Hit:
    """A rule that fired, with the numbers it read and computed."""

    rule_id: str
    kind: str
    effect: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    outcome: str
    blocks: tuple[Hit, ...] = ()
    deductions: tuple[Hit, ...] = ()
    grounds: tuple[Hit, ...] = ()
    facts_pending: tuple[str, ...] = ()
    possible_on: date | None = None  # when every block is time-bound: the date filing becomes possible
    next_action: str | None = None
    respondent: str | None = None  # who this verdict says to write to
    distributor_owned: bool | None = None  # whether the distributor owes the answer


# --- Evaluation --------------------------------------------------------------


def _limit(rule: Rule, facts: Facts) -> int | float:
    return getattr(facts, rule.limit) if isinstance(rule.limit, str) else rule.limit


def _check(rule: Rule, facts: Facts) -> tuple[bool, dict[str, Any]]:
    """Run a rule whose facts are all known. Returns (fired, values read)."""
    if rule.kind in _FLAG_KINDS:
        value = getattr(facts, rule.value)
        return value is (rule.kind == FLAG_TRUE), {"value": value}

    if rule.kind in _DURATION_KINDS and rule.value is None:
        value = whole_months_between(getattr(facts, rule.since), getattr(facts, rule.until))
    else:
        value = getattr(facts, rule.value)
    limit = _limit(rule, facts)
    values: dict[str, Any] = {"value": value, "limit": limit}

    if rule.kind == DURATION_UNMET:
        fired = value < limit
        values["remaining"] = limit - value
    elif rule.kind == DURATION_MET:
        fired = value >= limit
    elif rule.kind == THRESHOLD_BREACH:
        fired = value > limit
        values["excess"] = value - limit
    else:  # THRESHOLD_SHORT
        fired = value < limit
        values["shortfall"] = limit - value
    return fired, values


def evaluate(facts: Facts, rules) -> Verdict:
    """Apply ``rules`` to ``facts``.

    Outcome, in order of precedence:
      no gating rules at all        -> no_verdict
      any block fired               -> do_not_file_yet (a known block is final)
      any gating rule lacks a fact  -> facts_pending
      any deduction fired           -> file_with_known_deduction
      otherwise                     -> file

    Grounds attach to whatever the gating rules decide. Facts that only a
    ground or a calculation needs are listed in ``facts_pending`` so they get
    asked, but they never hold the verdict: a missing ground or amount never
    makes the answer more permissive.
    """
    facts = derive(facts)
    hits: dict[str, list[Hit]] = {BLOCK: [], DEDUCTION: [], GROUND: []}
    pending: dict[str, None] = {}
    gate_pending = False

    for rule in rules:
        missing = []
        applies = True
        for name, expected in rule.when:
            actual = getattr(facts, name)
            if actual is None:
                missing.append(name)
            elif actual != expected:
                applies = False
                break
        if not applies:
            continue

        missing += [n for n in rule.facts if getattr(facts, n) is None and n not in missing]
        if missing:
            pending.update(dict.fromkeys(missing))
            if rule.effect != GROUND:
                gate_pending = True
            continue

        fired, values = _check(rule, facts)
        if not fired:
            continue

        if rule.calculation:
            calc = CALCULATIONS[rule.calculation]
            unread = [n for n in calc.reads if getattr(facts, n) is None]
            if unread:
                pending.update(dict.fromkeys(unread))
                values.update({name: None for name in calc.outputs}, pending=True)
            else:
                values.update(calc.run(facts, values), pending=False)
        hits[rule.effect].append(Hit(rule.id, rule.kind, rule.effect, values))

    if not any(rule.effect != GROUND for rule in rules):
        outcome = NO_VERDICT
    elif hits[BLOCK]:
        outcome = DO_NOT_FILE_YET
    elif gate_pending:
        outcome = FACTS_PENDING
    elif hits[DEDUCTION]:
        outcome = FILE_WITH_KNOWN_DEDUCTION
    else:
        outcome = FILE

    possible_on = None
    if outcome == DO_NOT_FILE_YET:
        dates = [hit.values.get("claimable_from") for hit in hits[BLOCK]]
        if all(d is not None for d in dates):
            possible_on = max(dates)

    return Verdict(
        outcome=outcome,
        blocks=tuple(hits[BLOCK]),
        deductions=tuple(hits[DEDUCTION]),
        grounds=tuple(hits[GROUND]),
        facts_pending=tuple(pending),
        possible_on=possible_on,
        next_action=COVERAGE_QUERY if outcome == DO_NOT_FILE_YET else None,
        respondent=facts.respondent,
        distributor_owned=facts.distributor_owned,
    )


# --- Claim papers: what an insurer would query -----------------------------------
# Cross-checks between her policy and hospital bill. A mismatch is not a coverage
# block: it is the query letter that would delay her claim, caught before she files.

PAPER_CHECKS = (
    "patient_named_on_policy",
    "admission_in_policy_period",
    "discharge_after_admission",
    "bill_adds_up",
    "non_payable_items",
)


@dataclass(frozen=True)
class Papers:
    """Values read from her documents that cleared the confidence gate. None means not known."""

    insured_names: tuple[str, ...] | None = None  # policy
    policy_start_on: date | None = None  # policy: first start (inception)
    policy_end_on: date | None = None  # policy: end of the current period
    patient_name: str | None = None  # bill
    admission_on: date | None = None  # bill
    discharge_on: date | None = None  # bill
    bill_total: float | None = None  # bill: the total as printed
    bill_lines: tuple[tuple[str, float | None], ...] | None = None  # bill: (description, amount)


@dataclass(frozen=True)
class Finding:
    check: str  # one of PAPER_CHECKS
    problem: str  # what was found, e.g. name_mismatch
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PapersCheck:
    findings: tuple[Finding, ...]
    passed: tuple[str, ...]  # checks that ran and found nothing
    skipped: tuple[str, ...]  # checks that could not run: a value was missing or unclear


_TITLES = frozenset("mr mrs ms miss master baby shri smt sri kumari kum dr late".split())


def _words(text: str) -> list[str]:
    return "".join(ch.lower() if ch.isalnum() else " " for ch in text or "").split()


def _initial_or_same(a: str, b: str) -> bool:
    return a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))


def _same_order(a: list[str], b: list[str]) -> bool:
    if len(a) == 1 or len(b) == 1:  # one word only: it must be the other's first or last name
        one, other = (a, b) if len(a) == 1 else (b, a)
        return one[0] in (other[0], other[-1])
    if a[-1] != b[-1] or not _initial_or_same(a[0], b[0]):
        return False  # first and last names must agree; the surname exactly
    short, long = sorted((a[1:-1], b[1:-1]), key=len)
    position = 0
    for word in short:  # middle names: missing or shortened to an initial is fine, in order
        while position < len(long) and not _initial_or_same(word, long[position]):
            position += 1
        if position == len(long):
            return False
        position += 1
    return True


def same_person(a: str, b: str) -> bool | None:
    """Whether two printed names are the same person, as a claims desk would read them.

    Titles are ignored; a middle name may be missing or an initial (R. S. Patil is Ramesh
    Shankar Patil); the surname may come first. The surname must match exactly, so Patel
    is not Patil. None when they cannot be compared: a name is empty, or the scripts differ.
    """
    wa = [w for w in _words(a) if w not in _TITLES]
    wb = [w for w in _words(b) if w not in _TITLES]
    if not wa or not wb or all(w.isascii() for w in wa) != all(w.isascii() for w in wb):
        return None
    return _same_order(wa, wb) or _same_order(wa, wb[1:] + wb[:1]) or _same_order(wa[1:] + wa[:1], wb)


def mentions(description: str, keyword: str) -> bool:
    """Whole-word match, case-insensitive, plural allowed: "Gloves and masks" mentions "glove"."""
    words, wanted = _words(description), _words(keyword)
    if not wanted:
        return False
    for start in range(len(words) - len(wanted) + 1):
        window = words[start: start + len(wanted)]
        if all(w in (k, k + "s", k + "es") for w, k in zip(window, wanted)):
            return True
    return False


def check_papers(papers: Papers, non_payable: tuple[str, ...] = ()) -> PapersCheck:
    """Run every paper check. A check missing any value it needs is skipped, never passed."""
    findings: list[Finding] = []
    passed: list[str] = []
    skipped: list[str] = []

    def done(check: str, found: list[Finding]) -> None:
        findings.extend(found)
        if not found:
            passed.append(check)

    # The patient must be someone the policy insures.
    names = [n for n in papers.insured_names or () if n and n.strip()]
    verdicts = [same_person(papers.patient_name, n) for n in names] if papers.patient_name else []
    if not verdicts or all(v is None for v in verdicts):
        skipped.append("patient_named_on_policy")
    else:
        done("patient_named_on_policy", [] if any(verdicts) else [Finding(
            "patient_named_on_policy", "name_mismatch",
            {"patient": papers.patient_name, "insured": tuple(names)},
        )])

    # Admission inside the policy: not before it began, not after the current period ends.
    if papers.admission_on is None or (papers.policy_start_on is None and papers.policy_end_on is None):
        skipped.append("admission_in_policy_period")
    else:
        found = []
        if papers.policy_start_on is not None and papers.admission_on < papers.policy_start_on:
            found.append(Finding("admission_in_policy_period", "admission_before_policy",
                                 {"admission": papers.admission_on, "start": papers.policy_start_on}))
        if papers.policy_end_on is not None and papers.admission_on > papers.policy_end_on:
            found.append(Finding("admission_in_policy_period", "admission_after_policy",
                                 {"admission": papers.admission_on, "end": papers.policy_end_on}))
        if found or (papers.policy_start_on is not None and papers.policy_end_on is not None):
            done("admission_in_policy_period", found)
        else:
            skipped.append("admission_in_policy_period")  # only one end of the period is known

    # Discharge on or after admission.
    if papers.admission_on is None or papers.discharge_on is None:
        skipped.append("discharge_after_admission")
    else:
        done("discharge_after_admission", [] if papers.discharge_on >= papers.admission_on else [Finding(
            "discharge_after_admission", "discharge_before_admission",
            {"admission": papers.admission_on, "discharge": papers.discharge_on},
        )])

    # The printed total equals the lines.
    lines = papers.bill_lines or ()
    if papers.bill_total is None or not lines or any(amount is None for _, amount in lines):
        skipped.append("bill_adds_up")
    else:
        lines_total = sum((_dec(amount) for _, amount in lines), Decimal(0))
        total = _dec(papers.bill_total)
        done("bill_adds_up", [] if abs(lines_total - total) < 1 else [Finding(
            "bill_adds_up", "bill_total_mismatch",
            {"lines_total": _rupees(lines_total), "bill_total": _rupees(total)},
        )])

    # Items insurers usually do not pay.
    if not lines or not non_payable:
        skipped.append("non_payable_items")
    else:
        items = tuple(
            (description, amount) for description, amount in lines
            if any(mentions(description, keyword) for keyword in non_payable)
        )
        amounts = [amount for _, amount in items if amount is not None]
        done("non_payable_items", [Finding(
            "non_payable_items", "non_payable_items",
            {"items": items, "amount": _rupees(sum((_dec(a) for a in amounts), Decimal(0)))},
        )] if items else [])

    return PapersCheck(tuple(findings), tuple(passed), tuple(skipped))
