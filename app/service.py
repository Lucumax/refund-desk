"""Wires the pieces together: core engine + PayPal adapter + refund authority + agent + status sync +
sessions. The refund authority (app/authority.py) is the only thing that decides and the only thing
that reaches the engine; the provider adapter behind it is the only thing that differs between the
fake PayPal (simulation) and the PayPal sandbox."""
from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Callable

from approval_receipt import Ledger, ApproverSession, LedgerIntegrityError, FOUND
from approval_receipt.engine import IdempotencyConflict

from .agent import RefundAgent, AGENT_ID, MessageRejected
from .arena_policy import DEFAULT as DEFAULT_POLICY
from .arena_reader import Reader
from .auth import SessionManager
from .authority import RefundAuthority, build_mandates, AUTO_AGENT
from .holding_engine import HoldingEngine
from .extractor import Extractor, RuleBasedExtractor, PURPOSES
from .ledgerview import dispatch_count, intent_states
from .money import to_minor, to_value
from .orders import OrderBook
from .paypal_adapter import PayPalAdapter, PayPalClient, PayPalHttpError, REPLAY_WINDOW, KNOWN_STATUSES, refund_mismatches
from .policy_store import PolicyStore
from .store import Store
from .trusted import TrustedLedger
from .transport import Transport, TransportTimeout
from .webhooks import RefundSync, WebhookProcessor

# A same-key re-send of a refund PayPal may already have made rests on PayPal returning the SAME refund for a repeated
# PayPal-Request-Id. That has not been verified against PayPal (NOT_DONE.md). Until a sandbox run records it, a re-send
# is refused in every mode except the fixture (the simulation), where the fake PayPal is built to do exactly that.
LIVE_RESEND_VERIFIED = False
MANDATE_ID = "M-REFUND-AUTO"
PER_REFUND_LIMIT_MINOR = DEFAULT_POLICY.per_order_cap_minor     # defaults of the first policy version
DAILY_LIMIT_MINOR = DEFAULT_POLICY.daily_budget_minor
KEY_ENV = "APPROVAL_RECEIPT_HMAC_KEY"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResendRefused(Exception):
    """The owner asked to send an unresolved refund again and the rules do not allow it. `code` says why."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StartupIntegrityError(RuntimeError):
    """The saved receipts do not match the trusted checkpoint (or the checkpoint is missing).
    Startup stops BEFORE writing anything, so the evidence is preserved."""

    def __init__(self, reason_code: str, first_broken_id: str | None):
        super().__init__(f"receipt integrity check failed at startup: {reason_code} (at {first_broken_id})")
        self.reason_code, self.first_broken_id = reason_code, first_broken_id


def default_checkpoint_dir(data_dir: str) -> str:
    """A path OUTSIDE the data directory. Same disk by default, so it only catches casual
    edits; point it at another disk or service for real protection (NOT_DONE.md)."""
    return os.path.abspath(data_dir).rstrip("\\/") + ".checkpoint"


class Service:
    def __init__(self, data_dir: str | None, transport: Transport, orders: OrderBook,
                 extractor: Extractor | None = None, clock: Callable[[], datetime] = utcnow,
                 passcode: str = "demo", key: str | None = None, webhook_id: str = "WEBHOOK-ID-FROM-ENV",
                 credentials=None, base_url: str | None = None, mode: str = "fixture",
                 approval_ttl: timedelta = timedelta(hours=24), checkpoint_dir: str | None = None,
                 policies: PolicyStore | None = None, channel: str = "desk",
                 prior_refunded: Callable[[str], int] = lambda order_ref: 0, consent_reader=None):
        if not data_dir:
            raise ValueError("data_dir required (tests pass a tmp dir)")
        self.mode, self.clock, self.orders, self.data_dir = mode, clock, orders, data_dir
        self.reader_name = "rule-based fallback reader (not a model)"   # __main__ renames it when a model is set up
        os.makedirs(data_dir, exist_ok=True)
        self.checkpoint_dir = checkpoint_dir or default_checkpoint_dir(data_dir)
        self.checkpoint_path = os.path.join(self.checkpoint_dir, "checkpoint.json")
        if os.path.abspath(self.checkpoint_dir).startswith(os.path.abspath(data_dir) + os.sep):
            raise ValueError("checkpoint must live outside the data directory")
        # No fallback key. Sandbox mode needs APPROVAL_RECEIPT_HMAC_KEY. Fixture (demo) mode may use
        # the public demo key; the core then stamps every record key_mode=DEMO.
        signing_key = key or os.environ.get(KEY_ENV)
        self.demo_key = not signing_key
        self.signing_key = signing_key
        if self.demo_key and mode != "fixture":
            raise RuntimeError(f"sandbox mode needs {KEY_ENV} in the environment (any long random string you keep)")
        # Three signed chains: the money state machine (receipts), the authority's decision records
        # (decisions), and the policy versions (policy, shared with the arena). Each is checked
        # against its own separately stored checkpoint BEFORE anything is written.
        self.ledger = self._open_chain(os.path.join(data_dir, "receipts.jsonl"), self.checkpoint_path)
        self.decisions = self._open_chain(os.path.join(data_dir, "decisions", "receipts.jsonl"),
                                          os.path.join(self.checkpoint_dir, "decisions-checkpoint.json"))
        self.own_policies = policies is None
        if policies is None:
            policy_ledger = self._open_chain(os.path.join(data_dir, "policy", "receipts.jsonl"),
                                             os.path.join(self.checkpoint_dir, "policy-checkpoint.json"))
            policies = PolicyStore(policy_ledger, clock)
        self.policies = policies
        self.store = Store(os.path.join(data_dir, "state.json"))
        kw = {}
        if credentials:
            kw["credentials"] = credentials
        if base_url:
            kw["base_url"] = base_url
        self.client = PayPalClient(transport, **kw)
        self.adapter = PayPalAdapter(self.client, self.store.refund_index, self.store.save,
                                     dispatch_time=self._first_dispatch, clock=clock,
                                     custom_id_for_key=self._custom_id_for_key,
                                     dispatch_count=lambda k: dispatch_count(self.ledger, k),
                                     terms_for_key=self._terms_for_key)
        policy, prec = self.policies.active()
        # execution_ttl is the validity of an authorization BEFORE its first dispatch. It does not protect an
        # UNKNOWN refund: HoldingEngine keeps that hold whatever happens to permission to send again, and the
        # replay window (counted from the first dispatch) is the only time limit on a same-key re-send.
        self.engine = HoldingEngine(build_mandates(policy, prec["version"], orders.capture_ids), self.ledger, self.adapter,
                             clock=clock, approval_ttl=approval_ttl, execution_ttl=REPLAY_WINDOW + timedelta(hours=1))
        self.authority = RefundAuthority(self.engine, self.decisions, orders, self.policies, clock, channel, prior_refunded)
        # Recovery is never a second path around the authority: the engine hands every AUTHORIZED-but-unsent record to
        # the authority, which judges it again under the policy in force now (app/authority.py, recover_authorized).
        self.authority.demo_fx = (mode == "fixture")       # demo exchange rates exist in the simulation only (app/fx.py)
        self.engine.recovery_gate = self.authority.recover_authorized
        self.engine.resend_authority = self.authority.resend_authorized      # an UNKNOWN re-send is judged the same way
        self.engine.policy_guard = self.authority.policy_guard               # the version check right before every send
        self.agent = RefundAgent(self.engine, extractor or RuleBasedExtractor(), orders, self.store, clock,
                                 authority=self.authority, consent_reader=consent_reader)
        self.sync = RefundSync(self.engine, self.adapter, self.store, clock)
        self.webhooks = WebhookProcessor(self.sync, webhook_id)
        self.auth = SessionManager(passcode, clock)
        self.recovery_held: list = []       # [(intent id, reason code)] AUTHORIZED records the last recover() could not judge

    def _open_chain(self, path: str, checkpoint_path: str) -> Ledger:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existed = os.path.exists(path) and os.path.getsize(path) > 0
        ledger = TrustedLedger(path, self.signing_key, checkpoint_path=checkpoint_path,
                               allow_demo_key=self.demo_key)   # raises TRUNCATED for an emptied ledger
        if existed:
            # Verify the EXISTING chain against the EXISTING checkpoint before anything is written.
            # Never re-save or replace the checkpoint here: that would launder truncation.
            res = ledger.verify_trusted()
            if not res.ok:
                raise StartupIntegrityError(res.reason_code, res.first_broken_id)
        return ledger

    @property
    def mandate(self):
        """The AUTOMATIC mandate in force (follows the active policy version)."""
        self.authority.sync_mandates()
        return self.engine.mandates[AUTO_AGENT]

    def enforced_limits(self) -> dict:
        """The numbers the refund engine actually enforces right now, read from its live mandates (not from the
        policy store), next to the policy in force. The screens show these, and `matches_policy` says whether the two
        agree. They are made to agree by RefundAuthority.sync_mandates() before every decision."""
        from .authority import REVIEW_AGENT
        policy, rec = self.authority.sync_mandates()
        auto, review = self.engine.mandates[AUTO_AGENT], self.engine.mandates[REVIEW_AGENT]
        out = {"policy_version": rec["version"], "policy_hash": rec["policy_hash"],
               "automatic_allowance_minor": auto.per_payment_limit, "daily_budget_minor": auto.daily_limit,
               "approve_ceiling_minor": review.daily_limit, "mandate_version": auto.version,
               "evidence_required": policy.evidence_required, "eligible_reasons": sorted(auto.purpose_codes)}
        out["matches_policy"] = (auto.version == review.version == rec["version"]
                                 and out["automatic_allowance_minor"] == policy.per_order_cap_minor
                                 and out["daily_budget_minor"] == policy.daily_budget_minor
                                 and out["approve_ceiling_minor"] == policy.escalate_ceiling_minor
                                 and out["eligible_reasons"] == sorted(policy.eligible_reasons))
        return out

    # ---- buyer capability ------------------------------------------------------
    def buyer_capability(self, order_ref: str) -> str:
        """Order-bound proof, issued with the order link. HMAC of the order number under the
        signing key, so it cannot be guessed and one order's token opens no other order."""
        return self.ledger.mac("buyer-capability-v1:" + order_ref)[:32]

    def buyer_link(self, order_ref: str) -> str:
        return f"/buyer.html?o={order_ref}&c={self.buyer_capability(order_ref)}"

    def verify_capability(self, order_ref: str, cap: str) -> bool:
        import hmac
        return (self.orders.get(order_ref) is not None and isinstance(cap, str)
                and hmac.compare_digest(cap, self.buyer_capability(order_ref)))

    def _first_dispatch(self, key: str) -> datetime | None:
        """First time this idempotency key was sent, from signed receipts."""
        times = [datetime.fromisoformat(r["created_at"]) for r in self.ledger.receipts()
                 if r["event"] == "dispatch" and r["intent"]["idempotency_key"] == key]
        return min(times) if times else None

    def _terms_for_key(self, key: str):
        """(amount_minor, currency) we signed for this idempotency key: what a refund resource must agree with."""
        rec = self.ledger.latest_for_key(key)
        return (rec["intent"]["amount_minor"], rec["intent"]["currency"]) if rec else None

    def _custom_id_for_key(self, key: str) -> str:
        """What we send as the refund's custom_id: our intent id (for reconciliation). Read from the
        signed receipt the engine wrote before this send."""
        rec = self.ledger.latest_for_key(key)
        return rec["intent"]["id"] if rec else key

    # ---- actions ---------------------------------------------------------------
    def prefetch(self, text: str, order_ref: str, capability: str, reader: Reader | None = None,
                 extractor: Extractor | None = None, consent_reader=None):
        """The model's reading of one message, taken BEFORE the app-wide lock (see agent.Prefetch). The buyer's capability is
        checked first, so a caller without proof never costs a model call; the throttle slot is taken before the model is asked."""
        if not self.verify_capability(order_ref or "", capability or ""):
            raise MessageRejected("PROOF_REQUIRED")
        return self.agent.prefetch(text, order_ref, reader=reader, extractor=extractor, consent_reader=consent_reader)

    def message(self, text: str, order_ref: str, capability: str, reader: Reader | None = None,
                extractor: Extractor | None = None, label: str | None = None, reader_label: str | None = None,
                consent_reader=None, prefetched=None) -> dict:
        if not self.verify_capability(order_ref or "", capability or ""):
            raise MessageRejected("PROOF_REQUIRED")
        req = self.agent.handle_message(text, order_ref, reader=reader, extractor=extractor, label=label,
                                        reader_label=reader_label or (None if reader is not None else self.reader_name),
                                        consent_reader=consent_reader, prefetched=prefetched)
        return self._after(req)

    def approve(self, intent_id: str, session: ApproverSession, nonce: str, digest: str, note: str = ""):
        from approval_receipt import ApprovalError
        from .authority import REVIEW_AGENT
        if session.approver_id in (AUTO_AGENT, REVIEW_AGENT):
            raise ApprovalError("APPROVER_IS_AGENT")        # neither of the application's own agent identities can approve
        with self.engine._lock, self.policies.ledger.lock:
            # The trusted policy read, the mandate refresh and the approval are ONE step under the money lock AND the policy
            # chain's lock, so no other service instance can save a policy between "refresh" and "approve" (both are
            # cross-process file locks, re-entrant here).
            self.authority.sync_mandates()      # a policy saved since the card was shown changes the bound terms
            self._require_decision(intent_id)
            rec = self.engine.approve(intent_id, session, nonce, digest, note)
        self._after({"intent_id": intent_id})
        return rec

    def deny(self, intent_id: str, session: ApproverSession, note: str = ""):
        with self.engine._lock:
            self._require_decision(intent_id)
            rec = self.engine.deny(intent_id, session, note)
        self._after({"intent_id": intent_id})
        return rec

    def _require_decision(self, intent_id: str) -> None:
        """Approve and deny act only on an intent that a SIGNED authority decision routed to the owner."""
        from approval_receipt import ApprovalError
        why = self.authority.review_binding(intent_id)
        if why is not None:
            raise ApprovalError(why)

    def reconcile(self, intent_id: str):
        rec = self.engine.reconcile(intent_id)
        self._after({"intent_id": intent_id})
        return rec

    def recheck(self, intent_id: str) -> dict:
        """The owner's action on an UNKNOWN (or still SUBMITTING) refund: look again, send NOTHING.

        If we hold a refund id, it reads PayPal's refund resource (GET /v2/payments/refunds/{id}). If we do NOT hold
        one, NO request to PayPal is made at all: the answer comes from our own records only. Every result except
        NOT_UNRESOLVED is written as a signed `recheck` receipt, so the desk can show it and a later reader can audit it.

        Results:
          FOUND          PayPal's refund resource was read (`paypal_read` true); it is now recorded and read back.
          NO_KNOWN_ID    no refund id is in our records, so nothing was read from PayPal (`paypal_read` false). This
                         does NOT show that PayPal made no refund: this build cannot search PayPal by request id.
          UNREADABLE     we hold an id but the read failed or did not match, or the look-up window has passed.
          NOT_UNRESOLVED nothing to check."""
        with self.engine._lock:
            before = self.ledger.latest_for_intent(intent_id)
            if before is None:
                raise KeyError("NOT_FOUND")
            rec, status = self.engine._reconcile(intent_id)
            if status is None:
                return {"ok": True, "result": "NOT_UNRESOLVED", "intent_id": intent_id, "outcome": rec["outcome"],
                        "sent_again": False, "paypal_read": False, "mode": self.mode}
            key = rec["intent"]["idempotency_key"]
            basis = self.adapter.lookup_basis(key)
            if status == FOUND:
                result = "FOUND"
            else:
                result = "NO_KNOWN_ID" if basis == self.adapter.NO_KNOWN_ID else "UNREADABLE"
            summary = {"result": result, "intent_id": intent_id, "outcome_before": before["outcome"], "outcome_after": rec["outcome"],
                       "refund_id": rec.get("payment_ref"), "checked_at": self.clock().isoformat(), "mode": self.mode,
                       "sent_again": False, "paypal_read": result == "FOUND",
                       "read_attempted": result == "FOUND" or basis == self.adapter.KNOWN_ID}
            latest = self.ledger.latest_for_intent(intent_id)
            body = {k: v for k, v in latest.items() if k not in ("seq", "id", "key_id", "key_mode", "prev_hash", "hash", "signature")}
            body.update(event="recheck", recheck=summary, created_at=self.clock().isoformat())
            self.ledger.append(body)
            if result == "FOUND":
                self._after({"intent_id": intent_id})
                return {"ok": True, **summary, "readback": self.read_back(intent_id)}
            return {"ok": True, **summary}

    def resend_gate(self, intent_id: str) -> str | None:
        """None when a same-key re-send of this UNKNOWN refund is allowed, else the reason code. A re-send needs ALL of:
        the refund is UNKNOWN; the mode is the simulation (or a sandbox run has verified repeated-key behavior); a
        re-check AFTER the last send found no known id; and the first send is inside the replay window."""
        st = intent_states(self.ledger).get(intent_id)
        if st is None:
            return "NOT_FOUND"
        if st.rec["outcome"] != "UNKNOWN":
            return "NOT_UNKNOWN"
        if self.mode != "fixture" and not LIVE_RESEND_VERIFIED:
            return "RESEND_NOT_VERIFIED"
        basis = self.adapter.lookup_basis(st.rec["intent"]["idempotency_key"])
        if basis != self.adapter.NO_KNOWN_ID:
            return "REFUND_ID_KNOWN" if basis == self.adapter.KNOWN_ID else "WINDOW_PASSED"
        last_send = max((r["seq"] for r in st.history if r["event"] == "dispatch"), default=-1)
        checks = [r["recheck"] for r in st.history if r["event"] == "recheck" and r["seq"] > last_send]
        if not checks or checks[-1]["result"] != "NO_KNOWN_ID":
            return "RECHECK_REQUIRED"
        return None

    def retry(self, intent_id: str):
        """Same-key re-send of an UNKNOWN refund. First it only LOOKS (a known id is read, never re-sent); a re-send
        happens only if `resend_gate` allows it. Raises ResendRefused when the owner asked for a re-send that is not
        allowed. Permission to re-send never changes the hold on the money (see HoldingEngine)."""
        with self.engine._lock:
            rec, status = self.engine._reconcile(intent_id)
            if rec is None:
                raise KeyError("NOT_FOUND")
            if status == FOUND or rec["outcome"] != "UNKNOWN":
                self._after({"intent_id": intent_id})
                return rec
            why = self.resend_gate(intent_id)
            if why in ("REFUND_ID_KNOWN", "WINDOW_PASSED"):
                return rec                                  # nothing is sent automatically; a person resolves it
            if why is not None:
                raise ResendRefused(why)
            rec = self.engine.resend_unknown(intent_id)
            self._after({"intent_id": intent_id})
            return rec

    def recover(self) -> list[str]:
        """Run at start and then on a timer. After the core's integrity guard:
        1. requests saved but never submitted are submitted (same stored intent, same key);
        2. AUTHORIZED records that never dispatched are judged again by the refund authority under the policy and
           limits in force NOW (trusted policy read, decision chain read with its checkpoint, the same gate as a new
           request) and sent only if it still says yes; otherwise they are closed (BLOCKED, signed) or, when a signed
           chain does not verify, left AUTHORIZED and listed in `recovery_held` (nothing sent);
        3. SUBMITTING and UNKNOWN payments are read back (never blindly re-sent);
        4. webhook events queued during a read outage are processed;
        5. refunds PayPal still shows PENDING are polled to a final status.
        Returns the ids of intents touched."""
        touched: list[str] = []
        conflicts: list = []
        for req in self.agent.orphans():
            try:
                self.agent.submit_persisted(req)
            except IdempotencyConflict:
                # The unsigned request file names a key or intent id that the signed records tie to something else. It is
                # left alone (nothing is sent for it) and listed, and it does not stop the other requests being recovered.
                conflicts.append((req["intent_id"], "IDEMPOTENCY_CONFLICT"))
                continue
            touched.append(req["intent_id"])
        for rec in self.engine.recover():
            touched.append(rec["intent"]["id"])
        self.recovery_held = list(self.engine.held_in_recovery) + conflicts
        self.webhooks.retry_queued()
        touched += self.sync.poll_pending()
        for iid in {t for t in touched}:
            self.sync.sync_from_adapter(iid)
        return sorted(set(touched))

    def poll(self) -> list[str]:
        return self.recover()

    def save_policy(self, text: str, who: str, base_version, result_hash) -> dict:
        """Save a policy version and make the engine's mandates match it as ONE step under the money lock, so no
        decision can run against a mandate older than the version that was just saved. Lock order everywhere is the
        money chain first, then the policy chain."""
        with self.engine._lock:
            res = self.policies.save(text, who, base_version, result_hash)
            if res["ok"]:
                self.authority.sync_mandates()      # open approvals bound to the old version can no longer be approved
            return res

    def expire_due(self):
        return self.engine.expire_due()

    def webhook(self, headers: dict, body: bytes):
        return self.webhooks.process(headers, body)

    def _after(self, req: dict) -> dict:
        if req.get("intent_id"):
            self.sync.sync_from_adapter(req["intent_id"])
        return req

    def projection_ok(self, req: dict) -> bool:
        """The unsigned request record must agree with the signed receipt it points at."""
        if not req.get("intent_id"):
            return True
        rec = self.ledger.latest_for_intent(req["intent_id"])
        if rec is None:
            return True     # orphan: recovery will submit it from the stored intent
        a, b = req.get("intent") or {}, rec["intent"]
        if not all(a.get(k) == b.get(k) for k in ("id", "payee", "amount_minor", "currency", "purpose_code",
                                                  "idempotency_key")):
            return False
        # The order the owner is shown must be the order whose capture the signed intent pays. The request file's
        # order reference is unsigned; the capture in the signed receipt is not.
        order = self.orders.get(req.get("order_ref"))
        expected = order.capture_id if order else "unknown:" + (req.get("order_ref") or "")
        return b["payee"] == expected and req.get("capture_id") in (None, b["payee"])

    def checkpoint(self) -> dict:
        with open(self.checkpoint_path, encoding="utf-8") as f:
            return json.load(f)

    # ---- proof from the provider's own refund resources ---------------------------------------------
    def read_back(self, intent_id: str) -> dict:
        """Read the refund back from PayPal (GET /v2/payments/refunds/{id}) and compare it with what
        we sent. Evidence only from the refund resource we hold an id for; never a capture-level total.
        Missing provider data FAILS a check, it never counts as zero. The result is also written as a
        signed receipt (a sanitized summary, no tokens)."""
        st = intent_states(self.ledger).get(intent_id)
        if st is None:
            return {"ok": False, "reason": "NO_SUCH_INTENT", "checks": []}
        rec = st.rec
        ref = rec["payment_ref"]
        if not ref:
            return {"ok": False, "reason": "NO_REFUND_ID", "intent_id": intent_id, "checks": [], "mode": self.mode}
        try:
            d = self.client.get_refund(ref)
        except (TransportTimeout, PayPalHttpError, ValueError) as e:
            return {"ok": False, "reason": "PROVIDER_UNREADABLE", "detail": type(e).__name__, "intent_id": intent_id,
                    "refund_id": ref, "checks": [], "mode": self.mode}
        amount = d.get("amount") if isinstance(d.get("amount"), dict) else {}
        want = rec["intent"]
        checks = [
            {"id": "refund_id", "ok": d.get("id") == ref, "expected": ref, "seen": d.get("id")},
            {"id": "status_known", "ok": d.get("status") in KNOWN_STATUSES, "seen": d.get("status")},
            {"id": "amount", "ok": amount.get("value") == to_value(want["amount_minor"]),
             "expected": to_value(want["amount_minor"]), "seen": amount.get("value")},
            {"id": "currency", "ok": amount.get("currency_code") == want["currency"],
             "expected": want["currency"], "seen": amount.get("currency_code")},
            {"id": "custom_id_echo", "ok": d.get("custom_id") in (None, "", intent_id), "seen": d.get("custom_id"),
             "note": "informational: PayPal may not echo custom_id"},
        ]
        required = ("refund_id", "status_known", "amount", "currency")
        bound = all(c["ok"] for c in checks if c["id"] in required)       # this resource IS the refund we sent
        ok = bound and d.get("status") in ("COMPLETED", "PENDING")
        if bound and d.get("status") in KNOWN_STATUSES:
            self.sync.apply_status(ref, d["status"], "readback")           # never take a status from a resource that does not match
        sanitized = {"id": d.get("id"), "status": d.get("status"), "amount": amount, "custom_id": d.get("custom_id")}
        summary = {"ok": ok, "intent_id": intent_id, "refund_id": ref, "status": d.get("status"), "mode": self.mode,
                   "checks": checks, "provider_response_sha256": hashlib.sha256(
                       json.dumps(sanitized, sort_keys=True).encode()).hexdigest(),
                   "checked_at": self.clock().isoformat()}
        latest = self.ledger.latest_for_intent(intent_id)
        body = {k: v for k, v in latest.items() if k not in ("seq", "id", "key_id", "key_mode", "prev_hash", "hash", "signature")}
        body.update(event="readback", readback=summary, created_at=self.clock().isoformat())
        self.ledger.append(body)
        return summary

    def reconcile_capture(self, order_ref: str) -> dict:
        """Cross-check one capture using only refund resources we hold ids for: the sum of the
        refunds PayPal confirms for this order must not exceed the captured amount, and each must be
        readable. Any unreadable refund makes the answer NOT ok (it is never treated as zero)."""
        order = self.orders.get(order_ref)
        if order is None:
            return {"ok": False, "reason": "UNKNOWN_ORDER"}
        try:
            cap = self.client.get_capture(order.capture_id)
            captured = to_minor(cap["amount"]["value"])
        except (TransportTimeout, PayPalHttpError, ValueError, KeyError, TypeError) as e:
            return {"ok": False, "reason": "CAPTURE_UNREADABLE", "detail": type(e).__name__}
        # The capture resource must be THIS order's capture, in the order's currency.
        cap_bad = [k for k, good in (("capture_id", cap.get("id") == order.capture_id),
                                     ("currency", (cap.get("amount") or {}).get("currency_code") == order.currency)) if not good]
        confirmed, refunds, unreadable, invalid = 0, [], [], []
        for iid, st in intent_states(self.ledger).items():
            rec = st.rec
            if rec["intent"]["payee"] != order.capture_id or not rec["payment_ref"]:
                continue
            try:
                d = self.client.get_refund(rec["payment_ref"])
                minor = to_minor(d["amount"]["value"])
            except (TransportTimeout, PayPalHttpError, ValueError, KeyError, TypeError):
                unreadable.append(rec["payment_ref"])
                continue
            # The same checks as read_back: the resource must be the refund we hold the id for, with a known status, our
            # amount and our currency. A resource that is not is never counted, and it makes the answer NOT ok.
            bad = refund_mismatches(d, rec["payment_ref"], rec["intent"]["amount_minor"], rec["intent"]["currency"])
            if bad:
                invalid.append({"refund_id": rec["payment_ref"], "mismatches": bad})
                continue
            refunds.append({"refund_id": rec["payment_ref"], "status": d.get("status"), "amount_minor": minor})
            if d.get("status") in ("COMPLETED", "PENDING"):
                confirmed += minor
        ok = not unreadable and not invalid and not cap_bad and confirmed <= captured
        return {"ok": ok, "order": order_ref, "captured_minor": captured, "confirmed_refunds_minor": confirmed,
                "refunds": refunds, "unreadable": unreadable, "invalid": invalid, "capture_mismatches": cap_bad,
                "mode": self.mode, "source": "refund resources by known id; no capture-level cumulative field is used"}

    def verify_all(self) -> dict:
        """Chain plus separately stored checkpoint for all three signed chains."""
        out = {}
        for name, ledger in (("receipts", self.ledger), ("decisions", self.decisions), ("policy", self.policies.ledger)):
            try:
                res = ledger.verify_trusted()
                out[name] = res.as_dict()
            except LedgerIntegrityError as e:
                out[name] = {"ok": False, "records": 0, "reason_code": e.reason_code, "first_broken_id": e.first_broken_id}
        out["ok"] = all(v["ok"] for v in out.values())
        return out
