"""Provider-status reconciliation: record what PayPal says about a refund, safely.

Rules: (1) a webhook is accepted only after signature verification; (2) the verified event is
persisted BEFORE anything else, so a read outage cannot lose it; (3) the refund is READ back from
PayPal and only that answer is used, never the payload: if the read fails the event stays queued
and the answer is 503, so PayPal retries and a stale payload can never release a reservation;
(4) status moves only by refund_status.advance (never by timestamp); (5) unknown refunds are
logged and ignored; (6) every applied change is a new signed receipt, and the status used for
money accounting is derived from those signed receipts; (7) refunds still PENDING are polled
without needing any webhook."""
from __future__ import annotations
import json
from datetime import datetime
from typing import Callable

from approval_receipt import Engine
from approval_receipt.engine import SUCCEEDED, SUBMITTING, UNKNOWN

from .ledgerview import intent_states
from .paypal_adapter import PayPalAdapter, PayPalHttpError, KNOWN_STATUSES, refund_mismatches
from .refund_status import advance, APPLIED, CONFLICT, PENDING as P_PENDING
from .store import Store
from .transport import TransportTimeout

_DROP = ("seq", "id", "key_id", "key_mode", "prev_hash", "hash", "signature")
READ_ERRORS = (TransportTimeout, PayPalHttpError, ValueError, AttributeError, KeyError)


class RefundSync:
    def __init__(self, engine: Engine, adapter: PayPalAdapter, store: Store, clock: Callable[[], datetime]):
        self.engine, self.adapter, self.store, self.clock = engine, adapter, store, clock

    def intent_for_refund(self, refund_id: str, custom_id: str | None = None) -> str | None:
        for r in reversed(self.engine.ledger.receipts()):
            if r["payment_ref"] == refund_id or r.get("provider_refund_id") == refund_id:
                return r["intent"]["id"]
        if custom_id:   # UNVERIFIED: the webhook resource carries the custom_id we sent (our intent id)
            r = self.engine.ledger.latest_for_intent(custom_id)
            if r is not None:
                self.adapter.remember(r["intent"]["idempotency_key"], refund_id)   # lets reconcile read this refund
                return r["intent"]["id"]
        return None

    def status(self, refund_id: str) -> str | None:
        """Provider status of a refund as recorded in SIGNED receipts."""
        iid = self.intent_for_refund(refund_id)
        st = intent_states(self.engine.ledger).get(iid) if iid else None
        return st.provider_status if st else None

    def apply_status(self, refund_id: str, incoming: str | None, source: str) -> str:
        iid = self.intent_for_refund(refund_id)
        if iid is None:
            return "UNKNOWN_REFUND"
        current = intent_states(self.engine.ledger)[iid].provider_status
        t = advance(current, incoming)
        if t.result == APPLIED:
            self._receipt(iid, refund_id, t.new, source, conflict=False)
        elif t.result == CONFLICT:
            self.store.data["conflicts"].append({"refund_id": refund_id, "kept": current, "seen": incoming,
                                                 "source": source})
            self.store.save()
            self._receipt(iid, refund_id, current, source, conflict=True)
        return t.result

    def sync_from_adapter(self, intent_id: str) -> None:
        """After an execution step: record the status PayPal returned with the refund id."""
        rec = self.engine.ledger.latest_for_intent(intent_id)
        if rec and rec["outcome"] == SUCCEEDED and rec["payment_ref"]:
            st = self.adapter.statuses.get(rec["payment_ref"])
            if st in KNOWN_STATUSES:
                self.apply_status(rec["payment_ref"], st, "response")

    def poll_pending(self) -> list[str]:
        """Read every accepted refund whose signed provider status is missing or PENDING, and
        apply what PayPal says. No webhook needed. A failed read leaves it for the next round."""
        done = []
        for iid, st in intent_states(self.engine.ledger).items():
            rec = st.rec
            if rec["outcome"] != SUCCEEDED or not rec["payment_ref"] or st.provider_status not in (None, P_PENDING):
                continue
            try:
                resource = self.adapter.client.get_refund(rec["payment_ref"])
                current = resource.get("status")
            except READ_ERRORS:
                continue
            if refund_mismatches(resource, rec["payment_ref"], rec["intent"]["amount_minor"], rec["intent"]["currency"]):
                self._mismatch(rec["payment_ref"], "poll")
                continue
            if current in KNOWN_STATUSES and self.apply_status(rec["payment_ref"], current, "poll") == APPLIED:
                done.append(iid)
        return done

    def _mismatch(self, refund_id: str, source: str) -> None:
        """The refund resource PayPal returned is not the refund we hold terms for. Nothing is applied; kept for a person."""
        self.store.data["conflicts"].append({"refund_id": refund_id, "kept": None, "seen": "RESOURCE_MISMATCH", "source": source})
        self.store.save()

    def _receipt(self, iid: str, refund_id: str, status: str | None, source: str, conflict: bool) -> None:
        rec = self.engine.ledger.latest_for_intent(iid)
        body = {k: v for k, v in rec.items() if k not in _DROP}
        body.update(event="provider_status", provider_status=status, provider_status_source=source,
                    provider_refund_id=refund_id, created_at=self.clock().isoformat(),
                    flags=sorted(set(body["flags"]) | ({"PROVIDER_STATUS_CONFLICT"} if conflict else set())))
        self.engine.ledger.append(body)


class WebhookProcessor:
    HANDLED_RESOURCE = "refund"

    def __init__(self, sync: RefundSync, webhook_id: str):
        self.sync, self.webhook_id = sync, webhook_id

    def process(self, headers: dict, body: bytes) -> tuple[int, dict]:
        """Returns (http_status, result). 2xx tells PayPal to stop retrying (UNVERIFIED detail
        from the webhook overview: 2xx required; retries up to 25 times over 3 days)."""
        s = self.sync
        try:
            ok = s.adapter.client.verify_webhook(headers, body, self.webhook_id)
        except (TransportTimeout, PayPalHttpError):
            return 503, {"result": "VERIFY_UNAVAILABLE"}      # not 2xx: PayPal will send it again
        except ValueError:
            return 400, {"result": "BAD_BODY"}
        if not ok:
            return 400, {"result": "INVALID_SIGNATURE"}
        try:
            event = json.loads(body)
            event_id = event["id"]
            if not isinstance(event_id, str) or not isinstance(event.get("resource") or {}, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return 400, {"result": "BAD_BODY"}
        known = s.store.events.get(event_id)
        if known and known["state"] == "DONE":
            return 200, {"result": "DUPLICATE_EVENT"}
        if not known:   # persist the verified event before acting on it
            s.store.events[event_id] = {"state": "VERIFIED", "body": body.decode("utf-8", "replace"),
                                        "received_at": s.clock().isoformat()}
            s.store.save()
        return self._handle(event_id, event)

    def retry_queued(self) -> list[str]:
        """Re-run verified events whose read-back failed earlier. Already verified, so no new
        signature call is needed."""
        out = []
        for event_id, ev in list(self.sync.store.events.items()):
            if ev["state"] == "VERIFIED":
                code, res = self._handle(event_id, json.loads(ev["body"]))
                if code == 200:
                    out.append(event_id)
        return out

    def _handle(self, event_id: str, event: dict) -> tuple[int, dict]:
        s = self.sync
        res = event.get("resource") or {}
        if event.get("resource_type") != self.HANDLED_RESOURCE or not res.get("id"):
            return self._done(event_id, "IGNORED_EVENT")
        refund_id = str(res["id"])
        iid = s.intent_for_refund(refund_id, res.get("custom_id"))
        if iid is None:
            return self._done(event_id, "UNKNOWN_REFUND")
        rec = s.engine.ledger.latest_for_intent(iid)
        if rec and rec["outcome"] in (SUBMITTING, UNKNOWN):
            s.engine.reconcile(iid)                          # read the refund, same key, no new payment
        try:                                                  # PayPal's current answer, never the payload
            resource = s.adapter.client.get_refund(refund_id)
            current = resource.get("status")
        except READ_ERRORS:
            return 503, {"result": "READBACK_UNAVAILABLE"}   # event stays queued; nothing applied
        if rec and refund_mismatches(resource, refund_id, rec["intent"]["amount_minor"], rec["intent"]["currency"]):
            s._mismatch(refund_id, "webhook_read")
            return self._done(event_id, "RESOURCE_MISMATCH")
        return self._done(event_id, s.apply_status(refund_id, current, "webhook_read"))

    def _done(self, event_id: str, result: str) -> tuple[int, dict]:
        ev = self.sync.store.events.setdefault(event_id, {"body": ""})
        ev.update(state="DONE", result=result, body="" if result == "IGNORED_EVENT" else ev.get("body", ""))
        self.sync.store.save()
        return 200, {"result": result}
