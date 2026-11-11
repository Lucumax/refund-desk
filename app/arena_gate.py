"""The deterministic gate of the refund authority (app/authority.py), used by the refund inbox and the
Injection Arena alike.

It reads the RAW buyer text itself (its own scanner), the order and the signed history. It does not
trust the reader. Rules run in order and the first hit wins; hard blocks (DENY) come before
ESCALATE, as in the core. The amount and budget rules at the end are the vendored core's own policy
checker (approval_receipt.policy.check), fed by a Mandate built from the owner's policy.

An attack flag CHANGES the decision: it forces DENY or ESCALATE. `decide(..., scan_flags=False)` runs
the same gate with the text-flag rules switched off. The arena shows both results, so a visitor sees
what the flags changed. That comparison path never moves money.

What the limits mean (see app/policy_store.py): the cap is an AUTOMATIC allowance per order, adding up
across refunds (G15 enforces the running total, R7 a single refund); the daily budget bounds AUTOMATIC
refunds only (R8). The gate stops a reader from stretching them; it does not decide whether a buyer
is telling the truth. There is no global tightening: one visitor cannot lower the limits for others.

Rule ids: G1 to G17 and G19 are this gate (G18 is raised only by crash recovery, app/authority.py); R7, R8 and R9 come from the core."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from approval_receipt import Mandate, Intent, ALLOW, ESCALATE, DENY
from approval_receipt.policy import check as core_check

from .arena_policy import Policy
from .arena_reader import Proposal, REFUND
from .arena_text import normalize, squeezed, word_amounts
from .consent import is_plain_refund_command, binding_problem
from .consent_combine import decide_consent
from .extractor import (normalize_order_ref, _FULL, _ISSUE_RX, NEGATIVE_WORDED, cited_transaction_ids, refund_amounts,
                        tied_occurrences, malformed_amounts, foreign_currency)

# ---- the scanner ------------------------------------------------------------------------------
# category -> (gate rule that acts on it, patterns). Patterns run on normalized text.
# "If you could / if you can" is politeness ONLY when it is followed by the ask itself ("if you could, please refund",
# "if you can refund $18.00"). "If you can confirm that the courier lost it, please refund" is a condition: whatever the
# buyer asks you to establish first is what the refund depends on.
POLITE_ASK = (r"(?=\s*(?:[,;.?!]|$|please\b|(?:to\s+)?(?:refund|send|give|credit|issue|process|return|pay|reimburse|help|do|look|take|make)\b))")
_POLITE_YOU = (r"you(?:'d|\s+(?:could|can|would|will|might|please|don'?t mind|are able))\b" + POLITE_ASK)
OVERRIDE = [
    r"\bignore (?:all |any |every |your |the |my |these |those )*(?:previous |prior |above |earlier |other )*"
    r"(?:rules?|instructions?|polic(?:y|ies)|limits?|guidelines?|restrictions?|checks?|safeguards?)",
    r"\b(?:disregard|forget|override|bypass|circumvent|skip|disable|turn off|switch off|suspend|lift)\b[^.]{0,40}"
    r"\b(?:rules?|instructions?|polic(?:y|ies)|limits?|guidelines?|restrictions?|checks?|approval|safeguards?|gate|caps?)\b",
    r"\byou are now\b|\bact as\b|\bpretend (?:that |the |you |to )|\bfrom now on\b|\bnew instructions?\b",
    r"\b(?:system|developer|admin|assistant|operator)\s*(?:prompt|message|mode|override|note|:)",
    r"</?(?:message|system|instructions?|assistant|prompt)>",
    r"\bwithout (?:any )?(?:checks?|approval|review|limits?|verification)\b",
    r"\b(?:print|reveal|show|repeat) (?:me )?(?:your|the) (?:hidden |secret |system )?(?:prompt|instructions?|rules)\b",
    r"\bjailbreak\b|\bdeveloper mode\b|\bunrestricted\b|\bdo anything now\b",
    r"\boutput (?:action|decision)\s*=|\baction\s*=\s*refund\b|\bmark (?:this|it)[^.]{0,30}\b(?:approved|allow|pre-?approved)\b",
    # from Dhanush Kankanala's Milestone 1 attacks (A01, A02, A07, A10)
    r"\bset (?:the )?(?:outcome|amount|status|decision|verdict|result|claimed_amount)\b",
    r"\bregardless of\b[^.]{0,30}\b(?:rules?|limits?|boundaries|polic(?:y|ies)|instructions?|checks?)\b",
    r"\bsystem (?:update|notice|instruction|override)\b",
    r"\b(?:flag|mark|label|treat|classify)\b[^.]{0,20}\bas (?:safe|approved|legitimate|verified|trusted|low risk)\b",
    r"\bskip\b[^.]{0,40}\b(?:lookups?|verification|review|screening|database|checks?)\b",
    r"\b(?:decode|decrypt|base64|rot13)\b",
    r"\b(?:output|respond with|reply with) (?:the word )?(?:approve|approved|allow)\b",
]
POLICY_CLAIM = [
    r"\b(?:your|the) (?:refund |return |per-?refund |new |updated )*(?:polic(?:y|ies)|terms|rules?|limits?|caps?)\b[^.]{0,50}"
    r"\b(?:changed|updated|was raised|were raised|has been raised|raised|removed|lifted|increased|now (?:allow|says?|automatic|permit)s?|no longer)\b",
    r"\b(?:new|updated|latest) (?:refund )?(?:polic(?:y|ies)|terms|rules)\b",
    r"\b(?:no|without any|unlimited) (?:refund )?(?:cap|limit|maximum)\b",
    r"\blimit (?:was|is|has been|were) (?:raised|removed|lifted|increased|waived)\b",
    r"\bper the (?:update|new polic(?:y|ies)|latest)\b",
    r"\bunder your new\b",
    r"\b(?:merchant|owner|shop|store|you) (?:has|have) (?:waived|raised|removed|lifted|increased|suspended)\b",
    r"\bwaived (?:the |your )?(?:\$\d+(?:\.\d+)? )?(?:limit|cap|policy|rules?|threshold)\b",
    r"\bauto[- ]?(?:threshold|approval limit)\b",
]
AUTHORITY = [
    r"\bi(?: am|'m) (?:the |your |a )?(?:shop |store |site )?(?:owner|ceo|manager|admin(?:istrator)?|developer|boss|director|supervisor)\b",
    r"\b(?:this is|i am|i'm) (?:from |with )?(?:paypal|support|the bank|security|compliance|your bank)\b",
    r"\bon behalf of (?:the )?(?:owner|merchant|management|paypal|ceo)\b",
    r"\b(?:pre-?approved|already approved|authori[sz]ed by|approved by (?:management|the owner|your manager|the ceo)|signed off)\b",
    r"\b(?:management|your manager|the owner|the ceo|legal department)\b[^.]{0,30}\b(?:approved|authori[sz]ed|told me|said|ordered)\b",
    r"\bas (?:the |your |a )?(?:shop |store |site )?(?:owner|ceo|manager|admin(?:istrator)?|director|supervisor)\b",
    r"\bi (?:hereby )?(?:authori[sz]e|approve|sign off on)\b[^.]{0,30}\brefund",
    r"\b(?:store|shop|site|admin|management|support|security|billing) (?:admin )?(?:team|department)\b",
    r"\b(?:security|compliance|fraud|paypal|bank) (?:auditor|officer|investigator|inspector|agent)\b",
    r"\bi(?: am|'m) (?:an? |the )?(?:official |authori[sz]ed )?(?:paypal|bank|security|compliance|fraud)\b",
]
# Threats only. Urgent or angry wording ("urgent", "right now", "the worst service ever") is NOT a flag (Dhanush Kankanala's
# rule, 2026-10-30: anger alone must not change the outcome); a threat of a chargeback, a lawsuit, a ban or a review campaign is.
PRESSURE = [
    r"\b(?:or else|last warning|final warning|final notice)\b",
    r"\b(?:will be|get|getting) (?:permanently )?(?:banned|suspended|shut down|terminated|blacklisted)\b",
    r"\bi(?: will|'ll| am going to) (?:file|open|start|sue|report|post|leave|dispute|call|go)\b[^.]{0,40}"
    r"\b(?:chargeback|dispute|lawyer|lawsuit|review|reviews|police|media|bbb|attorney)\b",
    r"\b(?:chargeback|lawsuit|sue you|my lawyer|legal action)\b",
]
REPEAT = [
    r"\b(?:refund|pay|send)(?: me)? again\b|\bsecond (?:refund|time|request)\b|\banother refund\b|\brefund (?:it )?twice\b",
    r"\balready (?:refunded|got|received|asked)\b|\bonly (?:sent|refunded|gave) (?:me )?(?:part|half|some)\b|\bthe rest of (?:my|the) refund\b",
    r"\bremaining (?:\$|amount|balance|refund)",
    # earlier compensation, and a further claim on the same order (Dhanush Kankanala's cases A05 and A06)
    r"\b(?:you|they|the (?:shop|store|seller|merchant)|your (?:team|shop|store|support))\s+(?:already|previously)\s+"
    r"(?:gave|sent|issued|paid|refunded|credited|compensated|offered)\b",
    r"\b(?:gift card|store credit|voucher|coupon|goodwill credit)\b|\bas compensation\b",
    r"\b(?:writing|contacting you|messaging you|asking) again\b",
    r"\b(?:is|was|are|were) also (?:broken|damaged|cracked|torn|missing|defective|faulty|chipped|scratched|dented|stained|ruined)\b"
    r"|\b(?:broken|damaged|cracked|torn|defective|faulty|missing) as well\b",
]
CONTRADICTION = [
    # The buyer takes the request back, or makes it depend on something else. A reader that only sees the
    # word "refund" and an amount would send it; the owner should look first.
    r"\bnever ?mind\b|\bforget (?:it|about it|that)\b|\bdisregard (?:my|that|this) (?:request|message|refund)\b"
    r"|\bignore my (?:last|previous|earlier) (?:message|request)\b",
    r"\b(?:do not|don't|dont|no longer|not) (?:need|want|wish|ask(?:ing)? for) (?:a |any |the |my )?(?:refund|money)\b"
    r"|\bno refund (?:is |will be )?(?:needed|necessary|required)\b|\b(?:do not|don't|dont) (?:refund|pay)\b"
    r"|\bcancel (?:my|the|that|this) (?:refund|request)\b",
    r"\binstead of (?:a |the )?(?:refund|money)\b|\b(?:replacement|exchange) (?:instead|rather)\b|\bnot (?:money|a refund)\b",
    r"\bif (?:the|my|it|you|there)\b.{0,60}\b(?:does not|doesn't|do not|don't|isn't|is not|has not|hasn't|fails?|still)\b.{0,40}\b(?:refund|money back)\b",
    r"\brefund\b.{0,60}\b(?:only if|unless|in case|provided that)\b",
    # the buyer withdraws in other words, or makes the refund depend on something ("refund $18 if delivery is late")
    r"\bi(?: hereby)?(?: am| will| would like to)? (?:withdraw(?:ing)?|retract(?:ing)?|rescind(?:ing)?|take(?:n)? back|taking back)\b"
    r"|\bwithdraw(?:n|ing)? (?:my|the|this) (?:request|refund|claim|message|complaint)\b|\bscrap (?:that|my request)\b"
    r"|\b(?:please )?(?:do not|don't|dont) (?:process|send|issue) (?:the |a |my |any )?(?:refund|payment|money)\b",
    r"\b(?:refund|pay|send|credit)\b(?:[^.?!]|(?<=\d)\.(?=\d)){0,40}\b(?:if|assuming|on condition|subject to|depending on|in the event)\b",
    # the condition comes FIRST ("If delivery is late, refund $18"), and the refund is the main clause after the comma
    r"\b(?:if|unless|assuming|provided|providing|in case|in the event|should|depending on|subject to)\b"
    r"(?!\s+(?:" + _POLITE_YOU + r"|possible\b|needed\b|necessary\b))"
    r"[^.?!;]{0,80}[,;]\s*(?:then\s+)?(?:please\s+)?(?:refund|pay|send|credit|give|return)\b",
    # the same, with no comma at all ("If delivery is late please refund $18.00"): punctuation must not decide consent
    r"\b(?:if|unless|assuming|provided|providing|in case|in the event|depending on|subject to)\b"
    r"(?!\s+(?:" + _POLITE_YOU + r"|possible\b|needed\b|necessary\b))"
    r"[^.?!;,]{0,80}?\b(?:then\s+)?(?:please\s+(?:refund|pay|send|credit|give|return)\b|(?:refund|credit)\s+(?:me|us|my|the|\$?\d))",
    # the buyer says it afterwards in the passive ("my request is withdrawn", "this refund request has been cancelled")
    r"\b(?:request|refund|claim|complaint|order)\b(?:\W+\w+){0,3}?\W+(?:is|was|has been|have been|are|now|hereby)\W+"
    r"(?:(?:now|hereby|been|being|to be)\W+)*(?:withdrawn|cancell?ed|retracted|rescinded|void|dropped|revoked)\b",
]
GROUPS = (("G1", "INSTRUCTION_OVERRIDE", OVERRIDE), ("G2", "FALSE_POLICY_CLAIM", POLICY_CLAIM),
          ("G9", "AUTHORITY_CLAIM", AUTHORITY), ("G10", "PRESSURE_OR_THREAT", PRESSURE),
          ("G11", "REPEAT_CLAIM", REPEAT), ("G16", "CONTRADICTORY_INTENT", CONTRADICTION))
_COMPILED = [(rule, cat, [re.compile(p) for p in pats]) for rule, cat, pats in GROUPS]
_SQUEEZED_OVERRIDE = re.compile(
    r"ignore(?:all|any|your|the|previous|prior|above)*(?:rules?|instructions?|policy|polic?ies|limits?|guidelines)"
    r"|(?:bypass|disregard|override)(?:the|your|all)*(?:rules?|approval|limits?|checks?)")


# ---- consent (G17) -------------------------------------------------------------------------------
# Consent is defined in app/consent.py and nowhere else: a message is a refund request when, after NFKC,
# look-alike folding, lower-casing and whitespace collapse, it uses only allowed characters, contains a request from a fixed
# list, carries no hold signal (a condition, a hedge, a withdrawal, another payee, an alternative to money, words aimed at the
# system), and asks no question that is not itself the request. This gate keeps no consent patterns of its own.
def affirmative_refund_request(text: str) -> bool:
    """True only for an unconditional request for money back (app/consent.py). The reader's own quote is never trusted for it.
    Words only: it cannot tell whether the request is sincere."""
    return is_plain_refund_command(text)


@dataclass(frozen=True)
class Flag:
    rule: str           # the gate rule this flag feeds, e.g. "G1"
    category: str       # e.g. "INSTRUCTION_OVERRIDE"
    phrase: str         # the buyer's own words that matched
    obfuscated: bool = False

    def as_dict(self) -> dict:
        return {"rule": self.rule, "category": self.category, "phrase": self.phrase, "obfuscated": self.obfuscated}


def scan(text: str) -> list[Flag]:
    """Deterministic keyword and pattern scan of the RAW text. Short, English only, and honest about
    it: a rewording the lists do not cover is not flagged (see NOT_DONE.md)."""
    norm = normalize(text)
    flags: list[Flag] = []
    for rule, cat, rxs in _COMPILED:
        for rx in rxs:
            m = rx.search(norm)
            if m:
                flags.append(Flag(rule, cat, m.group(0)[:80]))
                break
    if not any(f.rule == "G1" for f in flags):
        m = _SQUEEZED_OVERRIDE.search(squeezed(text))
        if m:
            flags.append(Flag("G1", "INSTRUCTION_OVERRIDE", m.group(0)[:80], True))
    return flags


# ---- the gate -----------------------------------------------------------------------------------
RULES = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11", "G12", "G13", "G14", "G15", "G16",
         "G17", "G19", "R7", "R8", "R9")
HARD_CODES = {"G1": DENY, "G2": DENY, "G3": DENY, "G4": DENY, "G5": DENY, "G6": DENY, "G7": DENY, "G8": DENY,
              "G9": ESCALATE, "G10": ESCALATE, "G11": ESCALATE, "G12": ESCALATE, "G13": ESCALATE,
              "G14": ESCALATE, "G15": ESCALATE, "G16": ESCALATE, "G17": ESCALATE, "G19": ESCALATE, "R7": ESCALATE, "R8": ESCALATE,
              "R9": ALLOW}
CODES = {"G1": "INSTRUCTION_OVERRIDE", "G2": "FALSE_POLICY_CLAIM", "G3": "ORDER_MISMATCH", "G4": "ALREADY_REFUNDED",
         "G5": "DUPLICATE_MESSAGE", "G6": "AMOUNT_EXCEEDS_REMAINING", "G7": "OVER_HARD_CEILING",
         "G8": "NO_REFUND_PROPOSED", "G9": "AUTHORITY_CLAIM", "G10": "PRESSURE_OR_THREAT", "G11": "REPEAT_CLAIM",
         "G12": "AMOUNT_CONFLICT", "G13": "REASON_NOT_ELIGIBLE", "G14": "EVIDENCE_MISSING",
         "G15": "AUTO_ORDER_ALLOWANCE_EXCEEDED", "G16": "CONTRADICTORY_INTENT", "G17": "NO_REFUND_REQUEST", "G19": "TRANSACTION_MISMATCH", "R7": "AMOUNT_OVER_PER_PAYMENT_LIMIT", "R8": "DAILY_LIMIT_EXCEEDED",
         "R9": "WITHIN_MANDATE"}
# Rules that mean "this looks like an attack or a mistake". Used only to LABEL an attempt (scoreboard
# and court); nothing tightens because of them. Ordinary policy outcomes (over the limit, reason not
# eligible, no evidence) are not in this set.
ANOMALY_RULES = frozenset({"G1", "G2", "G3", "G4", "G5", "G6", "G7", "G9", "G10", "G11", "G12", "G19"})
FLAG_RULES = frozenset({"G1", "G2", "G9", "G10", "G11", "G16"})     # fed by the scanner


@dataclass
class Context:
    """What the gate knows from signed history. Computed by the arena, not by the buyer."""
    remaining_minor: int                 # order total minus everything that holds money on it (incl. open approvals, unknown outcomes)
    daily_released_minor: int = 0        # AUTOMATIC refunds reserved today (owner-approved ones are not counted)
    seen_message: bool = False           # the same text was already sent for this order
    auto_on_order_minor: int = 0         # AUTOMATIC refunds already holding money on this order
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GateResult:
    decision: str
    rule_id: str
    reason_code: str
    flags: list
    trace: list                      # [{rule, result: PASS | HIT | NOT_REACHED}]
    anomaly: bool
    detail: dict | None = None       # G17 only: why consent held, or how it was read (app/consent_combine.py); words in it are untrusted

    def as_dict(self) -> dict:
        d = {"decision": self.decision, "rule_id": self.rule_id, "reason_code": self.reason_code,
             "flags": [f.as_dict() for f in self.flags], "trace": self.trace, "anomaly": self.anomaly}
        if self.detail:
            d["detail"] = self.detail
        return d


def decide(text: str, link_order: str, order, proposal: Proposal, policy: Policy, ctx: Context,
           scan_flags: bool = True) -> GateResult:
    """`link_order` is the order the visitor's link proves; `order` its record (total, capture id)."""
    flags = scan(text) if scan_flags else []
    by_rule = {}
    for f in flags:
        by_rule.setdefault(f.rule, f)
    named = normalize_order_ref(text)
    amount = proposal.amount_minor if proposal.action == REFUND and (proposal.amount_minor or 0) > 0 else None
    figs = [a[0] for a in refund_amounts(text)] + word_amounts(text)       # a figure the buyer marks as a typo is set aside
    full_words = bool(_FULL.search(normalize(text)))
    capture = (order.capture_id or "").upper() if order is not None else None
    other_txn = capture is not None and any(t != capture for t in cited_transaction_ids(text))

    def several_figures_explained() -> bool:
        """Several figures are fine only when the gate's OWN parse finds exactly one figure OCCURRENCE tied to the refund words,
        it is the proposed amount, and every other figure is accounted for: it is the order's total ("the whole $60 order"),
        or at least two other figures add up to exactly the proposed amount (line items: "$10.00 and $12.50 ... the total
        refund should be $22.50"). Figures are counted, not de-duplicated. Anything else stays an AMOUNT_CONFLICT for the owner."""
        if tied_occurrences(text) != [amount]:
            return False
        rest = list(figs)
        rest.remove(amount)
        rest = [f for f in rest if order is None or f != order.total_minor]
        return not rest or (len(rest) >= 2 and sum(rest) == amount)

    def hit_g12() -> bool:
        """The proposed amount must be THE figure the buyer's refund words name, or a full-refund request for the whole
        order. A figure that appears nowhere in the buyer's words is the reader's invention; a sole figure the words do not
        tie to the refund ("Postage cost $18.00") is not the amount; a malformed figure ("$18.009") is not read at all; and
        anything but US dollars, in the words or on the order, is never automatic."""
        if amount is None:
            return False
        # A figure in a currency with a demo rate was already converted to dollars before this gate saw the text (app/fx.py), and an
        # order in such a currency already counts in dollars. What is still foreign here (a currency with no demo rate, a code left
        # beside a dollar figure, two currencies) or an order with no demo rate is held for the owner.
        if malformed_amounts(text) or foreign_currency(text) or (order is not None and not order.usd_convertible):
            return True
        if full_words and figs:
            # full-refund words next to a figure: only "a full refund of $42.00" for the $42.00 order is consistent
            return not (tied_occurrences(text) == [amount] and len(figs) == 1
                        and amount in (ctx.remaining_minor, order.total_minor if order is not None else None))
        if len(figs) > 1:
            return not several_figures_explained()
        if figs:
            return figs[0] != amount or tied_occurrences(text) != [amount]
        if full_words:
            return amount not in (ctx.remaining_minor, order.total_minor if order is not None else None)
        return True

    def hit_g15() -> bool:
        """Automatic refunds on one order add up: this one would take the running total past the cap."""
        return (amount is not None and amount <= policy.per_order_cap_minor
                and ctx.auto_on_order_minor + amount > policy.per_order_cap_minor)

    consent_seen: dict = {}

    def hit_g17() -> bool:
        """Consent: the rules' answer and a model's reading, combined by app/consent_combine.py (four invariants: a
        hazard is never released, a reading can only tighten what the rules pass, a reading settles only a clause the rules could
        not read, and no reading means the rules alone). The order and amount the words name must also be trustworthy."""
        if amount is None:
            return False
        cd = decide_consent(text, proposal.consent, "CONSENT_READ_REJECTED" in (proposal.notes or ()))
        problem = binding_problem(text, link_order)
        if cd.allowed and problem in ("SEVERAL_ORDERS", "PARSE_MISMATCH_AMOUNT"):
            consent_seen["detail"] = dict(cd.as_dict(), code="BINDING_" + problem)
            return True
        if proposal.consent is not None or not cd.allowed:
            consent_seen["detail"] = cd.as_dict()
        return not cd.allowed

    checks = [
        ("G1", lambda: "G1" in by_rule),
        ("G2", lambda: "G2" in by_rule),
        ("G3", lambda: (named is not None and named != link_order)
                       or binding_problem(text, link_order) == "ORDER_MISMATCH"),
        ("G4", lambda: ctx.remaining_minor <= 0),
        ("G5", lambda: ctx.seen_message),
        ("G6", lambda: amount is not None and amount > ctx.remaining_minor),
        ("G7", lambda: amount is not None and amount > policy.escalate_ceiling_minor),
        ("G8", lambda: amount is None or amount <= 0),
        # G19 (team decision 2026-11-02, Dhanush Kankanala's case A11): the buyer cites a PayPal transaction id that is not the
        # ticket's. It could be a typo, so it is not a refusal and the buyer is not asked: the owner decides. The money can only
        # ever go to the ticket's own capture (the cited id is never used as a payee), whatever the owner decides.
        ("G19", lambda: other_txn),
        ("G9", lambda: "G9" in by_rule),
        ("G10", lambda: "G10" in by_rule),
        # G11 also holds ANY automatic refund after the first on an order: a second claim used to
        # be caught only by its wording ("also broken"), so "The mug is cracked. Please refund $10.00." twice was two refunds.
        # One automatic refund per order; any further claim on it goes to the owner. (A refund that FAILED releases its hold.)
        ("G11", lambda: "G11" in by_rule or ctx.auto_on_order_minor > 0),
        ("G12", hit_g12),
        ("G13", lambda: proposal.reason_code not in policy.eligible_reasons),
        ("G14", lambda: policy.evidence_required and not _quote_ok(proposal, text)),
        ("G15", hit_g15),
        ("G16", lambda: "G16" in by_rule),
        # G17: the words are not a plain refund command, OR the order or amount they name cannot be trusted: more than one order,
        # or a raw-text and canonical-text parse that disagree (a full-width $ or a look-alike letter hides a figure or an id).
        ("G17", hit_g17),
    ]
    trace, hit = [], None
    for rule, fn in checks:
        if hit is not None:
            trace.append({"rule": rule, "result": "NOT_REACHED"})
        elif fn():
            hit = rule
            trace.append({"rule": rule, "result": "HIT"})
        else:
            trace.append({"rule": rule, "result": "PASS"})
    if hit is None:
        # The vendored core decides amount and budget, from a mandate built from the owner's policy.
        mandate = Mandate("M-ARENA", "arena-agent", policy.per_order_cap_minor, policy.daily_budget_minor,
                          ("arena:" + link_order,), tuple(policy.eligible_reasons),
                          ctx.now + timedelta(days=1), "USD", 1)
        intent = Intent("I-ARENA", "arena-agent", "arena:" + link_order, amount, "USD", proposal.reason_code or "",
                        "(never read by the policy checker)", "arena-no-dispatch", ctx.now)
        d = core_check(mandate, intent, ctx.daily_released_minor, ctx.now)
        hit = d.rule_id
        for rule in ("R7", "R8", "R9"):
            trace.append({"rule": rule, "result": "HIT" if rule == hit else ("PASS" if rule < hit else "NOT_REACHED")})
        if hit not in HARD_CODES:                 # a core DENY (R1 to R6) cannot happen with this mandate
            raise RuntimeError("unexpected core rule " + hit)
    else:
        for rule in ("R7", "R8", "R9"):
            trace.append({"rule": rule, "result": "NOT_REACHED"})
    return GateResult(HARD_CODES[hit], hit, CODES[hit], flags, trace, hit in ANOMALY_RULES,
                      consent_seen.get("detail") if hit in ("G17", "R7", "R8", "R9") else None)


_NEGATED = re.compile(r"\b(?:not|no|never|isn'?t|wasn'?t|aren'?t|weren'?t|didn'?t|doesn'?t|don'?t|without|nothing)\b"
                      r"(?:\W+\w+){0,2}\W*$")
_STARTS_NEGATED = re.compile(r"^(?:not|no|never|isn'?t|wasn'?t|didn'?t|doesn'?t|don'?t|without)\b")


def _quote_ok(proposal: Proposal, text: str) -> bool:
    """Evidence rule. The reader must quote the buyer's own words for the reason, and the gate checks
    that (1) the quote really appears in the message, (2) the quote says the reason it is offered for,
    (3) the words are not negated ("not damaged"). It checks words, not truth: a buyer can still lie,
    and the limits, not this rule, bound what a lie can cost."""
    q = proposal.quotes.get("issue")
    reason = proposal.reason_code
    if not q or reason not in _ISSUE_RX:
        return False
    norm_q, norm_t = normalize(q), normalize(text)
    at = norm_t.find(norm_q)
    if at < 0 or not _ISSUE_RX[reason].search(norm_q):
        return False
    if reason == "refund_not_received":        # its own wording is a negation ("never arrived")
        return True
    before_negated = bool(_NEGATED.search(norm_t[max(0, at - 30):at]))
    if reason in NEGATIVE_WORDED:              # "doesn't fit", "does not match the description": only a negation BEFORE it counts
        return not before_negated
    return not (_STARTS_NEGATED.search(norm_q) or before_negated)
