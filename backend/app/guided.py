"""The web chat's start options, as guided conversations.

Three journeys, picked at the start of the chat (a ``journey_chosen`` event, the same one WhatsApp writes):

* ``find``: Praman asks what she needs (who is covered, their ages, existing illness, the amount, the
  term), then lists policies found online and what to check before buying. It works for her parents too.
  What she answers stays in memory for the length of the conversation and is never stored. Only the
  kind of cover leaves the process (to the web search): no ages, no names, no illness, and the search
  text is masked like every other outbound text.
* ``check``: send the policy and ask about it (the conversation's own flow).
* ``complain``: tell what went wrong, with the policy or just its number. Praman answers as it always
  does; when that does not settle it, or she asks for a person, the complaint is registered with how to
  reach her (``complaints.register``) and shows on the console's Complaints tab.

Options, never a ranking; nothing here promises approval or payment, and nothing is sent to an insurer.
The state of a conversation is in memory only: a restart means she starts the question list again.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from app import complaints, store
from app.conversation import _NO, _YES, Message, _normalised
from app.services import policy_search
from app.services.redact import redact
from app.store import Store

JOURNEYS = ("find", "check", "complain")
_LOCK = threading.Lock()
_FIND: dict[str, dict[str, Any]] = {}  # user -> {"kind": str | None, "answers": {}, "step": str}
_COMPLAINT: dict[str, dict[str, Any]] = {}  # user -> {"stage": describe|contact|confirm, ...}

# --- Find a policy -------------------------------------------------------------------

OPEN_FIND = (
    "I can help you find the right kind of policy, for you or for your parents. I will ask a few questions, "
    "then show what I find online and what to check before you buy. Which insurance are you looking for?"
)
KIND_OPTIONS = (("find:health", "Health"), ("find:life", "Life"), ("find:motor", "Motor"))
_YES_NO_SKIP = (("illness:yes", "Yes"), ("illness:no", "No"), ("illness:skip", "Prefer not to say"))
_STEPS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "who": ("Who is this insurance for?", (
        ("who:self", "Myself"), ("who:family", "Me and my family"), ("who:parents", "My parents"),
        ("who:spouse", "My spouse"), ("who:child", "My child"))),
    "ages": ("How old is each person to be covered? For example: 62 and 58.", ()),
    "illness": (
        "Does anyone to be covered already have an illness or a past surgery, such as diabetes, blood pressure "
        "or heart trouble? I use this only to know which waiting periods to look at. I do not send it anywhere.",
        _YES_NO_SKIP),
    "cover": ("How much cover do you want? Pick one or type an amount.", (
        ("cover:5 lakh", "₹5 lakh"), ("cover:10 lakh", "₹10 lakh"), ("cover:25 lakh", "₹25 lakh"),
        ("cover:50 lakh or more", "₹50 lakh or more"), ("cover:not sure", "Not sure"))),
    "budget": ("What yearly premium can you comfortably pay? Type an amount, or tap Skip.", (("skip", "Skip"),)),
    "goal": ("Do you want protection for your family if something happens to you, or savings that pay out at maturity?", (
        ("goal:term", "Protection (term cover)"), ("goal:savings", "Savings with a payout at maturity"),
        ("goal:both", "Both, or not sure"))),
    "age": ("How old are you?", ()),
    "amount": ("How much do you want, as cover for your family or as the payout at maturity? For example: 50 lakh.", ()),
    "years": ("For how many years should the policy run? For example: 20.", ()),
    "vehicle": ("What do you want to insure?", (("vehicle:car", "Car"), ("vehicle:two-wheeler", "Two-wheeler"))),
    "vehicle_age": ("How old is the vehicle?", (
        ("vage:new", "New"), ("vage:1 to 5 years", "1 to 5 years"), ("vage:older than 5 years", "Older than 5 years"))),
    "cover_type": ("What should the policy cover?", (
        ("ctype:damage to others only (third-party)", "Damage to others only"),
        ("ctype:others and my own vehicle (comprehensive)", "Others and my own vehicle"),
        ("ctype:not sure", "Not sure"))),
}
_ORDER = {
    "health": ("who", "ages", "illness", "cover", "budget"),
    "life": ("goal", "age", "amount", "years", "budget"),
    "motor": ("vehicle", "vehicle_age", "cover_type"),
}
_OPTIONAL = {"budget"}
_REASK = {
    "ages": "I did not see an age. Send the ages as numbers, for example 62 and 58.",
    "age": "I did not see an age. Send it as a number, for example 45.",
    "years": "Send the number of years, for example 20.",
}
_SENIOR_AGE = 60


def _normal(text: str) -> str:
    return _normalised(text)


def _pick(text: str, options: tuple[tuple[str, str], ...]) -> str | None:
    """The option she chose: its id (a tap), its label, or its label as whole words inside a sentence."""
    said = _normal(text)
    if not said:
        return None
    for option_id, label in options:
        if said in (_normal(option_id), _normal(label)):
            return option_id
    for option_id, label in options:
        if _normal(label) and f" {_normal(label)} " in f" {said} ":
            return option_id
    return None


def _value(option_id: str) -> str:
    return option_id.split(":", 1)[1] if ":" in option_id else option_id


def _numbers(text: str, low: int, high: int) -> list[int]:
    return [n for n in (int(d) for d in re.findall(r"\d{1,3}", text or "")) if low <= n <= high]


def _clean(text: str) -> str:
    """Her typed amount or note, masked and short: it is shown back, never computed with."""
    return redact((text or "").strip())[:40]


def _question(step: str, parents: bool = False) -> Message:
    text, options = _STEPS[step]
    if step == "ages" and parents:
        text = "How old is each of your parents? For example: 62 and 58."
    return Message(text, buttons=options)


def forget(user: str) -> None:
    """Drop what she was in the middle of (delete everything, a new journey)."""
    with _LOCK:
        _FIND.pop(user, None)
        _COMPLAINT.pop(user, None)


def open_journey(conn: Store, user: str, case: dict, journey: str) -> tuple[Message, ...]:
    """She picked a start option: remember it, and say what happens next."""
    if journey not in JOURNEYS:
        raise ValueError(f"unknown journey {journey!r}")
    forget(user)
    store.record_event(conn, case["id"], "journey_chosen", {"journey": journey})
    if journey == "find":
        with _LOCK:
            _FIND[user] = {"kind": None, "answers": {}, "step": "kind"}
        return (Message(OPEN_FIND, buttons=KIND_OPTIONS),)
    if journey == "check":
        return (Message("Send a photo or PDF of your policy, then ask me anything about it."),)
    with _LOCK:
        _COMPLAINT[user] = {"stage": "describe", "parts": [], "last4": None, "want_person": False}
    return (Message(
        "Tell me what went wrong, in your own words. If you have the policy or the insurer's letter, send it too, "
        "or just type the policy number. If I cannot settle it, I will register your complaint for our team."),)


def _checks(kind: str, a: dict[str, Any]) -> list[str]:
    """What to check before buying, from her answers. Things to ask and read, never facts about an insurer."""
    if kind == "health":
        lines = [
            "the sum insured, and whether it is shared by the whole family or per person",
            "the room-rent limit, and the co-payment you would pay on each claim",
            "which hospitals are in the insurer's cashless network near you",
        ]
        if a.get("illness") == "yes":
            lines.append("the waiting period before an illness you already have is covered, and whether it is listed as excluded")
        if a.get("who") == "parents" or any(age >= _SENIOR_AGE for age in a.get("ages", [])):
            lines += [
                "the entry-age limit, and whether the policy can be renewed for life",
                "any co-payment or sub-limit that applies to senior citizens or to treatments such as cataract and joint replacement",
                "whether a medical check-up is needed before the policy is issued",
            ]
        lines.append("how claims are settled (cashless or reimbursement) and how long the insurer says it takes")
        return lines
    if kind == "life":
        lines = ["the policy term and the premium-paying term, and what happens if you stop paying"]
        if a.get("goal") in ("savings", "both"):
            lines += [
                "which part of the payout at maturity is guaranteed and which is not",
                "the charges, and what you get back if you surrender the policy early",
            ]
        if a.get("goal") in ("term", "both"):
            lines.append("what the policy does not pay for, and how a family claim is made")
        lines.append("the insurer's own wording on all of this: read it before you pay")
        return lines
    return [
        "whether it covers damage to others only, or your own vehicle too",
        "the add-ons you actually want (for example zero depreciation or engine cover) and what each one excludes",
        "how a claim is made, and which garages are cashless",
    ]


def _needs(kind: str, a: dict[str, Any]) -> str:
    """What goes to the web search: the kind of cover only. No ages, names or illness details."""
    parts = []
    if kind == "health":
        if a.get("who") in ("parents",) or any(age >= _SENIOR_AGE for age in a.get("ages", [])):
            parts.append("senior citizens")
        if a.get("who") in ("family", "spouse", "child"):
            parts.append("family floater")
        if a.get("illness") == "yes":
            parts.append("pre-existing disease cover")
        if a.get("cover"):
            parts.append(f"sum insured about {a['cover']}")
    elif kind == "life":
        parts.append("savings plan with maturity benefit" if a.get("goal") == "savings" else
                     "term plan" if a.get("goal") == "term" else "term and savings plans")
        if a.get("amount"):
            parts.append(f"about {a['amount']}")
        if a.get("years"):
            parts.append(f"{a['years']} year term")
    else:
        parts.append(a.get("vehicle", "vehicle"))
        if a.get("cover_type"):
            parts.append(a["cover_type"])
    return ", ".join(parts)


def _finish(conn: Store, case: dict, kind: str, a: dict[str, Any], language: str) -> tuple[Message, ...]:
    options = policy_search.suggest(kind, _needs(kind, a), language)
    messages = []
    if options:
        messages.append(Message(options, unverified=True, localized=True))
    else:
        messages.append(Message(
            "I could not look online just now, so I cannot list policies. The checks below still apply, and you can "
            "send me any policy you are considering and I will read it for you."))
    checks = "\n".join(f"- {line}" for line in _checks(kind, a))
    messages.append(Message(
        f"Before you buy, check:\n{checks}\n\nI do not rank insurers and cannot say what you will be approved for. "
        "Send me a policy you are considering and I will read these out of it."))
    return tuple(messages)


def _find_turn(conn: Store, user: str, case: dict, text: str, language: str) -> tuple[Message, ...] | None:
    with _LOCK:
        flow = _FIND.get(user)
    if flow is None:
        return None
    said = _normal(text)
    if flow["step"] == "kind":
        picked = _pick(text, KIND_OPTIONS)
        if picked is None:
            return (Message("Which insurance: health, life or motor?", buttons=KIND_OPTIONS),)
        flow["kind"] = _value(picked)
        store.record_event(conn, case["id"], "find_kind", {"kind": flow["kind"]})
        flow["step"] = _ORDER[flow["kind"]][0]
        return (_question(flow["step"]),)

    kind, step, answers = flow["kind"], flow["step"], flow["answers"]
    options = _STEPS[step][1]
    if said in {"cancel", "stop", "start over", "never mind"}:
        forget(user)
        return (Message("Okay, I have stopped the questions. Pick a start option any time."),)
    if step in _OPTIONAL and said in {"skip", "no", "none", "skip it"}:
        pass
    elif options and step not in _OPTIONAL:
        picked = _pick(text, options)
        if picked is None:
            if step == "cover":  # she may type an amount instead of tapping one
                cleaned = _clean(text)
                if not cleaned:
                    return (_question(step, answers.get("who") == "parents"),)
                answers[step] = cleaned
            else:
                return (Message("Please pick one of these.", buttons=options),)
        else:
            answers[step] = _value(picked)
    elif step in ("ages", "age"):
        ages = _numbers(text, 1, 99)
        if not ages:
            return (Message(_REASK[step]),)
        answers["ages"] = ages[:8]
    elif step == "years":
        years = _numbers(text, 1, 60)
        if not years:
            return (Message(_REASK["years"]),)
        answers["years"] = years[0]
    else:  # a typed amount (amount, budget)
        cleaned = _clean(text)
        if not cleaned:
            return (_question(step),)
        answers[step] = cleaned

    order = _ORDER[kind]
    index = order.index(step) + 1
    if index < len(order):
        flow["step"] = order[index]
        return (_question(flow["step"], answers.get("who") == "parents"),)
    with _LOCK:
        _FIND.pop(user, None)
    return _finish(conn, case, kind, answers, language)


# --- Complaint -------------------------------------------------------------------------

_WANTS_PERSON = re.compile(
    r"\b(talk|speak|connect|call)\b.*\b(someone|somebody|person|human|agent|representative|executive|team|you)\b"
    r"|\b(human|agent|representative|executive)\b|\bcall me\b|\bregister\b.*\bcomplaint\b"
    r"|\b(not|still)\b.*\b(solved|resolved|fixed|working)\b|\bunresolved\b"
    r"|कोणाशी|किसी से|बात करा|बात करनी|बात करना|इंसान"
)
_SUMMARY_CHARS = 220
_NUMBER_FILLER = {"my", "the", "policy", "number", "no", "num", "is", "it", "its", "here", "this", "a", "of"}


def wants_person(text: str) -> bool:
    said = _normal(text)
    return said == "talk" or bool(_WANTS_PERSON.search(said))


def complaint_hint() -> tuple[Message, ...]:
    """Said under Praman's answer in the complaint journey: how to get a person."""
    return (Message("If this has not settled it, tap Talk to someone and I will register your complaint for our team.",
                    buttons=(("talk", "Talk to someone"),)),)


def _complaint_class(conn: Store, case_id: str) -> tuple[str | None, str | None]:
    found = store.latest_event(conn, case_id, "grievance_reported")
    detail = (found or {}).get("detail") or {}
    return detail.get("grievance_class"), detail.get("product")


def _ask_contact() -> Message:
    return Message("What phone number or email can our team reach you on? Say skip if you would rather not say.")


def escalate(conn: Store, user: str, case: dict, seed: str = "") -> tuple[Message, ...]:
    """She said the answer did not help, or asked for a person: register the complaint, starting with how to
    reach her. ``seed`` is what she asked, used when she has not described the problem yet."""
    open_one = complaints.open_complaint(conn, case["id"])
    if open_one is not None:
        return (Message(f"Your complaint C-{open_one['id']} is already registered. Our team will contact you on "
                        f"{complaints.mask_contact(open_one.get('contact'))}."),)
    with _LOCK:
        flow = _COMPLAINT.setdefault(user, {"stage": "describe", "parts": [], "last4": None, "want_person": False})
        if seed and not flow["parts"]:
            flow["parts"].append(seed)
        if not flow["parts"]:
            flow["want_person"] = True
            return (Message("I will get a person to look at this. First, tell me in a line what went wrong, so you "
                            "do not have to explain it again."),)
        flow["stage"] = "contact"
    return (_ask_contact(),)


def _confirm(flow: dict) -> Message:
    summary = " ".join(flow["parts"])[:_SUMMARY_CHARS]
    policy = f" Policy ending {flow['last4']}." if flow.get("last4") else ""
    return Message(
        f"I will register this complaint for our team: \"{redact(summary)}\".{policy} They can reach you on "
        f"{complaints.mask_contact(flow.get('contact'))}. Your complaint, this summary and your contact go to our "
        "support team only. Nothing is sent to your insurer. Reply YES to register, or NO.")


def _complaint_turn(conn: Store, user: str, case: dict, text: str) -> tuple[Message, ...] | None:
    with _LOCK:
        flow = _COMPLAINT.get(user)
    if flow is None:  # after a registration, or a restart: asking for a person still works
        return escalate(conn, user, case) if wants_person(text) else None
    said = _normal(text)
    stage = flow["stage"]
    if stage == "contact":
        if said in {"skip", "no", "none"}:
            flow["contact"] = None
        else:
            contact = complaints.parse_contact(text)
            if contact is None:
                return (Message("I did not see a phone number or an email. Send a 10-digit mobile number or an email, or say skip."),)
            flow["contact"] = contact
        flow["stage"] = "confirm"
        return (_confirm(flow),)
    if stage == "confirm":
        if said in _YES:
            grievance_class, product = _complaint_class(conn, case["id"])
            store.record_consent(conn, case["id"], complaints.CONSENT, True)
            number = complaints.register(
                conn, case["id"], " ".join(flow["parts"]), flow.get("contact"), flow.get("last4"),
                product or case.get("product"), grievance_class)
            with _LOCK:
                _COMPLAINT.pop(user, None)
            return (Message(
                f"Your complaint is registered. Your reference is C-{number}. Our team will contact you on "
                f"{complaints.mask_contact(flow.get('contact'))}. Nothing has been sent to your insurer. You can keep "
                "asking me questions."),)
        if said in _NO:
            flow["stage"] = "describe"
            return (Message("Okay, I have not registered it. Tell me more, or tap Talk to someone when you are ready.",
                            buttons=(("talk", "Talk to someone"),)),)
        return (_confirm(flow),)

    # describing the problem
    last4 = complaints.policy_last4(text)
    if last4:
        flow["last4"] = last4
    if wants_person(text):
        words = text if len(said.split()) > 6 else ""
        return escalate(conn, user, case, seed=words)
    if last4 and not (set(said.split()) - _NUMBER_FILLER - {w for t in re.findall(r"[A-Za-z0-9/\-]+", text) if t[-4:] == last4 for w in _normal(t).split()}):
        # she only gave the policy number, with at most "my policy number is" around it
        return (Message(f"Noted: a policy ending {last4}. I cannot look a policy up by its number, but our team will "
                        "see it. Now tell me what went wrong."),)
    flow["parts"].append(text)
    if flow.get("want_person"):
        flow["want_person"] = False
        flow["stage"] = "contact"
        return (_ask_contact(),)
    return None


def turn(conn: Store, user: str, case: dict, journey: str | None, text: str, language: str) -> tuple[Message, ...] | None:
    """One message in a guided journey. None means: not part of a guided question, answer it as usual."""
    if journey == "find":
        return _find_turn(conn, user, case, text, language)
    if journey == "complain":
        return _complaint_turn(conn, user, case, text)
    return None
