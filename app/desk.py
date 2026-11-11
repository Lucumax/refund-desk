"""The Refund Desk: the ONE screen. A buyer message goes in at the top; the reader's proposal, the
authority's decision, the owner's approve or deny, the provider's answer and the read-back come out
below it, for one request at a time. It is a thin layer over the same service the inbox uses
(Service.message -> RefundAuthority). Nothing here decides money.

Owner-only (session and CSRF): it can send a message "as the buyer" using the order's own capability,
which is what a shop owner testing their own setup does. A client names a scenario by id; the server
picks the text, the order and the reader. A client can never hand over a proposal."""
from __future__ import annotations

from dataclasses import dataclass

from . import scripted, views
from .extractor import ModelExtractor
from .fixture_transport import FixturePayPal
from .ledgerview import intent_states, held_on_capture
from .scoreboard import compute
from .service import Service
from .strings import S

READER_CONFIGURED, READER_SCRIPTED, READER_COMPROMISED = "configured", "scripted_model", "compromised"


@dataclass
class SendPlan:
    """What a desk send will do, worked out WITHOUT side effects, so the model can read the message before the app-wide lock is
    taken. The server resolves it once, reads with these exact objects, and hands the same plan to send()."""
    order: str
    text: str
    label: str | None = None
    reader: object = None
    extractor: object = None
    reader_label: str | None = None
    fault: bool = False


class Desk:
    def __init__(self, svc: Service):
        self.svc = svc

    # ---- state ---------------------------------------------------------------------------------------
    def orders(self, states: dict) -> list[dict]:
        out = []
        for o in self.svc.orders.by_ref.values():
            held, _ = held_on_capture(self.svc.ledger, o.capture_id, states)
            out.append({"order_ref": o.order_ref, "item": o.item, "total_minor": o.total_minor,
                        "remaining_minor": max(0, o.total_minor - held)})
        return out

    def scoreboard(self, states: dict) -> dict:
        totals = {o.capture_id: o.total_minor for o in self.svc.orders.by_ref.values()}
        return compute(self.svc.authority.decisions(), states, scripted.LABELS, totals,
                       {o.order_ref: o.capture_id for o in self.svc.orders.by_ref.values()})

    def state(self) -> dict:
        svc = self.svc
        states = intent_states(svc.ledger)
        pol, prec = svc.policies.active()
        inbox = views.inbox_view(svc)
        sent = sum(1 for r in svc.ledger.receipts() if r["event"] == "dispatch")     # from signed receipts, not a counter
        with_id = sum(1 for st in states.values() if st.rec["payment_ref"])
        return {"mode": svc.mode, "provider": "fake" if svc.mode == "fixture" else "sandbox",
                "refund_requests_sent": sent, "refunds_with_id": with_id,
                "reader": {"configured": svc.reader_name, "scripted": "scripted test model (canned replies, not a live model)",
                           "compromised": scripted.CompromisedReader.name},
                "policy": {**pol.as_dict(), "version": prec["version"], "hash": prec["policy_hash"]},
                "enforced": svc.enforced_limits(),
                "orders": self.orders(states), "scenarios": [sc for sc in scripted.public_view() if svc.orders.get(sc["order"])],
                "faults": self.faults(),
                "requests": inbox["items"][:8], "waiting": inbox["waiting"], "rule_sentence": inbox["rule_sentence"],
                "scoreboard": self.scoreboard(states)}

    def faults(self) -> list[dict]:
        """Simulated PayPal faults. Offered only on the fixture, never in sandbox mode."""
        if not isinstance(self.svc.client.transport, FixturePayPal):
            return []
        return [f for f in scripted.fault_view() if self.svc.orders.get(f["order"])]

    # ---- send ----------------------------------------------------------------------------------------
    def plan(self, scenario_id: str | None, order: str | None, text: str | None) -> SendPlan:
        """Resolve a send into order, text and readers. No side effect: a simulated fault is only noted here, armed by send()."""
        svc = self.svc
        label = reader = extractor = None
        reader_label = None
        fault = False
        if scenario_id:
            sc = scripted.BY_ID.get(scenario_id)
            if sc is None:
                raise KeyError("UNKNOWN_SCENARIO")
            order, text, label = sc["order"], sc["text"], sc["id"]
            if sc.get("fault"):
                if not isinstance(svc.client.transport, FixturePayPal) or sc["fault"] != "timeout_after_accept":
                    raise KeyError("UNKNOWN_SCENARIO")        # a simulated fault never reaches a real PayPal call
                fault = True
            if sc["reader"] == READER_SCRIPTED:
                if not isinstance(svc.agent.extractor, ModelExtractor):     # no model set up: the scripted test model reads it
                    extractor = ModelExtractor(scripted.scripted_model_completer())
                    reader_label = S("desk.reader.scripted")
            elif sc["reader"] == READER_COMPROMISED:
                reader = scripted.CompromisedReader(sc["proposal"])
        return SendPlan(str(order or ""), str(text or ""), label, reader, extractor, reader_label, fault)

    def send(self, scenario_id: str | None, order: str | None, text: str | None, prefetched=None, plan: SendPlan | None = None) -> dict:
        svc = self.svc
        plan = plan or self.plan(scenario_id, order, text)
        armed = False
        if plan.fault:
            svc.client.transport.timeout_after_accept = True  # one-shot: only this request's refund loses its answer
            armed = True
        try:
            req = svc.message(plan.text, plan.order, svc.buyer_capability(plan.order),
                              reader=plan.reader, extractor=plan.extractor, label=plan.label, reader_label=plan.reader_label,
                              prefetched=prefetched)
        finally:
            if armed:       # the fault belongs to THIS request: if it did not dispatch (a duplicate, a refusal, an error) it must not wait for the next one
                svc.client.transport.timeout_after_accept = False
        view = views.proposal_view(svc, req, detail=True)
        return {"request": view, "scenario": scenario_id}

    # ---- re-check an unresolved refund (never a blind retry) ----------------------------------------------
    def _signed_intent(self, request_id: str) -> str:
        """The intent the SIGNED records tie to this request (never the request file's own pointer)."""
        from approval_receipt import ApprovalError
        req = self.svc.store.requests.get(request_id)
        if not req:
            raise KeyError("NOT_FOUND")
        iid, err = views.signed_target(self.svc, req)
        if err == "NOT_FOUND":
            raise KeyError("NOT_FOUND")
        if err:
            raise ApprovalError(err)
        return iid

    def recheck(self, request_id: str) -> dict:
        return self.svc.recheck(self._signed_intent(request_id))

    # ---- the provider's own record -------------------------------------------------------------------------
    def readback(self, request_id: str) -> dict:
        iid = self._signed_intent(request_id)
        rb = self.svc.read_back(iid)
        rb["label"] = S("desk.readback.fixture") if self.svc.mode == "fixture" else S("desk.readback.sandbox")
        rb["capture"] = self.svc.reconcile_capture(self.svc.store.requests[request_id]["order_ref"]) if rb.get("ok") else None
        return rb
