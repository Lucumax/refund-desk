"""Policy Builder: a plain-language rule box compiled by code into a CLOSED rule schema.

No free-form code runs and no model writes a rule. The compiler knows a small set of phrases.
Each clause is searched for known pieces (an amount per order, a daily budget, evidence, reasons);
every piece found is cut out, and whatever is left must be filler words from a fixed list. Any other
word means the clause is rejected, with a help message, and nothing is saved.

Fields (the whole schema; exact meaning in app/policy_store.py and the README):
  per_order_cap_minor     AUTOMATIC allowance per order: all automatic refunds on one order add up to at most this
  escalate_ceiling_minor  the most the owner may approve in ONE refund; above it, DENY
  daily_budget_minor      the most AUTOMATIC refunds may add up to in one UTC day (owner-approved refunds are outside it)
  evidence_required       the buyer's own words must support the reason
  eligible_reasons        reasons that may be refunded automatically (others go to the owner)

Patch semantics: a rule text changes only the fields it names; the rest stay as they were."""
from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict

from approval_receipt.receipts import digest

from .extractor import PURPOSES
from .money import to_minor

MAX_TEXT = 600
CAP_RANGE = (100, 50_000)             # $1.00 to $500.00
CEILING_RANGE = (100, 100_000)        # up to $1,000.00
DAILY_RANGE = (100, 500_000)          # up to $5,000.00
FIELDS = ("per_order_cap_minor", "escalate_ceiling_minor", "daily_budget_minor", "evidence_required",
          "eligible_reasons")


@dataclass(frozen=True)
class Policy:
    per_order_cap_minor: int = 2_500
    escalate_ceiling_minor: int = 20_000
    daily_budget_minor: int = 10_000
    evidence_required: bool = True
    eligible_reasons: tuple = tuple(PURPOSES)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["eligible_reasons"] = sorted(self.eligible_reasons)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        p = cls(int(d["per_order_cap_minor"]), int(d["escalate_ceiling_minor"]), int(d["daily_budget_minor"]),
                bool(d["evidence_required"]), tuple(sorted(d["eligible_reasons"])))
        errs = validate(p)
        if errs:
            raise ValueError("BAD_POLICY:" + errs[0])
        return p

    def hash(self) -> str:
        return digest(self.as_dict())


DEFAULT = Policy()


def validate(p: Policy) -> list[str]:
    """Machine codes for anything outside the closed schema."""
    errs = []
    if not (CAP_RANGE[0] <= p.per_order_cap_minor <= CAP_RANGE[1]):
        errs.append("CAP_OUT_OF_RANGE")
    if not (CEILING_RANGE[0] <= p.escalate_ceiling_minor <= CEILING_RANGE[1]):
        errs.append("CEILING_OUT_OF_RANGE")
    if not (DAILY_RANGE[0] <= p.daily_budget_minor <= DAILY_RANGE[1]):
        errs.append("DAILY_OUT_OF_RANGE")
    if p.per_order_cap_minor > p.escalate_ceiling_minor:
        errs.append("CAP_ABOVE_CEILING")
    if p.per_order_cap_minor > p.daily_budget_minor:
        errs.append("CAP_ABOVE_DAILY")
    if not p.eligible_reasons or any(r not in PURPOSES for r in p.eligible_reasons):
        errs.append("REASONS_INVALID")
    return errs


# ---- the phrase book ------------------------------------------------------------------------
_AMT = r"(?:\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s?(?:dollars|usd))"
_REASON_WORDS = {
    "refund_damaged": r"damaged|broken|defective|cracked",
    "refund_not_received": r"never arrived|not received|not delivered|lost|missing(?! items?| parts?)",
    "refund_wrong_item": r"wrong items?|wrong product",
    "refund_duplicate_charge": r"charged twice|double[- ]charged|double charges?|duplicate charges?|duplicates?",
    "refund_not_as_described": r"not as described|wrong fit|wrong size",
    "refund_missing_item": r"missing items?|missing parts?",
    "refund_returned": r"returned items?|returns?",
}
_ANY_REASON = r"any reason|all reasons|every reason"
_RW = "(?:" + "|".join(_REASON_WORDS.values()) + "|" + _ANY_REASON + ")"
_SEP = r"(?:\s*,\s*(?:and |or )?|\s+and\s+|\s+or\s+)"

_PIECES = (
    # name, regex. Order matters: the daily budget is cut out before the per-order cap.
    ("daily", re.compile(
        r"(?:(?:daily|per[- ]day)\s*(?:budget|limit|cap|total|maximum)?\s*(?:of|is|at|to)?\s*(?P<a>" + _AMT + r")"
        r"|(?P<b>" + _AMT + r")\s*(?:per|a|each|every)\s*day"
        r"|(?:budget|limit|total|cap)\s*of\s*(?P<c>" + _AMT + r")\s*(?:per|a|each|every)\s*day)")),
    ("ceiling", re.compile(
        r"(?:(?:never|do not|don't)\s+(?:refund|pay)\s*(?:more than|over|above)?\s*(?P<a>" + _AMT + r")"
        r"|(?:deny|refuse|block|reject)\s+(?:anything|refunds?|requests?|amounts?)?\s*(?:over|above|more than)\s*(?P<b>" + _AMT + r")"
        r"|hard\s+(?:limit|ceiling|max(?:imum)?)\s*(?:of|is|at)?\s*(?P<c>" + _AMT + r"))")),
    ("ask_over", re.compile(
        r"(?:ask me|escalate|send (?:it |them )?to me|need my approval|require my approval|check with me)\s*"
        r"(?:for|about|when|if)?\s*(?:anything|refunds?|requests?|amounts?)?\s*(?:over|above|more than)\s*(?P<a>" + _AMT + r")")),
    ("cap", re.compile(
        r"(?:up to|at most|no more than|a max(?:imum)? of|max(?:imum)?(?: of)?|limited to|capped at|cap of|cap)\s*(?P<a>" + _AMT + r")"
        r"(?:\s*(?:per|each|for each|a)\s*(?P<scope>order|refund|request))?")),
    ("evidence_off", re.compile(
        r"(?:(?:no|do not require|don't require|do not need|don't need|without)\s+(?:quoted\s+)?(?:evidence|quotes?|proof))")),
    ("evidence_on", re.compile(
        r"(?:(?:always\s+)?(?:require|requires|need|needs|must have)\s+(?:a\s+)?(?:quoted\s+)?(?:evidence|quote|proof)"
        r"(?:\s+from the buyer)?)")),
    ("reasons", re.compile(
        r"(?:only\s+)?(?:for|when(?:\s+the)?\s+reason\s+is|if(?:\s+the)?\s+reason\s+is|reason\s+must\s+be)\s+"
        r"(?P<list>" + _RW + r"(?:" + _SEP + _RW + r")*)(?:\s+(?:items?|orders?|claims?|packages?|goods))?")),
    ("rest_to_me", re.compile(
        r"(?:everything|anything|all)\s+else\s+(?:goes\s+to\s+me|to\s+me|comes\s+to\s+me|needs\s+my\s+approval|for\s+me)")),
)
FILLER = frozenset("""refund refunds refunding please i we will would like to want automatically auto up the a an of is
are my shop store buyers customers orders order any and only each per for it them also then set keep make let
needed required necessary
""".split())

HELP_EXAMPLES = (
    "refund up to $30 per order",
    "daily budget of $120",
    "never refund more than $200",
    "ask me for anything over $30",
    "require quoted evidence",
    "refund only for damaged items and wrong item",
    "everything else to me",
)


def _split_clauses(text: str) -> list[str]:
    parts = re.split(r"(?<!\d)\.(?=\s|$)|[;\n]", text)
    return [p.strip(" \t,") for p in parts if p.strip(" \t,")]


def _minor(token: str) -> int:
    t = token.lower().replace("$", "").replace(",", "").replace("dollars", "").replace("usd", "").strip()
    return to_minor(t)


def _parse_reasons(listing: str) -> list[str] | None:
    if re.search(_ANY_REASON, listing):
        return list(PURPOSES)
    found = []
    for code, rx in _REASON_WORDS.items():
        if re.search(rx, listing):
            found.append(code)
    return found or None


def _clause_pieces(clause: str) -> tuple[dict, list[str]]:
    """Cut known pieces out of one clause. Returns (assignments, leftover words)."""
    work = clause.lower()
    work = re.sub(r"\s+", " ", work)
    out: dict = {}
    for name, rx in _PIECES:
        while True:
            m = rx.search(work)
            if not m:
                break
            g = {k: v for k, v in m.groupdict().items() if v}
            if g.pop("scope", None) in ("refund", "request"):
                # "up to $30 per refund" is a different rule from the one this build enforces (an allowance per ORDER
                # that adds up across refunds). It is refused, not quietly read as something else.
                out.setdefault("_per_refund", set()).add(True)
            if name in ("daily", "ceiling", "ask_over", "cap"):
                amount = _minor(next(iter(g.values())))
                key = {"daily": "daily_budget_minor", "ceiling": "escalate_ceiling_minor",
                       "ask_over": "per_order_cap_minor", "cap": "per_order_cap_minor"}[name]
                out.setdefault(key, set()).add(amount)
            elif name == "evidence_on":
                out.setdefault("evidence_required", set()).add(True)
            elif name == "evidence_off":
                out.setdefault("evidence_required", set()).add(False)
            elif name == "reasons":
                rs = _parse_reasons(g["list"])
                out.setdefault("eligible_reasons", set()).add(tuple(sorted(rs or ())))
            elif name == "rest_to_me":
                out.setdefault("_rest", set()).add(True)
            work = work[:m.start()] + " " + work[m.end():]
    words = [w for w in (x.strip(".,;:!?") for x in re.findall(r"[^\s,]+", work)) if w and w not in FILLER]
    return out, words


@dataclass
class CompileResult:
    ok: bool
    policy: Policy | None = None
    errors: list = field(default_factory=list)      # {clause, code, hint?}
    recognized: list = field(default_factory=list)  # clauses that compiled
    changes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "policy": self.policy.as_dict() if self.policy else None, "errors": self.errors,
                "recognized": self.recognized, "changes": self.changes}


def compile_text(text: str, base: Policy = DEFAULT) -> CompileResult:
    """Compile plain language to a Policy by patching `base`. Strict: one bad clause rejects all."""
    text = (text or "").strip()
    if not text:
        return CompileResult(False, errors=[{"clause": "", "code": "EMPTY"}])
    if len(text) > MAX_TEXT:
        return CompileResult(False, errors=[{"clause": "", "code": "TOO_LONG"}])
    errors, recognized = [], []
    assigned: dict[str, set] = {}
    for clause in _split_clauses(text):
        pieces, left = _clause_pieces(clause)
        if not pieces or left:
            # a clause like "A and B" may be two rules; try each side on its own
            sides = [s for s in re.split(r"\s+and\s+|,\s*", clause) if s.strip()]
            if len(sides) > 1 and not pieces:
                sub = [_clause_pieces(s) for s in sides]
                if all(p and not l for p, l in sub):
                    for p, _ in sub:
                        for k, v in p.items():
                            assigned.setdefault(k, set()).update(v)
                    recognized.append(clause)
                    continue
            errors.append({"clause": clause, "code": "UNKNOWN_PHRASE", "left": left[:6]})
            continue
        for k, v in pieces.items():
            assigned.setdefault(k, set()).update(v)
        recognized.append(clause)
    if "_per_refund" in assigned:
        errors.append({"clause": "", "code": "PER_REFUND_NOT_SUPPORTED"})
    for key, vals in assigned.items():
        if len(vals) > 1:
            errors.append({"clause": key, "code": "CONFLICT", "field": key})
    if errors:
        return CompileResult(False, errors=errors, recognized=recognized)
    fields = dict(per_order_cap_minor=base.per_order_cap_minor, escalate_ceiling_minor=base.escalate_ceiling_minor,
                  daily_budget_minor=base.daily_budget_minor, evidence_required=base.evidence_required,
                  eligible_reasons=base.eligible_reasons)
    for key, vals in assigned.items():
        if key in ("_rest", "_per_refund"):
            continue
        fields[key] = next(iter(vals))
    if all(k == "_rest" for k in assigned):
        # only 'everything else to me' was said: nothing to change, which is a no-op, not an error
        pass
    policy = Policy(**{**fields, "eligible_reasons": tuple(sorted(fields["eligible_reasons"]))})
    bad = validate(policy)
    if bad:
        return CompileResult(False, errors=[{"clause": "", "code": c} for c in bad], recognized=recognized)
    return CompileResult(True, policy, [], recognized, diff(base, policy))


def diff(a: Policy, b: Policy) -> list[dict]:
    """One row per field: before, after, changed. Values are plain data (cents, bool, sorted list)."""
    da, db = a.as_dict(), b.as_dict()
    rows = []
    for f in FIELDS:
        rows.append({"field": f, "before": da[f], "after": db[f], "changed": da[f] != db[f]})
    return rows
