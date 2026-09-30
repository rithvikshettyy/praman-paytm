# PRAMAN — BUILD PRD: PAYTM EDITION (v2)

*Extends `PRD.md` (the HAQ v2 build). That document describes what exists and is
true today: the grievance ladder engine, the WhatsApp channel, the document
explainer. This one describes only the delta — what changes and what gets added so
the product fits the **AI-Powered Financial Journeys** track at the Paytm Build for
India hackathon, Mumbai edition.*

**Governing rule (unchanged from v2):** if it is not on stage, it does not get built.

| | |
|---|---|
| Team | Hack_Overflow |
| Track | AI-Powered Financial Journeys |
| Build window | 8 hours |
| Deliverable | One live demo, one repo, one video or deck |
| Baseline | 11 modules · 3,268 lines · 145 tests · 15 endpoints |
| Prior work | The Praman platform pre-dates this hackathon and is disclosed as such (confirm hackathon rules permit this before relying on the baseline in the pitch) |

### Changelog (v1 → v2)

| Change | Why |
|---|---|
| Added fact 4 to Part 0: the distributor relay problem | Paytm's own operational pain was missing from v1 |
| C1: `motor_policy` reserved in `product`; `distributor_owned` flag | Signals extensibility; powers the console |
| C2: bill heads, continuous cover, denial reason facts | Needed for correct deduction maths and the moratorium rule |
| C4: `threshold_short` rule kind; `duration_met` unlock kind | Removes inverted-comparison confusion; supports rules that help rather than block |
| C5: four more items to verify | Proportional deduction exemptions, moratorium, PED cap, broker licence dates |
| N1: room cap rule rewritten; `moratorium_reached` and `ped_wait_exceeds_cap` added | v1 would have computed a wrong deduction |
| N4 (lending): demoted to stretch | Two half-demos lose to one whole one |
| N5: extended with distributor ownership | Router now answers Paytm's question, not only the user's |
| N8: promoted into the build as the Distributor Console | Only surface that shows Paytm its own workload dropping |
| N9 (new): motor ruleset, stretch | Motor is a major Paytm product; judges will ask |
| Part 5: build order revised | N5 and N8 in; N4 out unless ahead |
| Part 9 (new): stage framing and demo script | "Stops avoidable rejections", not "refuses" |

---

## Part 0 — What changed in the thinking

Four facts about the sponsor set the shape of this delta. None of them are about
technology.

1. **Paytm distributes; it does not underwrite.** Loans are issued by partner banks
   and NBFCs with Paytm as the lending service provider. Insurance is sold through
   Paytm Insurance Broking, whose IRDAI broking licence was renewed in February 2026
   and runs to February 2029, in the Direct (Life & General) category. *(Source must
   be cited on the slide; see C5.)* A broker owes the policyholder service *after*
   the sale, including claims assistance. That duty is the commercial opening.
2. **So the product must know who owes the customer an answer.** Today every case
   becomes `banking/*` and points at one ladder. That is wrong for this domain: the
   respondent may be the insurer, the lender, or Paytm as the distributor, and each
   has a different first step and a different clock.
3. **The refusal is still the moat.** Do not dilute it. The engine's value is that it
   says *no, not yet, and here is why* — and that the no is a tested function rather
   than a model's opinion. Every new rule below is a refusal, not a recommendation.
4. **Paytm's support desk is a relay.** The customer paid Paytm, so the customer asks
   Paytm, even when the insurer owns the answer. Paytm agents then chase the insurer
   on the customer's behalf. Every case the product routes straight to the party that
   owes the answer is a ticket Paytm never handles. This is the number Paytm's judges
   care about, and v1 had no surface that showed it.

**Stage framing:** internally the refusal is the moat; on stage it is **"stops
avoidable rejections before they happen."** Same feature. One sounds like a no, the
other sounds like revenue protection.

**What we do not do:** recommend which loan or policy to buy, predict approval, file
anything automatically, or put the sponsor's name in the product name.

---

## Part 1 — What does not change

Protect these. They are why the demo is credible.

| Component | Why it stays untouched |
|---|---|
| `haq/core/ladder_engine.py` — purity contract | No network, no LLM, no randomness. Every new rule obeys it. |
| Fact-sheet `None` semantics | "Nobody asked" ≠ "no". A missing fact blocks a verdict; it never defaults to the permissive answer. |
| `verified_by` gate on legal content | Unverified content is labelled, in the repo and on screen. |
| Approval before anything leaves | The draft is read back in her language and she says yes. No silent sends. |
| One process, adapters at the edge | The claims and lending modules are rule sets and schemas, not a second app. |

---

## Part 2 — Changes to existing functionality

### C1 · Classifier learns products, not just grievances
**Where:** `haq/core/agent.py:71` (`CLASSIFY_PROMPT`), and the fact key map above it.

Today `grievance_class` is one of `banking/*`, `rti/no_response`, `other`, and the
intent is `grievance | question`. Neither can express "she has not been wronged yet —
she is about to sign something."

Add a third intent and two class families:

```
intent:          grievance | question | pre_decision
grievance_class: banking/*        (unchanged)
                 lending/undisclosed_charge | lending/kfs_mismatch
                 lending/wrong_emi | lending/disbursal_failed
                 lending/recovery_conduct | lending/foreclosure
                 insurance/claim_denied | insurance/claim_delayed
                 insurance/mis_sold | insurance/policy_mismatch
                 platform/payment_failed | platform/refund | platform/app_issue
                 rti/no_response | other
product:         health_policy | merchant_loan | motor_policy | null
```

`motor_policy` is reserved: the classifier may emit it, and N9 consumes it if built.
If N9 is not built, a `motor_policy` case falls through to the router (N5) and gets
the correct respondent with no readiness verdict.

`platform/*` classes exist so the router can say "this one is genuinely Paytm's" and
nothing else is.

`pre_decision` carries the Fair Offer Check and the claim readiness check — the two
moments where a refusal is worth most, because nothing has gone wrong yet.

**Acceptance:**
- A Marathi voice note about a loan offer classifies as `pre_decision` +
  `product=merchant_loan` and never as `banking/service_deficiency`.
- "Premium was debited twice" classifies as `platform/payment_failed`, not
  `insurance/*`.

### C2 · Facts grows product fields
**Where:** `haq/core/ladder_engine.py` (`Facts`).

Add, all defaulting to `None` so the existing missing-fact discipline holds:

```python
# respondent routing
respondent: str | None = None          # insurer | lender | distributor | bank
distributor_owned: bool | None = None  # True only for platform/* classes
# insurance
policy_start_on: date | None = None
procedure: str | None = None
wait_months: int | None = None
ped_wait_months: int | None = None     # pre-existing disease waiting period
months_held: int | None = None
months_continuous_cover: int | None = None  # incl. portability/renewals, for moratorium
sum_insured: float | None = None
room_cap_per_day: float | None = None
room_quoted_per_day: float | None = None
bill_deductible_heads: float | None = None  # room, nursing, surgeon, OT, etc.
bill_exempt_heads: float | None = None      # pharmacy, consumables, implants, devices, diagnostics
exclusion_listed: bool | None = None
policy_in_force: bool | None = None
denial_reason: str | None = None       # non_disclosure | waiting_period | exclusion | documents | other
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
```

`Verdict` gains two fields: `respondent: str | None` (who this verdict says to write
to) and `distributor_owned: bool | None` (whether Paytm owes the answer). Everything
else on `Verdict` is unchanged.

### C3 · Document extraction becomes per-document-type
**Where:** `haq/services/documents.py:46` (`EXTRACT_SCHEMA`, `FACT_MAP`).

One schema cannot read a rejection letter, a policy wording and a key fact statement.
Replace the single constant with a registry:

```python
EXTRACT_SCHEMAS = {
    "letter":  {...},   # today's schema, unchanged
    "policy":  {...},   # N2
    "bill":    {...},   # N1 room-cap maths: line items grouped into heads
    "kfs":     {...},   # N4 (stretch)
}
FACT_MAPS = { "letter": {...}, "policy": {...}, "bill": {...}, "kfs": {...} }

def detect_doc_type(text_or_first_page) -> str
def extract(file, doc_type: str | None = None) -> dict
```

`detect_doc_type` is a keyword heuristic, not a model: "key fact statement" / "annual
percentage rate" → `kfs`; "sum insured" / "waiting period" → `policy`; "final bill" /
"room charges" / "pharmacy" → `bill`; otherwise `letter`. Cheap, testable, and
wrong-answer-safe because the user is asked to confirm anything the confidence gate
flags.

**Bill head mapping** is a static lookup table in the repo (`data/bill_heads.yaml`),
not a model decision: each line-item keyword maps to `deductible` or `exempt`. Unmapped
lines are asked, not guessed.

**Acceptance:** the existing letter tests still pass untouched; a policy PDF returns
policy fields; a hospital bill returns the two head totals; an unreadable document
still degrades to the confirm-with-user path.

### C4 · Engine gains comparison rules
**Where:** `haq/core/ladder_engine.py` rule dispatch, plus `RULE_FACTS`.

Today's rules are existence and date-window checks. The new rules need four more rule
kinds, all pure:

| Rule kind | Reads | Fires when | Effect |
|---|---|---|---|
| `duration_unmet` | two dates or a month count | elapsed < required | block |
| `duration_met` | a month count + threshold | elapsed ≥ threshold | unlock (strengthens her case) |
| `threshold_breach` | two numbers | actual > allowed | block or deduction |
| `threshold_short` | two numbers | actual < required | block |
| `flag_false` | one boolean fact | the fact is `False` | block |

`threshold_short` replaces v1's use of `threshold_breach` with an inverted comparison
for `documents_incomplete`. One kind, one direction; tests read cleanly.

`duration_met` is the first rule kind that helps rather than blocks. It never
produces a "file" verdict on its own; it attaches a ground to an escalation draft.

Each new rule registers its required facts in `RULE_FACTS` so a missing fact still
lands in `facts_pending` rather than producing a confident wrong verdict.

### C5 · Legal content gets verified or gets removed
**Where:** `data/ladders/rbi_rbios_2026.yaml:16` (`verified_by: UNVERIFIED`),
`data/statutes.json` throughout, and every new YAML in this document.

Every ladder and statute row carries `verified_by: UNVERIFIED` and a generic RBI press
release URL. A judge is right to treat that as unshippable.

Items to settle against primary sources before the demo, in priority order:

1. **Proportional deduction exemptions** (feeds `room_cap_breach`). IRDAI guidance
   excludes pharmacy, consumables, implants, medical devices and diagnostics from
   proportionate deduction. Quote the circular and paragraph. If a Paytm Insurance
   judge sees a whole-bill cut, the headline feature is wrong on stage.
2. **Moratorium period** (feeds `moratorium_reached`). IRDAI Master Circular on
   Health Insurance Business (2024): 60 months of continuous cover, after which no
   claim may be contested on non-disclosure or misrepresentation except proven fraud
   and permanent policy exclusions. Quote the paragraph.
3. **PED waiting period cap** (feeds `ped_wait_exceeds_cap`). Same circular: maximum
   36 months. Quote the paragraph.
4. **The 90-day window** at `data/ladders/rbi_rbios_2026.yaml:52` and the
   `rbios_ninety_day_window` row in `data/statutes.json`. RB-IOS 2026, in force
   1 July 2026, did cut the filing window to 90 days — our reading holds — but
   published summaries also describe a one-year maintainability condition measured
   from registration with the regulated entity. Resolve the interaction against RBI's
   own FAQ PDF for the 2026 scheme, quote the paragraph, and set `verified_by` to a
   real person's name. *(Lower priority now that N4 is stretch.)*
5. **Broker licence dates** in Part 0 fact 1. Cite the IRDAI register entry or the
   company's disclosure on the slide. An unsourced sponsor fact is a free hit for a
   judge.
6. **Loan rejection is not an Ombudsman matter.** A commercial credit decision is
   excluded. The engine already has a `commercial_decision` exclusion; the lending
   ladder must use it, so "they refused my loan" is refused as an escalation and
   redirected.

Anything that cannot be verified in time keeps the `UNVERIFIED` badge and is shown to
the user with that badge. Do not quietly launder it.

### C6 · The "Filed" wording is a lie and must go
**Where:** `haq/channels/whatsapp.py:405`.

The reply says *filed*. What happened is that the user approved a draft inside our own
database. Nothing was sent anywhere. Change the string to say approved and ready to
send, and say the same thing on stage. If a judge opens the repo and finds a "Filed"
that files nothing, the demo is over.

### C7 · Store: products, documents, consent, ownership
**Where:** `haq/store.py` schema block.

```sql
ALTER TABLE cases ADD COLUMN product           TEXT;     -- health_policy | merchant_loan | motor_policy
ALTER TABLE cases ADD COLUMN respondent        TEXT;     -- insurer | lender | distributor
ALTER TABLE cases ADD COLUMN respondent_name   TEXT;     -- e.g. the insurer's legal name
ALTER TABLE cases ADD COLUMN distributor_owned INTEGER;  -- 1 = Paytm owes the answer

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL,
    doc_type    TEXT NOT NULL,      -- policy | bill | kfs | letter | claim_doc
    slot        TEXT,               -- discharge_summary | bill | id_proof | ...
    received_at TEXT NOT NULL,
    fields      TEXT NOT NULL DEFAULT '{}',
    confidence  REAL,
    retained    INTEGER NOT NULL DEFAULT 0   -- 0 = fields kept, original discarded
);

CREATE TABLE IF NOT EXISTS consents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    scope      TEXT NOT NULL,       -- read_policy | read_bill | read_kfs | store_fields | contact_insurer
    granted    INTEGER NOT NULL,
    at         TEXT NOT NULL
);
```

**Default: we keep extracted fields, not the source document.** The original is
discarded once fields are read, unless she asks us to keep it. This is a product
decision, not a nicety — the documents in scope are medical bills and loan
agreements.

---

## Part 3 — New functionality

### N1 · Claim readiness engine — the headline
**New:** `data/ladders/insurance_health_claim.yaml`, rules in `ladder_engine.py`.

A pure function over the fact sheet that answers one question before anything is
filed: *will this claim be rejected, and if not, how much will be cut?*

| Rule id | Kind | Fires when | Message (plain language) |
|---|---|---|---|
| `waiting_period_unmet` | `duration_unmet` | `months_held < wait_months` | "This treatment has a X-month waiting period. Your policy is Y months old. You can claim from [date]." |
| `procedure_excluded` | `flag_false` | procedure is on the exclusions list | "Your policy does not cover this treatment at all." |
| `policy_lapsed` | `flag_false` | `policy_in_force is False` | "The policy was not in force on the date of treatment." |
| `room_cap_breach` | `threshold_breach` | `room_quoted > room_cap` | "Your room costs more than the policy allows. About ₹D will be cut from room, doctor and surgery charges. Medicines, tests and implants are not cut." |
| `documents_incomplete` | `threshold_short` | `collected < required` | "N documents are still missing." |
| `ped_wait_exceeds_cap` | `threshold_breach` | `ped_wait_months > 36` | "Your policy lists a pre-existing disease wait longer than the regulator allows. Ask the insurer to explain." |
| `moratorium_reached` | `duration_met` | `months_continuous_cover ≥ 60` and `denial_reason == "non_disclosure"` | "You have been covered for 5+ years. The insurer cannot reject this for non-disclosure unless it proves fraud." |

**Room cap arithmetic** (in the engine, never in a prompt):

```
ratio      = room_cap_per_day / room_quoted_per_day
deduction  = (1 - ratio) * bill_deductible_heads
exempt     = bill_exempt_heads                  # never reduced
payable_est = (bill_deductible_heads - deduction) + exempt
```

If `bill_deductible_heads` or `bill_exempt_heads` is `None`, the rule still fires the
block message but reports the deduction as "to be calculated once the bill is
shared". It never falls back to cutting the whole bill.

Outcomes are three, not two: **file**, **do not file yet** (with the date it becomes
possible), and **file with a known deduction** — the third is what stops this being a
blunt refusal.

`moratorium_reached` and `ped_wait_exceeds_cap` are the two rules that turn the engine
from a gate into leverage: they give her a regulator-backed ground in the escalation
draft. One of them goes in the demo (see Part 9).

Next action on a block is never "give up": it is `COVERAGE_QUERY` — a written question
to the insurer that starts a clock.

### N2 · Policy extraction
**New:** `policy` schema in `documents.py`.

Fields: insurer, policy number, policy start date, continuous-cover start date (if
ported), sum insured, room-rent limit (absolute or percentage), co-pay,
specified-disease waiting period, pre-existing waiting period, named exclusions,
network status. Each carries a confidence; anything below the gate is asked, not
assumed. Percentages resolve against the sum insured inside the engine, not in the
prompt.

### N3 · Document checklist over WhatsApp
**New:** checklist state on the case; photo intake ticks slots off.

A claim needs a known set of documents. She sends photos one at a time; each is
classified into a slot and the bot replies with what is still missing, by voice. The
fact `documents_collected / documents_required` feeds `documents_incomplete`.

This is the single most demo-legible feature in this document: a checklist that fills
itself from photographs.

### N4 · Fair Offer Check — the lending half *(STRETCH)*
**Status:** built only if the build is ahead at 5:15. The deck keeps one slide:
"same engine, lending ruleset next", showing the rule table below.

**New:** `kfs` schema, `data/ladders/lending_offer.yaml`.

RBI's digital lending rules require a Key Fact Statement before a loan is taken. It is
the one document in this domain that is standardised, mandatory and universally
unread. Praman reads it aloud.

Extracted: lender name (not the app's name), sanctioned amount, processing and other
fees, net disbursal, APR, instalment amount and count, total repayable, late fee,
foreclosure charge, cooling-off period, bundled insurance and whether it was opted
into.

Computed in the engine, never by a model:

```
net_disbursal   = sanctioned_amount - fees
total_repayable = instalment_amount * instalment_count
cost_of_credit  = total_repayable - net_disbursal
```

| Rule id | Blocks when | What she is told |
|---|---|---|
| `kfs_missing` | `kfs_supplied is False` | "They have not given you the key fact statement. Ask for it before you sign." |
| `insurance_not_consented` | bundled and `insurance_consented is not True` | "Insurance was added for you. You can ask for it to be removed." |
| `disbursal_mismatch` | `net_disbursal < sanctioned_amount` | "₹A is the offer; ₹B actually reaches you." |
| `loan_rejection_not_escalable` | class is a refusal-to-lend | "A lender choosing not to lend is not something the Ombudsman will take up." |

The spoken summary is three numbers only: what reaches her, what goes back in total,
and what leaves per day or month. No APR lecture.

### N5 · Respondent router — who owes the answer
**New:** `respondent` and `distributor_owned` resolution in `cases.py`, per-ladder
tier ownership.

One pure function maps (product, grievance_class) to the party who owes an answer,
whether Paytm is that party, and the first step against them:

| Situation | Owner | Paytm owes it? | First step | Then | Then |
|---|---|---|---|---|---|
| Claim denied or delayed | insurer | no | insurer's grievance cell | IRDAI's grievance channel | Insurance Ombudsman |
| Coverage question on her own policy | insurer (answered by Praman from the policy) | no | answered in chat | coverage query to insurer | — |
| Mis-sold or bundled policy | distributor (broker) | **yes** | distributor | insurer | IRDAI |
| Premium debited twice, refund, mandate failure | distributor | **yes** | distributor's support ticket | — | — |
| App or platform issue | distributor | **yes** | distributor's support ticket | — | — |
| Loan servicing, charges, conduct | lender (named in the KFS) | no | lender | lender's nodal officer | RBI Ombudsman |
| Motor claim | insurer | no | insurer's claims desk | insurer's grievance cell | IRDAI |

Three consequences for the demo: the letter is addressed to the right legal entity by
name, the escalation clock started is the right one, and every case carries
`distributor_owned` so the console (N8) can show how many never needed Paytm.

Each row's waiting period and window is a `verified_by` line in the YAML — unverified
rows show the badge.

**Acceptance:** a claim-delay case routes to the insurer with
`distributor_owned=False`; a double-debit case routes to the distributor with
`distributor_owned=True`; neither passes through the other's ladder.

### N6 · Consent, redaction, deletion
**New:** consent prompts at first document, `store.consents`, delete and export paths.

- Field-level consent, asked in her language, before the first document is read.
- Account numbers, Aadhaar, PAN and policy identifiers redacted before any text leaves
  the process.
- "Delete everything" is a WhatsApp command, and it works. **Shown live on stage**:
  ten seconds, lands hard with a regulated-distributor panel.
- Every read, draft and send is an event row — the audit trail already exists; extend
  it rather than inventing a second log.

This is not compliance theatre. The demo involves medical bills and loan agreements,
and a judge from a regulated distributor will ask.

### N7 · Clocks for the new domains *(post-hackathon)*
**Extends:** existing deadline machinery.

New deadline kinds: insurer response window, claim intimation window (from date of
admission), cooling-off expiry on a loan, and a pre-debit reminder one day before each
instalment. The reminder is the retention feature: she hears from Praman *before* the
money leaves, not after the bounce.

Every window is a YAML value with its own `verified_by`.

### N8 · Distributor Console — Paytm's view *(PROMOTED INTO BUILD)*
**New:** aggregate reads over `events` and `cases`, one read-only page.

v1 had these as counters for later. v2 makes them the only screen that shows Paytm
its own benefit, which is what the sponsor's judges are scoring.

**The page has three blocks, nothing else:**

1. **Case list.** One row per case: product, class, respondent name,
   `distributor_owned` badge, verdict, clock state. Filter by "needs Paytm" / "routed
   to insurer or lender".
2. **Headline number.** "Of N cases, X needed Paytm." Computed live from
   `distributor_owned`.
3. **Six counters**, computed from real events: readiness checks run, claims stopped
   before filing and why, known deductions explained, coverage queries drafted,
   escalations drafted, cases routed away from Paytm.

**Rules:** every number on the page comes from an event row. No projected savings, no
cost-per-ticket figure, no invented percentages. If a judge asks "what would this save
Paytm", the answer is "multiply X by your cost per ticket; you know that number, we
don't."

One SQL view plus one server-rendered page. No charts library, no auth.

### N9 · Motor ruleset *(STRETCH, or standing answer)*
**New (if built):** `data/ladders/insurance_motor_claim.yaml`.

Motor is one of Paytm's most-sold insurance lines; a judge from Paytm Insurance may
ask. If time runs out, the standing answer is: "Same engine. Motor is one YAML file;
here are the three rules." If built, three rules only:

| Rule id | Kind | Fires when | Message |
|---|---|---|---|
| `od_without_valid_tp` | `flag_false` | `tp_cover_valid is False` | "Your own-damage policy does not include third-party cover, and third-party cover is required by law. Check it is valid before riding." |
| `zero_dep_absent` | `flag_false` (deduction outcome) | `zero_dep_addon is False` | "You do not have zero depreciation. Expect a cut on replaced parts at claim time." |
| `claim_exceeds_idv` | `threshold_breach` | `claim_amount > idv` | "The most this policy can pay is your IDV of ₹X." |

`zero_dep_absent` returns **file with a known deduction**, not a block. The exact
depreciation percentages are out of scope for the hackathon; the rule states that a
cut applies, not its size.

---

## Part 4 — API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/documents/policy` | Upload a policy, get extracted terms + confidence |
| `POST` | `/api/documents/bill` | Upload a hospital bill, get deductible and exempt head totals |
| `POST` | `/api/documents/kfs` | Upload a loan offer or KFS *(stretch)* |
| `POST` | `/api/readiness` | Fact sheet in, `Verdict` out — the claim or offer check |
| `GET` | `/api/checklist/{case_id}` | Slots, filled and missing |
| `POST` | `/api/checklist/{case_id}` | Attach a photo to a slot |
| `POST` | `/api/consent` | Grant or revoke a scope |
| `DELETE` | `/api/case/{case_id}` | Delete everything for this case |
| `GET` | `/api/console/cases` | Case list for the Distributor Console, filterable by `distributor_owned` |
| `GET` | `/api/metrics` | Headline number + the six counters |
| `GET` | `/console` | The Distributor Console page |

All thin. All logic in plain functions, as `PRD.md` Part 2 requires.

---

## Part 5 — Build order for the eight hours

| Hours | Work | Cut line |
|---|---|---|
| 0:00–0:45 | C1 classifier classes + C2 fact fields + C4 rule kinds, with tests | — |
| 0:45–2:30 | N1 claim rules + `insurance_health_claim.yaml` + room-cap arithmetic, tests first | **This is the demo. It does not get cut.** `moratorium_reached` and `ped_wait_exceeds_cap` can drop to one of the two if behind. |
| 2:30–3:30 | N2 policy extraction + bill extraction + C3 schema registry | Falls back to fixture policy and fixture bill if Doc AI misbehaves |
| 3:30–4:30 | N3 checklist over WhatsApp | Cut to a text list if photo classification is shaky |
| 4:30–5:15 | N5 respondent router + `distributor_owned` + correct letter addressing | Cut to insurer vs distributor only |
| 5:15–6:00 | N8 Distributor Console (one view, one page) | Cut to headline number + case list; counters optional |
| 6:00–6:45 | C6 wording fix, C5 verification pass, N6 consent prompt + live delete | **C6 and C5 do not get cut** |
| 6:45–8:00 | Record the demo, one take, real phone, Marathi | — |

**If ahead at 5:15:** N9 motor rules (30 min) before N4 lending (60 min). Motor is
the more likely judge question.

N7 is post-hackathon.

### Pre-hackathon prep (before 3 Oct, disclosed as prep)
- Settle C5 items 1–3 and 5 against primary sources; write `verified_by`.
- Build `data/bill_heads.yaml` and the fixture policy + fixture bill for the demo.
- Write the failing-case tests for every N1 rule so the build day starts red.
- Rehearse Part 9 script five times.

---

## Part 6 — Tests

The baseline is 145 passing tests and that number is part of the pitch. New tests, all
against the pure engine, no network:

- One test per new rule: blocked case, clear case, and missing-fact case.
- Three-outcome test: file / do not file / file with deduction.
- **Room cap arithmetic:** known bill with deductible and exempt heads; assert exempt
  heads are never reduced and the deduction matches the formula to the rupee.
- **Room cap with missing heads:** rule fires, deduction reported as pending, never a
  whole-bill cut.
- **Moratorium:** 59 months + non-disclosure denial → no ground; 60 months → ground
  attached; 60 months + exclusion denial → no ground (moratorium does not override
  permanent exclusions).
- **PED cap:** 36 → clear; 48 → flagged.
- **Router:** claim delay → insurer, `distributor_owned=False`; double debit →
  distributor, `distributor_owned=True`; mis-sold → distributor first.
- **Console:** headline number equals count of cases with `distributor_owned=False`
  over total, computed from a seeded event log.
- `loan_rejection_not_escalable` returns a redirect, not an escalation *(if N4 built)*.
- Arithmetic: `net_disbursal`, `total_repayable`, `cost_of_credit` on a known offer
  *(if N4 built)*.
- Confidence gate: a low-confidence extracted field never reaches a verdict without
  being confirmed.
- Retention: after extraction with default consent, the stored row has fields and no
  original document.

**Rule:** a rule without a failing-case test does not ship. That is what makes the
refusal defensible on stage.

---

## Part 7 — Risks, stated before a judge states them

| Risk | Standing answer |
|---|---|
| Legal content unverified | Badged in the data, badged on screen, and the rules the demo depends on get primary-source verification before the demo. |
| Deduction figure wrong | Engine never cuts exempt heads; formula is tested to the rupee; missing heads report "pending", never a guess. |
| Nothing is actually filed | True, and said plainly: Praman drafts and readies, and she approves. Sending is a partner integration, not a hackathon claim. |
| Extraction is wrong on a real policy | Confidence-gated. Anything uncertain is asked, in her language. No silent assumption reaches a verdict. |
| The example figures are invented | They are labelled as an example case. No rejection rates, no savings claims, no adoption numbers anywhere. The console shows only counts from events. |
| "What does this save Paytm?" | "Of N cases, X needed you. Multiply by your cost per ticket." We do not invent Paytm's numbers. |
| "What about motor?" | N9 if built; otherwise "same engine, one YAML file, here are the three rules." |
| "What about lending?" | N4 if built; otherwise the rule table slide. |
| Prior work | Disclosed on the first slide. The platform existed; the claims, routing and console modules are this build. Confirm rules permit this beforehand. |
| Paytm Payments Bank | Out of scope entirely. The respondent is an insurer or a partner lender, never the sponsor's wound-down bank. |

---

## Part 8 — Explicitly out of scope

Recommending a policy or a lender. Predicting approval or claim outcome. Automated
filing. Credit scoring. Underwriting. Storing KYC. Life and credit-card ladders.
Motor depreciation percentages and a full motor ladder (N9 covers three rules only).
Projected savings or cost figures for Paytm. Multi-tenancy and accounts. A mobile app.
Any use of the sponsor's brand in the product's name.

---

## Part 9 — Stage framing and demo script

**One line:** Praman stops avoidable claim rejections before they happen, and sends
every question to the party that actually owes the answer.

**Demo, 3 minutes, one real phone, Marathi:**

1. **Claim readiness (60s).** Voice note: her father is being admitted for a
   procedure. She sends a photo of the policy. Praman replies by voice: the room she
   was quoted is above her cap; about ₹D will be cut from room and doctor charges,
   medicines and tests will not be cut; pick the lower room and nothing is cut.
   Outcome: *file with a known deduction*, turned into *file clean*.
2. **Checklist (40s).** She sends three document photos. Each ticks a slot. Praman
   says two are still missing, by name.
3. **Leverage (30s).** Second case, pre-seeded: a claim denied for non-disclosure on a
   policy held six years. Praman explains the moratorium ground and reads back a
   draft to the insurer, addressed by name. She approves. Screen says **approved and
   ready to send**, not filed.
4. **Paytm's view (40s).** Switch to the Distributor Console. Both cases sit under
   "routed to insurer". Headline: "Of N cases, X needed Paytm." One double-debit case
   sits under "needs Paytm", correctly.
5. **Close (10s).** She types "delete everything". The case disappears from the
   console live.

**Slide order:** prior-work disclosure → problem (the relay) → demo → how the refusal
is tested (145 + new tests, pure engine) → what is verified and what is badged →
lending and motor as next rulesets → ask.
