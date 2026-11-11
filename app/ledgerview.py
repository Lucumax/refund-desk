"""Read-only projections computed from the SIGNED receipt chain. Money accounting (what is held
on an order, what status PayPal reported) is derived here, never from the mutable state file."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone

from approval_receipt import Ledger
from approval_receipt.engine import PENDING, AUTHORIZED, SUBMITTING, SUCCEEDED, UNKNOWN, RESERVING

from .trusted import trusted_receipts
from .refund_status import derive, FAILED as P_FAILED, CANCELLED as P_CANCELLED

ACTIVE = {PENDING, AUTHORIZED, SUBMITTING, SUCCEEDED, UNKNOWN}   # outcomes that hold the order's money


@dataclass(frozen=True)
class IntentState:
    rec: dict                    # latest receipt for the intent
    provider_status: str | None  # folded from signed provider_status receipts
    history: tuple

    @property
    def holds_money(self) -> bool:
        if self.rec["outcome"] not in ACTIVE:
            return False
        return self.provider_status not in (P_FAILED, P_CANCELLED)


def intent_states(ledger: Ledger) -> dict[str, IntentState]:
    by: dict[str, list] = {}
    for r in trusted_receipts(ledger):
        by.setdefault(r["intent"]["id"], []).append(r)
    out = {}
    for iid, recs in by.items():
        st = derive(r.get("provider_status") for r in recs if r["event"] == "provider_status")
        out[iid] = IntentState(recs[-1], st, tuple(recs))
    return out


def held_on_capture(ledger: Ledger, capture_id: str, states: dict | None = None) -> tuple[int, str | None]:
    """Money committed on this capture, and the id of an unresolved intent if any (a person or
    PayPal has not finished). Derived only from signed receipts."""
    total, open_intent = 0, None
    for iid, st in (states if states is not None else intent_states(ledger)).items():
        rec = st.rec
        if rec["intent"]["payee"] != capture_id or not st.holds_money:
            continue
        total += rec["intent"]["amount_minor"]
        if rec["outcome"] != SUCCEEDED:
            open_intent = open_intent or iid
    return total, open_intent


def auto_reserved_on_capture(ledger: Ledger, capture_id: str, agent_id: str, states: dict | None = None) -> int:
    """What AUTOMATIC refunds (the given agent id) already hold on this capture: authorized, sending,
    accepted, or unknown. Refunds the owner approved run under another agent id and are not counted
    here. This is the per-order automatic allowance used so far."""
    total = 0
    for st in (states if states is not None else intent_states(ledger)).values():
        rec = st.rec
        if (rec["intent"]["payee"] == capture_id and rec["intent"]["agent_id"] == agent_id
                and rec["outcome"] in RESERVING and st.holds_money):
            total += rec["intent"]["amount_minor"]
    return total


def reserved_today(ledger: Ledger, agent_id: str, now: datetime, day_tz=timezone.utc, exclude: str | None = None) -> int:
    """Automatic spend reserved today (UTC day, dated when it was authorized). The same rule as the
    core engine's own daily check, kept here so the authority and the core cannot disagree. A refund
    that PayPal later marks FAILED still counts until a person resolves it: the conservative choice. `exclude` leaves
    one intent out, so a refund that is being judged again (recovery) is not counted against itself."""
    latest, auth_at = {}, {}
    for r in trusted_receipts(ledger):
        latest[r["intent"]["id"]] = r
        if r["outcome"] == AUTHORIZED:
            auth_at[r["intent"]["id"]] = datetime.fromisoformat(r["created_at"])
    today = now.astimezone(day_tz).date()
    return sum(r["intent"]["amount_minor"] for i, r in latest.items()
               if r["outcome"] in RESERVING and r["intent"]["agent_id"] == agent_id
               and i in auth_at and i != exclude and auth_at[i].astimezone(day_tz).date() == today)


def dispatch_count(ledger: Ledger, idempotency_key: str) -> int:
    """How many times this key was sent to the provider, counted from signed `dispatch` receipts.
    The engine writes the receipt before every send, so while a send is in flight the count already
    includes it: 1 means first send, 2 or more means a re-send."""
    return sum(1 for r in trusted_receipts(ledger)
               if r["event"] == "dispatch" and r["intent"]["idempotency_key"] == idempotency_key)
