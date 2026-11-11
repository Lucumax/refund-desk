"""The refund authority: the ONE place that decides whether a proposed refund may move money, and the
one place that hands an approved intent to the core engine and the provider adapter.

Both the refund inbox (real buyer messages, fake or sandbox PayPal) and the Injection Arena (stranger
text, fake PayPal only) call `RefundAuthority.authorize`. Only the provider adapter differs. Nothing
else in the application builds a refund call.

What it does, in order, under the ledger lock (one decision at a time, across threads and processes):

1. Reads the owner's ACTIVE policy version (app/policy_store.py) and makes the core engine's mandates
   match it (automatic mandate and review mandate, both versioned, so an approval card issued under an
   older policy can no longer be approved).
2. Builds the context from SIGNED receipts only: what is left on the order after everything that holds
   money (open approvals, sending, unknown outcomes, pending-at-PayPal refunds, accepted refunds), what
   automatic refunds already hold on the order, what automatic refunds are reserved today, and whether
   this exact text was already decided for this order.
3. Runs the gate (app/arena_gate.py) on the RAW text and the reader's proposal.
4. Routes:
     ALLOW    -> engine under the AUTOMATIC agent. Reserved before it is sent. Counts toward the daily
                 automatic budget and the order's automatic allowance.
     ESCALATE -> engine under the REVIEW agent (its limit is zero, so the engine always parks it for
                 the owner). Reserves the order's balance, not the daily automatic budget.
     DENY     -> a signed BLOCKED receipt and NO adapter call.
5. Writes a signed decision record (reader proposal, gate trace, flags, policy version and numbers)
   BEFORE anything is sent, so the reasoning survives a crash.

Binding. Every outward send, and every approve or deny, is tied to a SIGNED decision for that exact intent
id, idempotency key, amount AND purpose (the reason class on the signed proposal), and one decision allows one send. A
refused attempt is written as its own record and never takes the place of the decision for an operation already known. An unsigned request file (its id, its pointers, its
stored intent) only ever PROPOSES: a request id, key or intent id that a signed decision already ties to another intent
is a conflict, and a conflict is refused (a signed BLOCKED decision, nothing sent). The policy chain's lock is held from
the moment the policy is read until the send is made, and a last version check runs right before the dispatch.

Human-approved refunds: they run under the REVIEW agent, so they do NOT use up the daily automatic
budget. They are bounded by the order's remaining balance and the policy's hard ceiling per refund,
and each is an explicit decision by the owner. Stated here, in the README and on the desk screen.

The authority bounds what a wrong or hostile proposal can cost. It does not decide whether a buyer is
telling the truth."""
from __future__ import annotations
import hashlib
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from approval_receipt import Intent, Ledger, LedgerIntegrityError, Mandate, Decision, ALLOW, ESCALATE, DENY
from approval_receipt.engine import (Engine, IdempotencyConflict, BLOCKED, PENDING, SUCCEEDED, UNKNOWN,
                                     SUBMITTING, AUTHORIZED, injection_flags)

from .arena_gate import Context, GateResult, decide, scan, RULES
from .arena_policy import Policy
from .arena_reader import Proposal, REFUND
from .extractor import PURPOSES
from .ledgerview import (held_on_capture, auto_reserved_on_capture, reserved_today, intent_states)
from .orders import OrderBook
from .policy_store import PolicyStore
from .trusted import trusted_receipts

AUTO_AGENT = "refund-agent"
REVIEW_AGENT = "refund-review"
AUTO_MANDATE, REVIEW_MANDATE = "M-REFUND-AUTO", "M-REFUND-REVIEW"
MANDATE_END = datetime(2027, 12, 31, tzinfo=timezone.utc)
ROUTE_AUTO, ROUTE_REVIEW, ROUTE_BLOCKED = "AUTO", "REVIEW", "BLOCKED"
DECISION_EVENT = "authority_decision"
# Refusals that name an id or key that belongs to ANOTHER operation (never closed with a receipt of their own). Now
# EVERY refusal for an intent that a signed decision or a receipt already names is of this kind: an unsigned request file can
# neither add a record under that intent id nor close it, whatever the file claims to be (see `authorize`).
NO_RECEIPT_REFUSALS = frozenset({"INTENT_ID_REUSED", "KEY_REUSED", "INTENT_ID_IN_LEDGER"})
RECOVERY_RULE = "G18"      # raised only here, when recovery finds an approved refund can no longer go out; never by decide()


def build_mandates(policy: Policy, version: int, capture_ids: tuple) -> list[Mandate]:
    """The two mandates the engine needs, derived from the owner's policy. AUTOMATIC: the cap is the
    most one automatic refund may be, the daily budget bounds automatic refunds only. REVIEW: a limit
    of zero, so every review-routed intent is parked for the owner; the ceiling is enforced by the
    gate (G7) before anything gets here. Both carry the policy version, which is part of the terms an
    approval is bound to."""
    return [
        Mandate(AUTO_MANDATE, AUTO_AGENT, policy.per_order_cap_minor, policy.daily_budget_minor, tuple(capture_ids),
                tuple(policy.eligible_reasons), MANDATE_END, "USD", version),
        Mandate(REVIEW_MANDATE, REVIEW_AGENT, 0, policy.escalate_ceiling_minor, tuple(capture_ids),
                tuple(PURPOSES), MANDATE_END, "USD", version),
    ]


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").lower().split()).encode()).hexdigest()


@dataclass
class AuthResult:
    decision: str                 # what is enforced: ALLOW | ESCALATE | DENY
    rule_id: str
    reason_code: str
    route: str                    # AUTO | REVIEW | BLOCKED
    gate: GateResult | None
    without_flags: dict | None
    intent_id: str | None
    outcome: str | None           # engine outcome right after the call, None when nothing was submitted
    payment_ref: str | None
    amount_minor: int | None
    policy_version: int
    policy_hash: str
    replayed: bool = False
    record_id: str | None = None  # the signed decision record

    @property
    def released_minor(self) -> int:
        return (self.amount_minor or 0) if self.outcome == SUCCEEDED else 0

    @property
    def held_minor(self) -> int:
        return (self.amount_minor or 0) if self.outcome == PENDING else 0

    @property
    def unresolved_minor(self) -> int:
        return (self.amount_minor or 0) if self.outcome in (UNKNOWN, SUBMITTING, AUTHORIZED) else 0


def _same_terms(a: Intent, b: Intent) -> bool:
    """Same business payload. The agent id is left out: the authority sets it by route, so the stored intent and the
    signed one legitimately differ there."""
    pa, pb = a.payload(), b.payload()
    pa.pop("agent_id"), pb.pop("agent_id")
    return pa == pb


def _bound(intent: Intent, order, proposal: Proposal) -> bool:
    return (order is not None and intent.payee == order.capture_id and intent.currency == "USD"
            and intent.purpose_code == proposal.reason_code)


class UnknownOrder(Exception):
    pass


class RefundAuthority:
    def __init__(self, engine: Engine, log: Ledger, orders: OrderBook, policies: PolicyStore,
                 clock: Callable[[], datetime], channel: str = "desk",
                 prior_refunded: Callable[[str], int] = lambda order_ref: 0):
        self.engine, self.log, self.orders, self.policies, self.clock = engine, log, orders, policies, clock
        self.channel, self.prior_refunded = channel, prior_refunded
        self._mandate_version: int | None = None
        # Demo exchange rates (app/fx.py) are a SIMULATION feature, switched on by the Service for the fixture mode only. Against a real
        # PayPal sandbox nothing is converted and a signed decision that carries a conversion is never sent (recovery blocks it).
        self.demo_fx = False
        # ONE lock order everywhere: the money lock (engine._lock), then this thread lock, then
        # the policy chain's lock. Recovery and re-sends enter with the money lock already held (the engine calls in), so
        # taking this lock first, as authorize() used to, deadlocked a concurrent authorization against recovery. The core's
        # file lock waits for its in-process thread lock without a time limit; only the cross-process wait has the 60 s
        # timeout, so the order, not the timeout, is what prevents the deadlock.
        self._tl = threading.RLock()
        self.sync_mandates()

    # ---- policy -> mandates -------------------------------------------------------------------------
    def sync_mandates(self) -> tuple[Policy, dict]:
        """Make the engine's mandates match the active policy version. Cheap when nothing changed."""
        policy, rec = self.policies.active()           # a trusted read: raises if the policy chain is truncated or rolled back
        if self._mandate_version is not None and rec["version"] < self._mandate_version:
            raise LedgerIntegrityError("POLICY_ROLLBACK")
        if rec["version"] != self._mandate_version:
            # A decision record is signed with the policy version it was made under. A policy older than one already
            # decided on is a rollback, whatever the (possibly restored) policy chain says.
            floor = max((d.get("policy_version") or 0 for d in self.decisions()), default=0)
            if rec["version"] < floor:
                raise LedgerIntegrityError("POLICY_ROLLBACK")
            for m in build_mandates(policy, rec["version"], self.orders.capture_ids):
                self.engine.mandates[m.agent_id] = m
            self._mandate_version = rec["version"]
        return policy, rec

    # ---- reading the decision log ---------------------------------------------------------------------
    def decisions(self) -> list[dict]:
        return [r for r in trusted_receipts(self.log) if r["event"] == DECISION_EVENT]

    def decisions_by_intent(self) -> dict[str, dict]:
        out = {}
        for r in self.decisions():
            if r.get("intent_id"):
                if r.get("binding_conflict") and r["intent_id"] in out and not out[r["intent_id"]].get("binding_conflict"):
                    continue            # a refusal is never the decision for an intent that has a real one
                out[r["intent_id"]] = r
        return out

    def _refusal_logged(self, body: dict) -> dict | None:
        """The refusal record already written for this very attempt (same claim, same words), so a recovery that runs again
        does not add one record per pass."""
        for r in reversed(self.decisions()):
            if (r.get("binding_conflict") == body["binding_conflict"] and r.get("text_hash") == body["text_hash"]
                    and r.get("intent_id") == body["intent_id"] and r.get("order") == body["order"]):
                return r
        return None

    def _logged(self, key: str | None, intent_id: str | None) -> dict | None:
        """The signed decision already written for THIS intent (same key AND same intent id). A request id never selects a
        decision: it is an unsigned label, and one request's id must not let another request borrow its decision."""
        if not key or not intent_id:
            return None
        for r in reversed(self.decisions()):
            if r.get("idempotency_key") == key and r.get("intent_id") == intent_id:
                return r
        return None

    def binding_conflict(self, intent: Intent, order_ref: str, amount: int | None, request_id: str | None,
                         reason_code: str | None = None, thash: str | None = None) -> str | None:
        """None when the signed decision log is consistent with this (intent id, key, request id, order, amount, purpose, text):
        it either knows none of them, or knows them all as ONE operation. Otherwise the reason. The request file, its id and
        its stored intent are unsigned, so a conflict means the file is wrong, never the log. A signed decision binds the
        PURPOSE too: the reason class on the signed proposal must be the one on the intent and on the proposal now offered, so
        a decision made for one reason is never reused to send a refund for another. Refusal records
        (`binding_conflict`) are not decisions about an operation and are skipped."""
        for d in self.decisions():
            if d.get("binding_conflict"):
                continue
            if d.get("intent_id") == intent.id:
                if d.get("idempotency_key") != intent.idempotency_key:
                    return "INTENT_ID_REUSED"
                if d.get("request_id") not in (None, request_id):
                    return "REQUEST_ID_MISMATCH"
                if d.get("order") not in (None, order_ref) or (d.get("amount_minor") is not None and amount is not None
                                                                and d["amount_minor"] != amount):
                    return "DECISION_MISMATCH"
                signed_reason = (d.get("proposal") or {}).get("reason_code")
                if signed_reason is not None and (signed_reason != intent.purpose_code
                                                  or (reason_code is not None and signed_reason != reason_code)):
                    return "DECISION_MISMATCH"
                if thash is not None and d.get("text_hash") not in (None, thash):
                    return "DECISION_MISMATCH"
            elif intent.idempotency_key and d.get("idempotency_key") == intent.idempotency_key:
                return "KEY_REUSED"
            elif request_id and d.get("request_id") == request_id:
                return "REQUEST_ID_REUSED"
        return None

    # ---- context from signed history ------------------------------------------------------------------
    def context(self, order_ref: str, thash: str, now: datetime, states: dict | None = None,
                own_key: str | None = None, own_intent: str | None = None, exclude: str | None = None) -> Context:
        """`own_key` / `own_intent` identify the request being decided, so its own earlier decision
        record (written before a crash) is not mistaken for a repeated message. The unsigned request id is NOT used for
        this: a forged id must not hide an earlier decision from the repeat check. `exclude` is an intent already in the
        ledger that is being judged again (recovery): the money it holds is left out, so it is not counted against
        itself."""
        order = self.orders.get(order_ref)
        if order is None:
            raise UnknownOrder(order_ref)
        ledger = self.engine.ledger
        states = states if states is not None else intent_states(ledger)
        if exclude is not None:
            states = {k: v for k, v in states.items() if k != exclude}
        held, _ = held_on_capture(ledger, order.capture_id, states)
        remaining = order.total_minor - self.prior_refunded(order_ref) - held
        _, prec = self.policies.active()
        seen = any(r["order"] == order_ref and r["text_hash"] == thash and not r.get("binding_conflict")
                   and not (own_key and r.get("idempotency_key") == own_key and r.get("intent_id") == own_intent)
                   and not self._set_aside_by_owner(r, states, prec["version"]) for r in self.decisions())
        return Context(remaining_minor=remaining,
                       daily_released_minor=reserved_today(ledger, AUTO_AGENT, now, exclude=exclude),
                       seen_message=seen,
                       auto_on_order_minor=auto_reserved_on_capture(ledger, order.capture_id, AUTO_AGENT, states),
                       now=now)

    @staticmethod
    def _set_aside_by_owner(decision: dict, states: dict, active_version: int) -> bool:
        """An earlier decision under an OLDER policy version that the owner denied (or that expired unanswered) does
        not make the same words a repeat: its card could no longer be approved, so the owner's instruction "deny it;
        the buyer writes again" must lead to a fresh look under the rule now in force."""
        st = states.get(decision.get("intent_id") or "")
        if st is not None and st.rec["event"] == "blocked_at_recovery":
            return True             # recovery closed it before any send: the buyer may write again
        return (st is not None and st.rec["outcome"] in ("DENIED", "EXPIRED")
                and decision.get("policy_version") != active_version)

    # ---- ids -------------------------------------------------------------------------------------------
    def _next_intent_id(self) -> str:
        n = 0
        used = {d["intent_id"] for d in self.decisions() if d.get("intent_id")}
        for r in self.engine.ledger.receipts():
            try:
                n = max(n, int(r["intent"]["id"].split("-")[-1]))
            except (ValueError, KeyError, IndexError):
                continue
        while True:
            n += 1
            iid = f"I-{n:04d}"
            if self.engine.ledger.latest_for_intent(iid) is None and iid not in used:
                return iid

    # ---- the decision -----------------------------------------------------------------------------------
    def authorize(self, text: str, order_ref: str, proposal: Proposal, *, intent: Intent | None = None,
                  request_id: str | None = None, extra: dict | None = None) -> AuthResult:
        """Decide, record, and (only if allowed or parked for the owner) hand to the engine.
        `intent` is given when the caller already allocated ids and the idempotency key (the inbox does,
        so a crash can be recovered with the same key); otherwise one is built from the proposal."""
        with self.engine._lock, self._tl, self.policies.ledger.lock:
            # The policy chain's lock is held from the policy read to the send: no other service, thread or process can
            # save a new version in between (and `policy_guard` checks the version once more right before the dispatch).
            policy, prec = self.sync_mandates()
            ledger = self.engine.ledger
            now = self.clock()
            order = self.orders.get(order_ref)
            if intent is not None:
                prior = ledger.latest_for_key(intent.idempotency_key)
                if prior is not None:                       # replay: the engine answers with the earlier receipt
                    if prior["intent"]["id"] != intent.id or not _same_terms(Intent.from_snapshot(prior["intent"]), intent):
                        # same key under another intent id, or a different payload: never a quiet replay (the key is
                        # single-use for the one intent it was signed for)
                        raise IdempotencyConflict(intent.idempotency_key)
                    return self._replay(prior, prec)
            thash = text_hash(text)
            amount = proposal.amount_minor if proposal.action == REFUND and (proposal.amount_minor or 0) > 0 else None
            conflict = (self.binding_conflict(intent, order_ref, amount, request_id, proposal.reason_code, thash)
                        if intent is not None else None)
            if (conflict is None and intent is not None and order is not None and amount is not None
                    and (not _bound(intent, order, proposal) or intent.amount_minor != amount)
                    and (ledger.latest_for_intent(intent.id) is not None
                         or any(d.get("intent_id") == intent.id and not d.get("binding_conflict") for d in self.decisions()))):
                # The stored intent disagrees with the proposal (capture, currency, purpose or amount) for an operation a signed
                # decision already names: the unsigned file was altered. A refusal, never a new decision under that intent id.
                conflict = "DECISION_MISMATCH"
            if conflict is not None:
                # The request file names an id, key or request id that a SIGNED decision already ties to another intent.
                gate = bare = self._denial("G3", conflict, scan(text))
            else:
                gate, bare = self._judge(text, order_ref, order, proposal, policy, now, thash, intent=intent,
                                         request_id=request_id)
            if order is not None and intent is None and amount is not None and proposal.reason_code in PURPOSES:
                intent = Intent(self._next_intent_id(), AUTO_AGENT, order.capture_id, amount, "USD",
                                proposal.reason_code, "Buyer says: " + " ".join(text.split())[:240],
                                str(uuid.uuid4()), now)
            route = {ALLOW: ROUTE_AUTO, ESCALATE: ROUTE_REVIEW, DENY: ROUTE_BLOCKED}[gate.decision]
            if intent is not None and route != ROUTE_BLOCKED:
                agent = AUTO_AGENT if route == ROUTE_AUTO else REVIEW_AGENT
                intent = replace(intent, agent_id=agent)
            refusal = conflict
            if refusal is None and intent is not None and route == ROUTE_BLOCKED and ledger.latest_for_intent(intent.id) is not None:
                refusal = "INTENT_ID_IN_LEDGER"          # a refusal for an id that already has a signed receipt
            record_intent_id, record_key = (intent.id if intent else None), (intent.idempotency_key if intent else None)
            known = False
            if refusal:
                # A REFUSED attempt is recorded under its OWN record, and the decision log is append-only and keyed. The
                # claimed intent id is used only when no signed decision and no receipt uses it (an orphan's own, still
                # unknown id). If either does, the refusal carries NO intent id, key or request id, so it can neither replace
                # nor block the signed operation, and no receipt is written under that id: the unsigned file cannot prove it is
                # the request it claims to be (a conflicting orphan can copy every identifier), so it is refused and listed
                # for a person. (An earlier version let a "same operation altered" refusal close an unsent signed decision; a
                # conflicting orphan could use that to replace a legitimate decision.)
                known = (ledger.latest_for_intent(intent.id) is not None
                         or any(d.get("intent_id") == intent.id and not d.get("binding_conflict") for d in self.decisions()))
                if known:
                    record_intent_id = None
                record_key = None
            body = {"event": DECISION_EVENT, "at": now.isoformat(), "channel": self.channel,
                    # a refused request id is NOT written as the decision's request id: the id belongs to the intent it was bound to first
                    "request_id": None if refusal else request_id,
                    "intent_id": record_intent_id,
                    "idempotency_key": record_key,
                    "order": order_ref, "text": text, "text_hash": thash, "proposal": proposal.as_dict(),
                    "gate": gate.as_dict(),
                    "without_flags": {"decision": bare.decision, "rule_id": bare.rule_id,
                                      "decision_changed": bare.decision != gate.decision},
                    "policy_version": prec["version"], "policy_hash": prec["policy_hash"], "policy": policy.as_dict(),
                    "route": route, "amount_minor": amount, **(extra or {})}
            if refusal:
                body["binding_conflict"] = {"reason": refusal, "request_id_claimed": request_id,
                                            "intent_id_claimed": intent.id, "idempotency_key_claimed": intent.idempotency_key}
                rec = self._refusal_logged(body) or self.log.append(body)       # once, not once per recovery pass
            else:
                logged = (self._logged(body["idempotency_key"], intent.id) if intent is not None else None)
                same = (logged is not None and logged["route"] == route and logged["gate"]["rule_id"] == gate.rule_id
                        and logged.get("request_id") in (None, request_id))
                rec = logged if same else self.log.append(body)
            outcome = payment_ref = None
            decision, rule_id, reason = gate.decision, gate.rule_id, gate.reason_code
            if route == ROUTE_BLOCKED:
                if intent is not None:
                    if known or refusal in NO_RECEIPT_REFUSALS or ledger.latest_for_intent(intent.id) is not None:
                        # Nothing is written under an id or key that belongs to another operation: closing it with a
                        # receipt would close THAT operation. The caller holds the request file and lists it.
                        raise IdempotencyConflict(intent.id)
                    blocked = self.engine._write(
                        dict(intent=intent.snapshot(), flags=injection_flags(intent.reason_text),
                             decision={"decision": DENY, "rule_id": gate.rule_id, "reason_code": gate.reason_code}),
                        BLOCKED, "gate_blocked")
                    outcome = blocked["outcome"]
            elif intent is None:
                # The gate parked it for the owner, but there is no valid reason code to build an intent from,
                # so there is nothing an owner could approve. It is recorded (signed) and nothing is submitted.
                pass
            else:
                res = self.engine.submit(intent)           # the only path to the provider adapter
                outcome, payment_ref = res.receipt["outcome"], res.receipt["payment_ref"]
                if (outcome == BLOCKED and res.receipt["event"] == "blocked_at_execution"
                        and res.receipt["decision"]["rule_id"] == RECOVERY_RULE):
                    # the last check before the send found the policy had moved: say so in the signed decision log too
                    self.log.append(self._recovery_block_record(intent, rec, res.receipt["decision"]["reason_code"], None,
                                                                policy, self.policies.active()[1], now))
                if res.decision.decision != gate.decision:       # the engine's own check was stricter: it wins
                    decision, rule_id, reason = res.decision.decision, res.decision.rule_id, res.decision.reason_code
                    route = {ALLOW: ROUTE_AUTO, ESCALATE: ROUTE_REVIEW, DENY: ROUTE_BLOCKED}[decision]
            return AuthResult(decision, rule_id, reason, route, gate, body["without_flags"],
                              intent.id if intent else None, outcome, payment_ref, amount, prec["version"],
                              prec["policy_hash"], False, rec["id"])

    def _judge(self, text: str, order_ref: str, order, proposal: Proposal, policy: Policy, now: datetime, thash: str, *,
               intent: Intent | None, request_id: str | None, exclude: str | None = None) -> tuple[GateResult, GateResult]:
        """The ONE place a proposal is judged: the gate on the raw text, the signed history and the policy in force,
        plus the checks that tie a supplied intent to what was decided on. Fresh sends (`authorize`) and recovery
        (`recover_authorized`) both come through here, so recovery cannot be more lenient than a new request.
        Returns (the gate result that is enforced, the same gate without the text-flag rules)."""
        amount = proposal.amount_minor if proposal.action == REFUND and (proposal.amount_minor or 0) > 0 else None
        if order is None:
            # An order we do not know proves nothing about the capture a supplied intent names (the unsigned
            # request file can be edited so that the capture is a real, allowlisted one). Fail closed: this is a
            # DENY with a signed decision record, and the intent, if any, is written BLOCKED. It never reaches
            # the engine's submit, so it can neither be sent nor skip the order allowance.
            gate = self._denial("G3", "ORDER_MISMATCH", scan(text))
            return gate, gate
        ctx = self.context(order_ref, thash, now, own_key=intent.idempotency_key if intent else None,
                           own_intent=intent.id if intent else None, exclude=exclude)
        gate = decide(text, order_ref, order, proposal, policy, ctx)
        bare = decide(text, order_ref, order, proposal, policy, ctx, scan_flags=False)
        if intent is not None and amount is not None and not _bound(intent, order, proposal):
            # A supplied (persisted) intent must name THIS order's capture, in USD, for the proposal's reason.
            # The unsigned request file is only a pointer: if it disagrees, nothing is sent and the attempt is blocked.
            gate = self._denial("G3", "ORDER_MISMATCH", gate.flags)
            return gate, gate
        if intent is not None and intent.amount_minor != amount:
            # The persisted intent and the proposal disagree on the amount: the unsigned pointer was altered.
            gate = self._denial("G3", "ORDER_MISMATCH", gate.flags)
            return gate, gate
        return gate, bare

    # ---- recovery -----------------------------------------------------------------------------------------
    def recover_authorized(self, rec: dict) -> dict | None:
        """Recovery of an AUTHORIZED refund that never reached SUBMITTING (the process stopped after the signed
        authorization and before the send). It is judged again as a new request would be, then sent or closed:

        1. Under the money lock, the policy chain is read together with its checkpoint and the mandates are made to
           match the version in force NOW. The decision chain is read together with ITS checkpoint, whole. If either
           fails to verify, `LedgerIntegrityError` goes up and the engine HOLDS the record: not sent, not closed.
        2. The signed decision for this intent must exist and agree with the receipt (key, amount, route, order and
           capture). The policy version it was made under must still be the one in force. The raw buyer text and the
           proposal are taken from the SIGNED decision, never from the editable request file.
        3. The same gate (`_judge`) is run on that text under the current policy and the signed history, leaving this
           refund out of its own totals. An automatic refund must still be ALLOW; an owner-approved one must not be a
           DENY (the owner's yes already covered the escalation, but not a hard stop such as a lowered ceiling).
        4. Only then does the engine's own pre-dispatch check run and the refund go out, in the same lock hold.
        Any failure after step 1 writes a new signed decision (route BLOCKED, rule G18, with the reason and the
        re-check result) and a BLOCKED receipt. Nothing was sent, so closing it is safe; the buyer can write again.
        Returns the receipt written, or None when another process already moved the record."""
        with self.engine._lock, self._tl, self.policies.ledger.lock:
            policy, prec = self.sync_mandates()
            by_intent = self.decisions_by_intent()
            cur = self.engine.ledger.latest_for_intent(rec["intent"]["id"])
            if cur is None or cur["outcome"] != AUTHORIZED:
                return None
            intent = Intent.from_snapshot(cur["intent"])
            dec = by_intent.get(intent.id)
            now = self.clock()
            why, rechecked = self._recovery_verdict(intent, dec, policy, prec, now)
            if why is None:
                return self.engine._execute(cur)
            self.log.append(self._recovery_block_record(intent, dec, why, rechecked, policy, prec, now))
            return self.engine._follow(cur, BLOCKED, "blocked_at_recovery",
                                       decision=Decision(DENY, RECOVERY_RULE, why).as_dict())

    def resend_authorized(self, rec: dict) -> dict | None:
        """A same-key re-send of an UNKNOWN refund goes through the SAME authority path as recovery. The caller
        (Service.retry) has already decided that a re-send is allowed in principle (the mode, a re-check that found no
        known id, the replay window); this decides whether THIS refund may still go out under the policy and the signed
        records in force NOW: policy chain and decision chain read with their checkpoints, the signed decision for this
        intent in agreement with the receipt, the policy version it was made under still the one in force, and the gate
        (`_judge`) run again on the signed text with this refund left out of its own totals.

        Unlike an AUTHORIZED refund that was never sent, an UNKNOWN one may already have been accepted by PayPal, so a
        refusal does NOT close it: a signed `resend_refused` receipt keeps the outcome UNKNOWN and the hold stays. The
        receipt carries the reason. A chain that does not verify refuses the re-send too (reason: the chain's code)."""
        with self.engine._lock, self._tl, self.policies.ledger.lock:
            cur = self.engine.ledger.latest_for_intent(rec["intent"]["id"])
            if cur is None or cur["outcome"] != UNKNOWN:
                return cur
            intent = Intent.from_snapshot(cur["intent"])
            try:
                policy, prec = self.sync_mandates()
                why, _ = self._recovery_verdict(intent, self.decisions_by_intent().get(intent.id), policy, prec, self.clock())
            except LedgerIntegrityError as e:
                why = e.reason_code
            if why is not None:
                return self.engine._follow(cur, UNKNOWN, "resend_refused", resend_refused={"reason": why})
            return self.engine._execute(cur)

    def review_binding(self, intent_id: str) -> str | None:
        """None when approving or denying this intent is bound to a SIGNED authority decision for it: the decision exists,
        was routed to the owner, and agrees with the receipt on key, amount, agent, order and capture. Otherwise
        `NO_AUTHORITY_DECISION`. An intent that is missing or no longer waiting is left to the engine, which answers it
        itself (and acts on nothing). A chain that does not verify raises `LedgerIntegrityError`."""
        rec = self.engine.ledger.latest_for_intent(intent_id)
        if rec is None or rec["outcome"] != PENDING:
            return None
        dec = self.decisions_by_intent().get(intent_id)
        i = rec["intent"]
        order = self.orders.get(dec.get("order")) if dec else None
        if (dec is None or dec.get("route") != ROUTE_REVIEW or dec.get("idempotency_key") != i["idempotency_key"]
                or dec.get("amount_minor") != i["amount_minor"] or i["agent_id"] != REVIEW_AGENT
                or order is None or i["payee"] != order.capture_id
                or (dec.get("proposal") or {}).get("reason_code") != i["purpose_code"]):
            return "NO_AUTHORITY_DECISION"
        return None

    def policy_guard(self) -> str | None:
        """The compare-and-swap right before any dispatch: None while the policy version the mandates were built from is
        still the one in force, else the reason code. Never a substitute for the lock, a second line of defence behind it
        (a save from a process that does not take the lock, or a peer in another process)."""
        _, rec = self.policies.active()
        return None if rec["version"] == self._mandate_version else "POLICY_CHANGED"

    def _recovery_verdict(self, intent: Intent, dec: dict | None, policy: Policy, prec: dict,
                          now: datetime) -> tuple[str | None, GateResult | None]:
        """(None, None) when the refund may still go out, else (reason code, the gate result of the re-check if any)."""
        if dec is None:
            return "NO_DECISION_RECORD", None
        if dec.get("fx") and not self.demo_fx:
            return "FOREIGN_CURRENCY_HELD", None
        order = self.orders.get(dec.get("order"))
        agent = {ROUTE_AUTO: AUTO_AGENT, ROUTE_REVIEW: REVIEW_AGENT}.get(dec.get("route"))
        if (agent is None or intent.agent_id != agent or dec.get("idempotency_key") != intent.idempotency_key
                or dec.get("amount_minor") != intent.amount_minor or order is None
                or intent.payee != order.capture_id
                or (dec.get("proposal") or {}).get("reason_code") != intent.purpose_code):
            return "DECISION_MISMATCH", None
        if dec.get("policy_version") != prec["version"]:
            return "POLICY_CHANGED", None
        gate, _ = self._judge(dec["text"], dec["order"], order, Proposal.from_dict(dec["proposal"]), policy, now,
                              dec["text_hash"], intent=intent, request_id=dec.get("request_id"), exclude=intent.id)
        allowed = gate.decision == ALLOW if agent == AUTO_AGENT else gate.decision != DENY
        return (None, None) if allowed else ("RECHECK_FAILED", gate)

    def _recovery_block_record(self, intent: Intent, dec: dict | None, why: str, rechecked: GateResult | None,
                               policy: Policy, prec: dict, now: datetime) -> dict:
        text = dec["text"] if dec else intent.reason_text
        gate = GateResult(DENY, RECOVERY_RULE, why, [],
                          [{"rule": r, "result": "NOT_REACHED"} for r in RULES] + [{"rule": RECOVERY_RULE, "result": "HIT"}], True)
        return {"event": DECISION_EVENT, "at": now.isoformat(), "channel": self.channel,
                "request_id": dec.get("request_id") if dec else None, "intent_id": intent.id,
                "idempotency_key": intent.idempotency_key, "order": dec.get("order") if dec else None,
                "text": text, "text_hash": text_hash(text), "proposal": dec["proposal"] if dec else None,
                "gate": gate.as_dict(),
                "without_flags": {"decision": DENY, "rule_id": RECOVERY_RULE, "decision_changed": False},
                "policy_version": prec["version"], "policy_hash": prec["policy_hash"], "policy": policy.as_dict(),
                "route": ROUTE_BLOCKED, "amount_minor": intent.amount_minor,
                "recovery": {"reason": why, "decided_under_version": dec.get("policy_version") if dec else None,
                             "version_now": prec["version"], "recheck": rechecked.as_dict() if rechecked else None}}

    @staticmethod
    def _denial(rule: str, code: str, flags: list) -> GateResult:
        """A hard DENY raised by the authority itself (the supplied intent does not match what was decided on)."""
        return GateResult(DENY, rule, code, flags,
                          [{"rule": r, "result": "HIT" if r == rule else "NOT_REACHED"} for r in RULES], True)

    def _replay(self, prior: dict, prec: dict) -> AuthResult:
        d = prior["decision"]
        logged = self.decisions_by_intent().get(prior["intent"]["id"])
        route = {ALLOW: ROUTE_AUTO, ESCALATE: ROUTE_REVIEW, DENY: ROUTE_BLOCKED}[d["decision"]]
        return AuthResult(d["decision"], d["rule_id"], d["reason_code"], route, None,
                          logged["without_flags"] if logged else None, prior["intent"]["id"], prior["outcome"],
                          prior["payment_ref"], prior["intent"]["amount_minor"], prec["version"], prec["policy_hash"],
                          True, logged["id"] if logged else None)
