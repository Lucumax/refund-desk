"""Safe provider-status transitions for a refund. Order comes from the status itself, never
from timestamps: a late-arriving old event cannot move a refund backward.

PENDING (1) may become COMPLETED, FAILED or CANCELLED (2). A terminal status never changes.
A different terminal status after a terminal one is a CONFLICT: kept for a person, not applied.
Status names are UNVERIFIED against PayPal's current docs."""
from __future__ import annotations
from dataclasses import dataclass

PENDING, COMPLETED, FAILED, CANCELLED = "PENDING", "COMPLETED", "FAILED", "CANCELLED"
RANK = {PENDING: 1, COMPLETED: 2, FAILED: 2, CANCELLED: 2}
TERMINAL = (COMPLETED, FAILED, CANCELLED)

APPLIED, NOOP, STALE, CONFLICT, UNKNOWN_STATUS = "APPLIED", "NOOP", "STALE", "CONFLICT", "UNKNOWN_STATUS"


@dataclass(frozen=True)
class Transition:
    new: str | None
    result: str


def advance(current: str | None, incoming: str | None) -> Transition:
    if incoming not in RANK:
        return Transition(current, UNKNOWN_STATUS)
    if current is None:
        return Transition(incoming, APPLIED)
    if current not in RANK:
        return Transition(incoming, APPLIED)
    if incoming == current:
        return Transition(current, NOOP)
    if current in TERMINAL:
        # terminal is final: a different terminal is a conflict, an earlier stage is stale
        return Transition(current, CONFLICT if incoming in TERMINAL else STALE)
    return Transition(incoming, APPLIED)   # PENDING to a terminal status


def derive(statuses) -> str | None:
    """Fold provider statuses, oldest first, through `advance`. Used to read the authoritative
    status from SIGNED receipts, so no mutable store field decides money accounting."""
    cur = None
    for s in statuses:
        cur = advance(cur, s).new
    return cur
