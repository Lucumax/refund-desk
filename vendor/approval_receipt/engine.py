"""Intent handling, approval flow, execution states, injection flagging.

Every state change appends one receipt. Execution states for a payment:
PENDING_APPROVAL -> AUTHORIZED -> SUBMITTING -> SUCCEEDED | FAILED | UNKNOWN.
Other terminal outcomes: BLOCKED, DENIED, EXPIRED. AUTHORIZED is persisted before
dispatch, reserves the spend against the daily limit, and carries execute_by (execution
validity) which is rechecked before every dispatch. Every operation runs under the ledger's
cross-process lock and first verifies the chain and trusted checkpoint (fail closed).
"""
from __future__ import annotations
import hmac, re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from .models import Mandate, Intent, Decision, ApproverSession, ALLOW, ESCALATE, DENY
from .policy import check
from .adapters import (PaymentAdapter, PaymentTimeout, normalize_lookup, is_definitive_failure,
                       FOUND, ABSENT, INDETERMINATE)
from .errors import ApprovalError, LedgerIntegrityError
from .receipts import Ledger, digest

PENDING, AUTHORIZED, SUBMITTING = "PENDING_APPROVAL", "AUTHORIZED", "SUBMITTING"
SUCCEEDED, FAILED, UNKNOWN = "SUCCEEDED", "FAILED", "UNKNOWN"
BLOCKED, DENIED, EXPIRED = "BLOCKED", "DENIED", "EXPIRED"
RESERVING = {AUTHORIZED, SUBMITTING, SUCCEEDED, UNKNOWN}  # count against the daily limit

# Flagging only, a small list of known phrases. Not comprehensive detection, and it
# never feeds the decision.
_INJ = re.compile("|".join([
    r"ignore (all |your |previous |the )*(rules|instructions|polic)",
    r"disregard (all |your |previous |the )*(rules|instructions|polic)",
    r"pay anyway", r"override (the )?(limit|policy|rules|mandate)",
    r"you are now", r"system prompt", r"approve (this|it) (immediately|now)",
    r"bypass", r"do not (log|tell|flag)",
]), re.IGNORECASE)


def injection_flags(text: str) -> list[str]:
    return ["INJECTION_SUSPECTED"] if _INJ.search(text or "") else []


class IdempotencyConflict(Exception):
    """Same idempotency key reused with a different intent payload, or an intent id reused
    under a different key."""


@dataclass(frozen=True)
class Result:
    decision: Decision
    receipt: dict
    replayed: bool = False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def terms_digest(intent: Intent, mandate: Mandate | None) -> str:
    """Immutable terms an approval binds to."""
    return digest({"payee": intent.payee, "amount_minor": intent.amount_minor,
                   "currency": intent.currency, "purpose_code": intent.purpose_code,
                   "mandate_id": mandate and mandate.id,
                   "mandate_version": mandate and mandate.version,
                   "mandate_expires_at": mandate and mandate.expires_at.isoformat()})


class Engine:
    def __init__(self, mandates: list[Mandate], ledger: Ledger, adapter: PaymentAdapter,
                 clock=utcnow, approval_ttl: timedelta = timedelta(hours=24),
                 day_tz: tzinfo = timezone.utc, execution_ttl: timedelta = timedelta(minutes=15),
                 require_checkpoint: bool = True) -> None:
        if len({m.agent_id for m in mandates}) != len(mandates):
            raise ValueError("more than one mandate for the same agent_id")
        self.mandates = {m.agent_id: m for m in mandates}
        self.ledger, self.adapter = ledger, adapter
        self.clock, self.approval_ttl, self.day_tz = clock, approval_ttl, day_tz
        self.execution_ttl, self.require_checkpoint = execution_ttl, require_checkpoint
        self._lock = ledger.lock  # cross-process, re-entrant; decide+reserve+dispatch under it

    def _guard(self) -> None:
        """Fail closed BEFORE any side effect: chain, signatures and trusted checkpoint."""
        res = self.ledger.verify_trusted() if self.require_checkpoint else self.ledger.verify()
        if not res.ok:
            raise LedgerIntegrityError(res.reason_code, res.first_broken_id)

    def _execute_by(self, mandate: Mandate | None, now: datetime) -> str:
        limit = now + self.execution_ttl
        if mandate is not None:
            limit = min(limit, mandate.expires_at)
        return limit.isoformat()

    # ---- intents -------------------------------------------------------
    def submit(self, intent: Intent) -> Result:
        with self._lock:
            self._guard()
            prior = self.ledger.latest_for_key(intent.idempotency_key)
            if prior is not None:
                prior_intent = Intent.from_snapshot(prior["intent"])
                if prior_intent.payload() != intent.payload():
                    raise IdempotencyConflict(intent.idempotency_key)
                return Result(Decision(**prior["decision"]), prior, replayed=True)
            if self.ledger.latest_for_intent(intent.id) is not None:
                raise IdempotencyConflict(intent.id)  # intent id reused under a new key

            now = self.clock()
            mandate = self.mandates.get(intent.agent_id)
            decision = check(mandate, intent, self._reserved(intent.agent_id, now), now)
            base = dict(intent=intent.snapshot(), decision=decision.as_dict(),
                        flags=injection_flags(intent.reason_text))
            if decision.decision == ALLOW:
                rec = self._write(base, AUTHORIZED, "intent_decided",
                                  execute_by=self._execute_by(mandate, now))
                rec = self._execute(rec)
            elif decision.decision == ESCALATE:
                d = terms_digest(intent, mandate)
                rec = self._write(base, PENDING, "intent_decided", intent_digest=d,
                                  approval_nonce=self.ledger.mac("nonce:" + intent.id + ":" + d)[:32],
                                  approval_expires_at=(now + self.approval_ttl).isoformat())
            else:
                rec = self._write(base, BLOCKED, "intent_decided")
            return Result(decision, rec)

    def _day(self, dt: datetime):
        return dt.astimezone(self.day_tz).date()

    def _reserved(self, agent_id: str, now: datetime) -> int:
        """Spend reserved today (day in day_tz, default UTC, dated at authorization)."""
        latest, auth_at = {}, {}
        for r in self.ledger.receipts():
            latest[r["intent"]["id"]] = r
            if r["outcome"] == AUTHORIZED:
                auth_at[r["intent"]["id"]] = datetime.fromisoformat(r["created_at"])
        today = self._day(now)
        return sum(r["intent"]["amount_minor"] for i, r in latest.items()
                   if r["outcome"] in RESERVING and r["intent"]["agent_id"] == agent_id
                   and self._day(auth_at[i]) == today)

    def _pre_dispatch(self, rec: dict) -> dict | None:
        """Policy, terms and execution-validity recheck right before any dispatch. Returns a
        terminal receipt when the payment must NOT be dispatched, else None."""
        now = self.clock()
        intent = Intent.from_snapshot(rec["intent"])
        mandate = self.mandates.get(intent.agent_id)
        d = check(mandate, intent, 0, now)  # only hard blocks matter here
        if d.decision == DENY:
            return self._follow(rec, BLOCKED, "blocked_at_execution", decision=d.as_dict())
        if rec.get("intent_digest") and terms_digest(intent, mandate) != rec["intent_digest"]:
            return self._follow(rec, BLOCKED, "blocked_at_execution")
        ex = rec.get("execute_by")
        if ex and now >= datetime.fromisoformat(ex):
            return self._follow(rec, EXPIRED, "authorization_expired")
        return None

    def _execute(self, authorized: dict) -> dict:
        """Authorized (or confirmed-absent UNKNOWN) record: recheck, persist SUBMITTING,
        dispatch, persist the result. A generic exception is UNKNOWN, never FAILED."""
        stop = self._pre_dispatch(authorized)
        if stop is not None:
            return stop
        rec = self._follow(authorized, SUBMITTING, "dispatch")
        i = rec["intent"]
        try:
            ref = self.adapter.pay(i["payee"], i["amount_minor"], i["currency"], i["idempotency_key"])
        except PaymentTimeout:
            return self._follow(rec, UNKNOWN, "dispatch_result")
        except Exception as e:
            if is_definitive_failure(e):
                return self._follow(rec, FAILED, "dispatch_result")
            return self._follow(rec, UNKNOWN, "dispatch_result")
        return self._follow(rec, SUCCEEDED, "dispatch_result", payment_ref=ref)

    # ---- reconcile (never mints a new idempotency key) -------------------
    def reconcile(self, intent_id: str) -> dict:
        """Ask the provider about a SUBMITTING or UNKNOWN payment, using the same key.
        A lookup that raises or is indeterminate leaves the record exactly as it is."""
        return self._reconcile(intent_id)[0]

    def _reconcile(self, intent_id: str) -> tuple[dict | None, str | None]:
        with self._lock:
            self._guard()
            rec = self.ledger.latest_for_intent(intent_id)
            if rec is None or rec["outcome"] not in (SUBMITTING, UNKNOWN):
                return rec, None
            try:
                raw = self.adapter.lookup(rec["intent"]["idempotency_key"])
                res = normalize_lookup(raw, getattr(self.adapter, "lookup_none_is_absent", False))
            except Exception:
                return rec, INDETERMINATE
            if res.status == FOUND:
                return self._follow(rec, SUCCEEDED, "reconciled", payment_ref=res.payment_ref), FOUND
            if res.status == ABSENT:
                if rec["outcome"] == SUBMITTING:
                    rec = self._follow(rec, UNKNOWN, "reconciled")
                return rec, ABSENT
            return rec, INDETERMINATE

    def retry(self, intent_id: str) -> dict:
        """Re-dispatch an UNKNOWN payment with the SAME key, only when the provider
        DEFINITIVELY reports no record of it. Any other answer leaves it UNKNOWN."""
        with self._lock:
            rec, status = self._reconcile(intent_id)
            if rec and rec["outcome"] == UNKNOWN and status == ABSENT:
                return self._execute(rec)
            return rec

    def recover(self) -> list[dict]:
        """Crash recovery. After the integrity guard: AUTHORIZED records that never reached
        SUBMITTING go through the same-key execution path (policy and validity rechecked;
        SUBMITTING is persisted first, so a second recover cannot dispatch again). SUBMITTING
        and UNKNOWN records are only reconciled, never blindly re-dispatched (use retry).
        Returns the new receipts written."""
        with self._lock:
            self._guard()
            latest: dict[str, dict] = {}
            for r in self.ledger.receipts():
                latest[r["intent"]["id"]] = r
            out = []
            for iid, rec in latest.items():
                if rec["outcome"] == AUTHORIZED:
                    out.append(self._execute(rec))
                elif rec["outcome"] in (SUBMITTING, UNKNOWN):
                    new, _ = self._reconcile(iid)
                    if new is not None and new["id"] != rec["id"]:
                        out.append(new)
            return out

    # ---- approvals -----------------------------------------------------
    def pending_approvals(self) -> list[dict]:
        latest: dict[str, dict] = {}
        for r in self.ledger.receipts():
            latest[r["intent"]["id"]] = r
        return [r for r in latest.values() if r["outcome"] == PENDING]

    def approve(self, intent_id: str, session: ApproverSession, nonce: str,
                intent_digest: str, note: str = "") -> dict:
        return self._resolve(intent_id, "approve", session, nonce, intent_digest, note)

    def deny(self, intent_id: str, session: ApproverSession, note: str = "") -> dict:
        return self._resolve(intent_id, "deny", session, "", "", note)

    def expire_due(self) -> list[dict]:
        with self._lock:
            self._guard()
            now = self.clock()
            return [self._follow(r, EXPIRED, "approval_resolved") for r in self.pending_approvals()
                    if now >= datetime.fromisoformat(r["approval_expires_at"])]

    def _resolve(self, intent_id, action, session, nonce, claimed_digest, note) -> dict:
        if not isinstance(session, ApproverSession) or not session.approver_id:
            raise ApprovalError("SESSION_REQUIRED")
        with self._lock:
            self._guard()
            rec = self.ledger.latest_for_intent(intent_id)
            if rec is None:
                raise ApprovalError("INTENT_NOT_FOUND")
            if rec["outcome"] != PENDING:
                a = rec["approver"]
                if a and a["action"] == action and a["id"] == session.approver_id:
                    return rec  # retry: same receipt back, nothing executes again
                raise ApprovalError("NOT_PENDING" if rec["decision"]["decision"] != ESCALATE
                                    else "ALREADY_RESOLVED")
            intent = Intent.from_snapshot(rec["intent"])
            if session.approver_id == intent.agent_id:
                raise ApprovalError("APPROVER_IS_AGENT")
            now = self.clock()
            if now >= datetime.fromisoformat(rec["approval_expires_at"]):
                self._follow(rec, EXPIRED, "approval_resolved")
                raise ApprovalError("APPROVAL_EXPIRED")
            approver = {"id": session.approver_id, "session_id": session.session_id,
                        "note": note, "action": action}
            if action == "deny":
                return self._follow(rec, DENIED, "approval_resolved", approver=approver)
            if not hmac.compare_digest(nonce or "", rec["approval_nonce"]):
                raise ApprovalError("NONCE_INVALID")
            mandate = self.mandates.get(intent.agent_id)
            current = terms_digest(intent, mandate)
            if claimed_digest != rec["intent_digest"] or current != rec["intent_digest"]:
                raise ApprovalError("TERMS_CHANGED")
            # Recheck hard blocks (and mandate expiry) right before execution.
            recheck = check(mandate, intent, self._reserved(intent.agent_id, now), now)
            if recheck.decision == DENY:
                self._follow(rec, BLOCKED, "approval_resolved",
                             approver={**approver, "action": "approve_blocked"},
                             decision=recheck.as_dict())
                raise ApprovalError("HARD_BLOCK_AT_EXECUTION")
            auth = self._follow(rec, AUTHORIZED, "approval_resolved", approver=approver,
                                execute_by=self._execute_by(mandate, now))
            return self._execute(auth)

    # ---- receipts ------------------------------------------------------
    def _write(self, base: dict, outcome: str, event: str, **over) -> dict:
        body = {"intent_digest": None, "approval_nonce": None, "approval_expires_at": None,
                "approver": None, "payment_ref": None, "execute_by": None, "flags": [],
                **base, **over}
        body.update(event=event, outcome=outcome, created_at=self.clock().isoformat())
        return self.ledger.append(body)

    def _follow(self, prev: dict, outcome: str, event: str, **over) -> dict:
        """Next receipt for the same intent. The nonce is dropped, so it is single-use."""
        base = {k: prev[k] for k in ("intent", "decision", "approver", "flags", "intent_digest",
                                     "payment_ref")}
        base["execute_by"] = prev.get("execute_by")
        return self._write({**base, **over}, outcome, event)
