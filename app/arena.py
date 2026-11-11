"""The Injection Arena, the Policy Builder front, and the Refund Court transcript.

The arena is NOT a separate program. Every attempt goes through the SAME refund authority
(app/authority.py) that the refund inbox uses: same gate, same policy source, same reservations, same
core engine, same adapter code. What differs is only the provider adapter behind it: the arena's
authority sits on its own isolated ledger and a FAKE PayPal, so strangers can attack it freely without
touching the owner's ledger, the daily budget of the real desk, or any sandbox credential.

Everything here is a SIMULATION: "money" means fake-provider refunds on the sample orders. No model is
called unless the visitor chose the model reader with ARENA_READER=model.

Because no owner is online in the arena, a request the gate holds for the owner is recorded as held and
then closes at once (the approval window is zero), so nobody's attempt can lock up an order for others.
There is no global tightening of the limits: one visitor cannot lower them for the next.

State is DERIVED from signed receipts on every call (the authority's decision records and the engine's
receipts), never from an editable file."""
from __future__ import annotations
import hmac
import os
import threading
from dataclasses import dataclass
from datetime import timedelta

from approval_receipt import ALLOW, ESCALATE, DENY
from approval_receipt.engine import SUCCEEDED
from approval_receipt.receipts import digest

from .agent import MessageRejected
from .arena_presets import BY_ID, SEED_REFUNDS, LABELS, public_view
from .arena_reader import Reader, RuleReader, REFUND
from .authority import ROUTE_AUTO, ROUTE_REVIEW, AUTO_AGENT
from . import fx
from .extractor import FALLBACK_LABEL
from .fixture_transport import FixturePayPal
from .ledgerview import intent_states, reserved_today
from .refund_status import FAILED as P_FAILED, CANCELLED as P_CANCELLED
from .money import fmt
from .scoreboard import compute
from .service import Service
from .stamp import arena_stamp, execution_view
from .strings import S

MAX_CHARS = 1000
# The stamp kind (built from the SIGNED execution record) -> the label in the detail table's "Allowed" column.
ACTION_BY_KIND = {"approved": "RELEASED", "held": "HELD_FOR_OWNER", "refused": "BLOCKED", "unknown": "UNKNOWN",
                  "failed": "FAILED", "not_sent": "NOT_SENT", "mismatch": "MISMATCH"}
RATE_PER_HOUR = 600           # whole arena, memory only


@dataclass
class ArenaPrefetch:
    """The arena reader's proposal for one message, taken before the app-wide lock. Single use;
    an unexpected failure of the reader is carried in `error` and raised once by Arena.attempt."""
    text: str
    order_ref: str
    proposal: object = None
    error: BaseException | None = None
    used: bool = False


class Arena:
    def __init__(self, svc: Service, reader: Reader | None = None):
        self.svc, self.reader = svc, reader or RuleReader()
        self.clock = svc.clock
        self._hits: list = []
        self._lock = threading.RLock()
        captures = {o.capture_id: o.total_minor for o in svc.orders.by_ref.values()}
        self.provider = FixturePayPal(captures)             # the FAKE adapter, always, whatever the desk uses
        for order_ref, minor in SEED_REFUNDS.items():       # earlier refunds the fake provider already knows about
            o = svc.orders.get(order_ref)
            if o:
                self.provider.refunds["SEED-" + order_ref] = {"capture_id": o.capture_id, "minor": minor,
                                                              "custom_id": "seed", "status": "COMPLETED"}
        self.sim = Service(os.path.join(svc.data_dir, "arena_sim"), self.provider, svc.orders, clock=svc.clock,
                           key=svc.signing_key, mode="fixture", approval_ttl=timedelta(0),
                           checkpoint_dir=os.path.join(svc.checkpoint_dir, "arena_sim"), policies=svc.policies,
                           channel="arena", prior_refunded=lambda ref: SEED_REFUNDS.get(ref, 0))
        self.authority = self.sim.authority
        self.log = self.sim.decisions

    # ---- derived state ----------------------------------------------------------------------------
    def _attempts(self) -> list[dict]:
        return [d for d in self.authority.decisions() if d.get("attempt_id")]

    def active_policy(self):
        return self.svc.policies.active()

    def remaining(self, order_ref: str, states: dict | None = None) -> int:
        o = self.svc.orders.get(order_ref)
        from .ledgerview import held_on_capture
        held, _ = held_on_capture(self.sim.ledger, o.capture_id, states)
        return max(0, o.total_minor - SEED_REFUNDS.get(order_ref, 0) - held)

    def budget(self, policy) -> dict:
        daily = reserved_today(self.sim.ledger, AUTO_AGENT, self.clock())
        return {"per_order_allowance_minor": policy.per_order_cap_minor, "escalate_ceiling_minor": policy.escalate_ceiling_minor,
                "daily_budget_minor": policy.daily_budget_minor, "daily_automatic_minor": daily,
                "daily_room_minor": max(0, policy.daily_budget_minor - daily)}

    def scoreboard(self, states: dict | None = None) -> dict:
        states = states if states is not None else intent_states(self.sim.ledger)
        totals = {o.capture_id: o.total_minor for o in self.svc.orders.by_ref.values()}
        return compute(self.authority.decisions(), states, LABELS, totals,
                       {o.order_ref: o.capture_id for o in self.svc.orders.by_ref.values()})

    def orders_view(self, states: dict) -> list[dict]:
        return [{"order_ref": o.order_ref, "item": o.item, "total_minor": o.total_minor,
                 "remaining_minor": self.remaining(o.order_ref, states)} for o in self.svc.orders.by_ref.values()]

    def state(self) -> dict:
        policy, prec = self.active_policy()
        states = intent_states(self.sim.ledger)
        attempts = self._attempts()
        return {"reader": {"kind": self.reader.kind, "name": self.reader.name},
                "policy": {**policy.as_dict(), "version": prec["version"], "hash": prec["policy_hash"]},
                "budget": self.budget(policy), "scoreboard": self.scoreboard(states),
                "orders": self.orders_view(states), "presets": public_view(),
                "recent": [self.summary(a, states) for a in reversed(attempts[-12:])]}

    def _money(self, a: dict, states: dict) -> tuple[int, int]:
        """(released, held) for one attempt, from the SIGNED execution receipt. Released: the signed outcome says the
        fake provider accepted it, for the amount the receipt signed (not the amount a decision record or an unsigned
        file says). Held: the gate parked it for an owner who is not online in the arena."""
        st = states.get(a["intent_id"]) if a.get("intent_id") else None
        if st is not None and st.rec["outcome"] == SUCCEEDED and st.provider_status not in (P_FAILED, P_CANCELLED):
            return st.rec["intent"]["amount_minor"], 0
        if a["route"] == ROUTE_REVIEW:
            return 0, a.get("amount_minor") or 0
        return 0, 0

    def summary(self, a: dict, states: dict) -> dict:
        released, _ = self._money(a, states)
        return {"id": a["attempt_id"], "order": a["order"], "decision": a["gate"]["decision"], "rule_id": a["gate"]["rule_id"],
                "preset": a.get("preset"), "receipt_id": a["id"], "released_minor": released, "text": a["text"][:90]}

    # ---- the attempt -----------------------------------------------------------------------------
    def _throttle(self) -> None:
        now = self.clock()
        self._hits = [t for t in self._hits if now - t < timedelta(hours=1)]
        if len(self._hits) >= RATE_PER_HOUR:
            raise MessageRejected("RATE_LIMITED")
        self._hits.append(now)

    def prefetch(self, text: str, order_ref: str):
        """The reader (a model, when ARENA_READER=model) reads the message BEFORE the app-wide lock is taken. The same
        refusals as attempt() come first, and the arena's throttle slot is taken before the reader is asked. Returns an
        ArenaPrefetch, which attempt() uses once as it is. An unexpected failure of the reader is carried
        in it and raised once by attempt(), never retried under the lock."""
        text = (text or "").strip()
        if not text:
            raise MessageRejected("EMPTY_MESSAGE")
        if len(text) > MAX_CHARS:
            raise MessageRejected("MESSAGE_TOO_LONG")
        order = self.svc.orders.get(order_ref)
        if order is None:
            raise MessageRejected("UNKNOWN_ORDER")
        text, _ = fx.convert_text(text)          # a figure in another currency becomes dollars at the demo rate (app/fx.py)
        with self._lock:
            self._throttle()
        pre = ArenaPrefetch(text, order_ref)
        try:
            pre.proposal = self.reader.read(text, order)
        except Exception as e:
            pre.error = e
        return pre

    def attempt(self, text: str, order_ref: str, preset_id: str | None = None, prefetched=None) -> dict:
        text = (text or "").strip()
        if not text:
            raise MessageRejected("EMPTY_MESSAGE")
        if len(text) > MAX_CHARS:
            raise MessageRejected("MESSAGE_TOO_LONG")
        order = self.svc.orders.get(order_ref)
        if order is None:
            raise MessageRejected("UNKNOWN_ORDER")
        original = text
        text, conversions = fx.convert_text(text)
        with self._lock:
            pre = None
            if (isinstance(prefetched, ArenaPrefetch) and not prefetched.used and prefetched.text == text
                    and prefetched.order_ref == order_ref):
                prefetched.used = True
                pre = prefetched
            if pre is None:
                self._throttle()
            elif pre.error is not None:
                raise pre.error
            proposal = pre.proposal if pre is not None else self.reader.read(text, order)
            attempt_id = f"A-{len(self._attempts()) + 1:04d}"
            name = FALLBACK_LABEL if "MODEL_OUTPUT_REJECTED" in (proposal.notes or ()) else self.reader.name
            extra = {"attempt_id": attempt_id, "preset": preset_id if preset_id in BY_ID else None,
                     "reader": {"kind": self.reader.kind, "name": name}}
            if conversions:
                extra["fx"] = {"original_text": original, "conversions": conversions}
            res = self.authority.authorize(text, order_ref, proposal, extra=extra)
            self.sim._after({"intent_id": res.intent_id})
            if res.route == ROUTE_REVIEW:
                self.sim.expire_due()          # no owner online in the arena: the held request closes now
        return self.attempt_view(self.find_attempt(attempt_id))

    def attempt_view(self, a: dict) -> dict:
        policy, _ = self.active_policy()
        states = intent_states(self.sim.ledger)
        order = self.svc.orders.get(a["order"])
        prop, gate = a["proposal"], a["gate"]
        decision = gate["decision"]
        released, held = self._money(a, states)
        st = states.get(a["intent_id"]) if a.get("intent_id") else None
        stamp = arena_stamp(decision=decision, rule_id=gate["rule_id"], reason=S("arena.rule." + gate["rule_id"]),
                            execution=execution_view(st.rec if st else None, st.provider_status if st else None),
                            suggested_minor=prop["amount_minor"] if prop["action"] == REFUND else None)
        # The "allowed" column says what the SIGNED execution says happened, not what the gate permitted: a permitted
        # send whose answer was lost is UNKNOWN, never "Released", and its amount is not counted as zero either.
        allowed_action = ACTION_BY_KIND[stamp["kind"]]
        unsure = stamp["kind"] in ("unknown", "mismatch")
        allowed_amount = allowed_action if unsure else released
        rows = [
            {"key": "action", "proposed": prop["action"], "allowed": allowed_action,
             "changed": not (prop["action"] == REFUND and stamp["kind"] == "approved")},
            {"key": "amount", "proposed": prop["amount_minor"], "allowed": allowed_amount,
             "changed": (prop["amount_minor"] or 0) != allowed_amount},
            {"key": "reason", "proposed": prop["reason_code"],
             "allowed": prop["reason_code"] if decision == ALLOW else None,
             "changed": decision != ALLOW},
        ]
        return {"id": a["attempt_id"], "order": a["order"], "item": order.item if order else None,
                "total_minor": order.total_minor if order else None,
                "text": (a.get("fx") or {}).get("original_text") or a["text"], "fx": (a.get("fx") or {}).get("conversions") or [],
                "preset": a.get("preset"),
                "reader": {**a["reader"], "proposal": prop}, "gate": gate, "without_flags": a["without_flags"], "diff": rows,
                "released_minor": released, "held_minor": held, "route": a["route"],
                "stamp": stamp,
                "policy_version": a["policy_version"],
                "receipt": {"id": a["id"], "seq": a["seq"], "hash": a["hash"], "at": a["at"],
                            "signature_ok": self._record_ok(a)},
                "budget": self.budget(policy), "scoreboard": self.scoreboard(states)}

    def find_attempt(self, attempt_id: str) -> dict | None:
        return next((a for a in self._attempts() if a["attempt_id"] == attempt_id), None)

    # ---- receipts -------------------------------------------------------------------------------
    def _record_ok(self, rec: dict) -> bool:
        body = {k: v for k, v in rec.items() if k not in ("hash", "signature")}
        return rec.get("hash") == digest(body) and hmac.compare_digest(str(rec.get("signature")), self.log.mac(rec["hash"]))

    def receipt(self, receipt_id: str) -> dict | None:
        rec = next((r for r in self.log.records() if r["id"] == receipt_id and r["event"] != "genesis"), None)
        if rec is None:
            return None
        return {"id": rec["id"], "event": rec["event"], "hash": rec["hash"], "prev_hash": rec["prev_hash"],
                "signature_ok": self._record_ok(rec), "at": rec["at"], "record": rec,
                "key_mode": rec.get("key_mode")}

    def verify(self) -> dict:
        v = self.sim.verify_all()
        bad = next((x for k, x in v.items() if k != "ok" and not x["ok"]), None)
        if bad:
            return {"ok": False, "records": bad["records"], "reason_code": bad.get("reason_code"),
                    "first_broken_id": bad.get("first_broken_id")}
        return {"ok": True, "records": sum(x["records"] for k, x in v.items() if k != "ok"), "reason_code": None,
                "first_broken_id": None}

    # ---- policy builder (the policy lives in one place: the policy store) ---------------------------
    def policy_state(self) -> dict:
        return self.svc.policies.state()

    def preview_policy(self, text: str) -> dict:
        return self.svc.policies.preview(text)

    def save_policy(self, text: str, who: str) -> dict:
        res = self.svc.policies.save(text, who)
        if res["ok"]:
            self.svc.authority.sync_mandates()
            self.authority.sync_mandates()
        return res

    def diff_versions(self, a: int, b: int) -> dict | None:
        return self.svc.policies.diff_versions(a, b)

    # ---- refund court -----------------------------------------------------------------------------
    def court(self, attempt_id: str) -> dict | None:
        rec = self.find_attempt(attempt_id)
        if rec is None:
            return None
        order = self.svc.orders.get(rec["order"])
        prop, gate = rec["proposal"], rec["gate"]
        lines = [
            {"role": "CLERK", "kind": "say", "text": S("arena.court.case", attempt=rec["attempt_id"], order=rec["order"],
                                                      item=order.item if order else "", total=fmt(order.total_minor) if order else "")},
            {"role": "BUYER", "kind": "quote", "text": rec["text"]},
        ]
        if prop["action"] == REFUND:
            reason = S("issue." + prop["reason_code"]) if prop["reason_code"] else S("arena.court.no_reason")
            lines.append({"role": "READER", "kind": "say", "text": S("arena.court.reader_proposes", amount=fmt(prop["amount_minor"]),
                                                                    reason=reason)})
            q = prop["quotes"].get("issue") or prop["quotes"].get("refund_intent")
            if q:
                lines.append({"role": "READER", "kind": "quote", "text": q})
        else:
            lines.append({"role": "READER", "kind": "say", "text": S("arena.court.reader_" + prop["action"].lower())})
        for f in gate["flags"]:
            lines.append({"role": "CLERK", "kind": "flag", "text": S("arena.court.flag", rule=f["rule"],
                                                                     category=S("arena.flag." + f["category"])),
                          "quote": f["phrase"]})
        for t in gate["trace"]:
            if t["result"] == "HIT":
                lines.append({"role": "JUDGE", "kind": "rule", "rule": t["rule"], "result": "HIT",
                              "text": S("arena.rule." + t["rule"])})
        passed = [t["rule"] for t in gate["trace"] if t["result"] == "PASS"]
        if passed:
            lines.append({"role": "JUDGE", "kind": "passed", "text": S("arena.court.passed", count=len(passed),
                                                                      rules=", ".join(passed))})
        wf = rec["without_flags"]
        if wf["decision_changed"]:
            lines.append({"role": "JUDGE", "kind": "say", "text": S("arena.court.counterfactual",
                                                                   decision=S("arena.decision." + wf["decision"]),
                                                                   rule=wf["rule_id"])})
        verdict = S("arena.court.verdict_" + gate["decision"], rule=gate["rule_id"], amount=fmt(prop["amount_minor"] or 0),
                    code=gate["reason_code"])
        lines.append({"role": "VERDICT", "kind": "verdict", "decision": gate["decision"], "rule": gate["rule_id"],
                      "text": verdict})
        lines.append({"role": "RECEIPT", "kind": "receipt", "id": rec["id"], "hash": rec["hash"],
                      "text": S("arena.court.receipt", id=rec["id"], hash=rec["hash"][:16])})
        return {"id": rec["attempt_id"], "decision": gate["decision"], "rule_id": gate["rule_id"], "lines": lines,
                "receipt_id": rec["id"]}
