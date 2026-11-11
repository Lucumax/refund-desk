"""The core engine with ONE application-level rule: permission to send again is separate from the hold on
possibly-sent money.

The vendored core rechecks policy, terms and execution validity right before every dispatch and, when the
check fails, writes BLOCKED or EXPIRED. Both stop counting against the order balance and the budgets. That
is right for a payment that was never sent. It is wrong for an UNKNOWN payment, where a first send may
already have been accepted by PayPal: revoking permission to send AGAIN (a new policy version, a changed
eligible reason, an expired execution window) must not release the hold.

So for an UNKNOWN record this engine:
- ignores `execute_by`. That window belongs to the first dispatch. After the first dispatch the only time
  limit on a same-key re-send is the replay window, counted from the first dispatch (PayPalAdapter.lookup);
- if the hard-block check or the approval terms no longer pass, refuses the re-send and writes a signed
  `resend_refused` receipt whose outcome is still UNKNOWN, so the hold stays.

Two more application-level rules:

- An AUTOMATIC authorization has no approval bound to its terms, and the core's pre-dispatch check stops only a DENY.
  So an automatic refund whose amount now needs the owner (a lowered per-refund cap, a spent daily budget) used to go
  out anyway if it had been parked before its first send. Here the same `check` that decided ALLOW runs again right
  before the send, with the refund itself left out of the day's total, and anything but ALLOW writes BLOCKED.
- `recover()` never sends an AUTHORIZED record by itself. Each one is handed to `recovery_gate` (the refund authority,
  app/authority.py), which judges it again under the policy in force now and sends it only if the answer is still
  yes. With no gate wired, nothing is sent: recovery is never a second path around the authority.

Two more hooks, both wired by the service to the refund authority:

- `policy_guard` runs right before EVERY dispatch (first send or re-send): if the policy version in force is no longer the
  one the mandates were built from, nothing is sent (BLOCKED, or for an UNKNOWN refund a refused re-send that keeps the
  hold). The authority also holds the policy chain's lock from the policy read to the send, so this is the second line
  of defence, for a save the lock does not cover.
- `resend_authority` decides every same-key re-send of an UNKNOWN refund (see RefundAuthority.resend_authorized). With none
  wired, nothing is re-sent.

The vendored core is not modified (its manifest still checks); this subclass overrides private hooks, the same ones
the core calls. The change that would belong in the core is
a proposal for the core, not part of this repository: a retry of an UNKNOWN or SUBMITTING payment must never record
EXPIRED, only reconcile."""
from __future__ import annotations
from datetime import datetime
from typing import Callable

from approval_receipt import Engine, Intent, Decision, DENY, ALLOW, INDETERMINATE, LedgerIntegrityError
from approval_receipt.engine import UNKNOWN, SUBMITTING, AUTHORIZED, RESERVING, terms_digest
from approval_receipt.policy import check


POLICY_RULE = "G18"      # the same id the authority uses when something moved between the decision and the send


class HoldingEngine(Engine):
    # Set by the service to RefundAuthority.recover_authorized. Called with an AUTHORIZED record that never reached
    # SUBMITTING; returns the receipt it wrote, or None when the record was held (nothing sent, nothing closed).
    recovery_gate: Callable[[dict], dict | None] | None = None
    # Set by the service to RefundAuthority.resend_authorized. Called with an UNKNOWN record; sends it again, or writes a
    # signed `resend_refused` receipt that keeps it UNKNOWN. Returns the receipt now holding the intent.
    resend_authority: Callable[[dict], dict | None] | None = None
    # Set by the service to RefundAuthority.policy_guard: None while the policy version is unchanged, else a reason code.
    policy_guard: Callable[[], str | None] | None = None
    held_in_recovery: list

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.held_in_recovery = []       # [(intent id, reason code)] from the last recover(): left AUTHORIZED, not sent

    def _reserved_without(self, agent_id: str, now: datetime, intent_id: str) -> int:
        """The core's own daily total (`_reserved`), leaving one intent out so it is not counted against itself."""
        latest, auth_at = {}, {}
        for r in self.ledger.receipts():
            latest[r["intent"]["id"]] = r
            if r["outcome"] == AUTHORIZED:
                auth_at[r["intent"]["id"]] = datetime.fromisoformat(r["created_at"])
        today = self._day(now)
        return sum(r["intent"]["amount_minor"] for i, r in latest.items()
                   if i != intent_id and r["outcome"] in RESERVING and r["intent"]["agent_id"] == agent_id
                   and self._day(auth_at[i]) == today)

    def _pre_dispatch(self, rec: dict) -> dict | None:
        moved = self.policy_guard() if self.policy_guard is not None else None      # the version check, right before the send
        if rec["outcome"] != UNKNOWN:
            if moved is not None:
                return self._follow(rec, "BLOCKED", "blocked_at_execution", decision=Decision(DENY, POLICY_RULE, moved).as_dict())
            stop = super()._pre_dispatch(rec)
            if stop is not None or rec.get("intent_digest"):
                return stop         # (an owner-approved record is bound to its terms by the digest the core checked)
            intent = Intent.from_snapshot(rec["intent"])
            now = self.clock()
            d = check(self.mandates.get(intent.agent_id), intent,
                      self._reserved_without(intent.agent_id, now, intent.id), now)
            if d.decision != ALLOW:
                return self._follow(rec, "BLOCKED", "blocked_at_execution",
                                    decision=Decision(DENY, d.rule_id, d.reason_code).as_dict())
            return None
        intent = Intent.from_snapshot(rec["intent"])
        mandate = self.mandates.get(intent.agent_id)
        why = moved
        d = check(mandate, intent, 0, self.clock())
        if why is not None:
            pass
        elif d.decision == DENY:
            why = d.reason_code
        elif rec.get("intent_digest") and terms_digest(intent, mandate) != rec["intent_digest"]:
            why = "TERMS_CHANGED"
        if why is None:
            return None
        return self._follow(rec, UNKNOWN, "resend_refused", resend_refused={"reason": why})

    def recover(self) -> list[dict]:
        """Crash recovery. AUTHORIZED records that never reached SUBMITTING are judged again by the recovery gate (the
        refund authority) and sent only if it says so; SUBMITTING and UNKNOWN records are only reconciled, never
        blindly re-dispatched. A record the gate cannot judge (a signed chain does not verify) stays AUTHORIZED:
        held, not sent and not closed, listed in `held_in_recovery`."""
        with self._lock:
            self._guard()
            latest: dict[str, dict] = {}
            for r in self.ledger.receipts():
                latest[r["intent"]["id"]] = r
            out, self.held_in_recovery = [], []
            for iid, rec in latest.items():
                if rec["outcome"] == AUTHORIZED:
                    if self.recovery_gate is None:
                        self.held_in_recovery.append((iid, "NO_RECOVERY_GATE"))
                        continue
                    try:
                        new = self.recovery_gate(rec)
                    except LedgerIntegrityError as e:
                        self.held_in_recovery.append((iid, e.reason_code))
                        continue
                    if new is not None:
                        out.append(new)
                elif rec["outcome"] in (SUBMITTING, UNKNOWN):
                    new, _ = self._reconcile(iid)
                    if new is not None and new["id"] != rec["id"]:
                        out.append(new)
            return out

    def _reconcile(self, intent_id: str):
        """The core turns a SUBMITTING record into UNKNOWN when the adapter answers ABSENT. This build's adapter never
        says ABSENT (it cannot search PayPal), so the same conversion is done here for 'no refund id known, still
        inside the replay window': a send with no answer IS an unknown outcome."""
        with self._lock:
            rec, status = super()._reconcile(intent_id)
            basis = getattr(self.adapter, "lookup_basis", None)
            if (rec is not None and rec["outcome"] == SUBMITTING and status == INDETERMINATE and basis is not None
                    and basis(rec["intent"]["idempotency_key"]) == "NO_KNOWN_ID"):
                rec = self._follow(rec, UNKNOWN, "reconciled")
            return rec, status

    def resend_unknown(self, intent_id: str) -> dict:
        """Send an UNKNOWN payment again with the SAME key. The caller (Service.retry) decides whether a re-send is
        allowed in principle; the refund authority then judges THIS refund under the policy and signed records in force
        now (`resend_authority`), and only it reaches the engine's own pre-dispatch check and the dispatch."""
        with self._lock:
            self._guard()
            rec = self.ledger.latest_for_intent(intent_id)
            if rec is None or rec["outcome"] != UNKNOWN:
                return rec
            if self.resend_authority is None:
                return self._follow(rec, UNKNOWN, "resend_refused", resend_refused={"reason": "NO_RESEND_AUTHORITY"})
            return self.resend_authority(rec)
