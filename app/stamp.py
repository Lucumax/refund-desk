"""The verdict stamp: one word, one rule, one reason, one consequence line, for every request.

This is presentation only. It reads facts that the signed records already hold (the stage, the route, the rule
id, the amounts) and turns them into the few sentences the first screen shows. It decides nothing and moves no
money. The page puts the stamp on top as the biggest thing on screen, and keeps what the AI suggested apart from
what the code decided. Every sentence comes from copy/strings.draft.json."""
from __future__ import annotations

from .money import fmt
from .strings import S

# Each stage the views can produce maps to exactly one stamp kind. A test walks this table so a new stage cannot
# slip through without a stamp.
KIND_BY_STAGE = {
    "SENT_DONE": "approved", "SENT_PENDING": "approved", "PROVIDER_FAILED": "failed",
    "PENDING_APPROVAL": "held", "PENDING": "held",
    "BLOCKED": "refused", "DENIED": "denied", "EXPIRED": "expired",
    "UNKNOWN": "unknown", "SUBMITTING": "unknown", "AUTHORIZED": "unknown",
    "FAILED": "not_sent",
    "NEEDS_INFO": "ask", "DUPLICATE": "duplicate",
    "NOT_SUBMITTED": "pending_check", "STATE_MISMATCH": "mismatch",
}
TONE = {"approved": "ok", "held": "warn", "refused": "bad", "denied": "bad", "expired": "warn", "unknown": "warn",
        "failed": "bad", "not_sent": "bad", "ask": "info", "duplicate": "info", "pending_check": "warn", "mismatch": "bad"}
KINDS = tuple(sorted(set(KIND_BY_STAGE.values())))
SENT_STAGES = ("SENT_DONE", "SENT_PENDING")


def _compare(kind: str, suggested_minor: int | None, allowed_minor: int | None, awaiting_owner: bool) -> str | None:
    """'The AI suggested X. Your rule let Y through.' Only when the reader actually proposed a sum."""
    if not suggested_minor or kind in ("failed", "not_sent", "mismatch", "pending_check"):
        return None
    sug = fmt(suggested_minor)
    if kind == "unknown":
        return S("stamp.compare_unknown", suggested=sug)
    if awaiting_owner:
        return S("stamp.compare_held", suggested=sug)
    return S("stamp.compare", suggested=sug, allowed=fmt(allowed_minor or 0))


def request_stamp(*, mode: str, stage: str, route: str | None, rule_id: str | None, reason: str | None,
                  amount_minor: int | None, suggested_minor: int | None, stale: bool = False) -> dict:
    """The stamp for one desk or inbox request. `mode` is 'fixture' or the sandbox; `route` is the authority's
    route (AUTO, REVIEW, BLOCKED) when a decision was recorded."""
    kind = KIND_BY_STAGE[stage]
    sim = mode == "fixture"
    amount = fmt(amount_minor) if amount_minor else None
    by_owner = route == "REVIEW"
    word = {"approved": "approved_owner" if by_owner else "approved", "denied": "denied_owner"}.get(kind, kind)
    if kind == "approved":
        pending = stage == "SENT_PENDING"
        out = S("stamp.out.sent_" + ("pending_" if pending else "") + ("sim" if sim else "sandbox"), amount=amount)
        reason_line = reason or S("stamp.reason.approved")
        allowed = amount_minor
    elif kind == "held":
        out = S("stamp.out.held_stale") if stale else S("stamp.out.held", amount=amount)
        reason_line, allowed = reason, 0
    elif kind == "refused":
        out, reason_line, allowed = S("stamp.out.nothing"), reason, 0
    elif kind == "denied":
        out, reason_line, allowed = S("stamp.out.nothing"), S("stamp.reason.denied"), 0
    elif kind == "expired":
        out, reason_line, allowed = S("stamp.out.nothing"), S("stamp.reason.expired"), 0
    elif kind == "unknown":
        out, reason_line, allowed = S("stamp.out.unknown", amount=amount), S("stamp.reason.unknown"), None
    elif kind == "failed":
        out, reason_line, allowed = S("stamp.out.failed"), S("stamp.reason.failed"), 0
    elif kind == "not_sent":
        out, reason_line, allowed = S("stamp.out.not_sent"), S("stamp.reason.not_sent"), 0
    elif kind == "ask":
        out, reason_line, allowed = S("stamp.out.ask"), reason, 0
    elif kind == "duplicate":
        out, reason_line, allowed = S("stamp.out.nothing_short"), reason, 0
    elif kind == "pending_check":
        out, reason_line, allowed = S("stamp.out.pending_check"), reason, 0
    else:                                                   # mismatch
        out, reason_line, allowed = S("stamp.out.mismatch"), reason, 0
    return {"kind": kind, "tone": TONE[kind], "word": S("stamp.word." + word),
            "rule_line": S("stamp.rule_line", rule=rule_id) if rule_id else None,
            "reason": reason_line, "consequence": out,
            "compare": _compare(kind, suggested_minor, allowed, awaiting_owner=(kind == "held")),
            "provenance": "simulated" if sim else "sandbox", "stage": stage}


# What the SIGNED execution record says happened to the money, whatever the gate decided.
MONEY_OUT = ("SUCCEEDED",)                                   # the provider accepted the refund
UNRESOLVED = ("UNKNOWN", "SUBMITTING", "AUTHORIZED")         # sent, or about to be, and not known to be finished
NOT_SENT = ("BLOCKED", "FAILED", "EXPIRED", "DENIED")        # the engine closed it without a completed send


def execution_view(rec: dict | None, provider_status: str | None) -> dict | None:
    """The only facts the arena stamp may use: the latest SIGNED receipt for the intent (its outcome and the amount it
    signed) and the folded signed provider status. None when no execution receipt exists."""
    if rec is None:
        return None
    return {"outcome": rec["outcome"], "provider_status": provider_status, "amount_minor": rec["intent"]["amount_minor"]}


def arena_kind(decision: str, execution: dict | None) -> str:
    """Stamp kind for one arena attempt, from the signed execution. The gate's decision is permission, not evidence of
    dispatch: it is only compared against the execution, and any disagreement is shown as a mismatch, never as a
    verdict. A decision that allowed a send with no execution record is a mismatch too (a crash between the two)."""
    outcome = execution["outcome"] if execution else None
    pstatus = execution["provider_status"] if execution else None
    if outcome in MONEY_OUT:
        if decision != "ALLOW":
            return "mismatch"                                   # money went out under a decision that allowed none
        return "failed" if pstatus in ("FAILED", "CANCELLED") else "approved"
    if outcome in UNRESOLVED:
        return "unknown" if decision == "ALLOW" else "mismatch"
    if decision == "ALLOW":
        if outcome is None:
            return "mismatch"                                   # allowed, but nothing signed shows what happened
        return "held" if outcome == "PENDING" else "not_sent"
    if decision == "ESCALATE":
        return "held"                                           # nothing went out; the arena closes it (no owner online)
    return "refused"


def arena_stamp(*, decision: str, rule_id: str, reason: str, execution: dict | None, suggested_minor: int | None) -> dict:
    """The stamp for one Injection Arena attempt. The arena always uses the simulated PayPal, and no owner is
    online, so a held request closes on the spot. The word and the money line come from the signed `execution`
    (see `execution_view`), never from the gate's decision."""
    kind = arena_kind(decision, execution)
    sent_minor = execution["amount_minor"] if execution and kind in ("approved", "unknown", "failed") else 0
    if kind == "approved":
        out = S("stamp.arena.sent", amount=fmt(sent_minor))
    elif kind == "unknown":
        out = S("stamp.out.unknown", amount=fmt(sent_minor))
    elif kind == "failed":
        out = S("stamp.out.failed")
    elif kind == "not_sent":
        out = S("stamp.out.not_sent")
    elif kind == "mismatch":
        out = S("stamp.out.mismatch")
    elif kind == "held":
        out = S("stamp.arena.held")
    else:
        out = S("stamp.arena.nothing")
    word = {"held": "arena_held"}.get(kind, kind)
    return {"kind": kind, "tone": TONE[kind], "word": S("stamp.word." + word),
            "rule_line": S("stamp.rule_line", rule=rule_id), "reason": reason, "consequence": out,
            "compare": _compare(kind, suggested_minor, sent_minor, awaiting_owner=False),
            "provenance": "simulated", "stage": execution["outcome"] if execution else decision}
