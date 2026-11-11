"""The refund agent. It reads a buyer message through a pluggable extractor (or, for tests and the
compromised-reader harness, a Reader that hands over a ready proposal) and PROPOSES a refund. It
never decides money: it hands the proposal to the refund authority (app/authority.py), whose gate
returns ALLOW, ESCALATE or DENY and which alone talks to the core engine. Machine codes only in this
module; text lives in copy/strings.draft.json and is applied in views.py.

Safety rules kept here:
- The order comes from the buyer's capability (checked by the service), not from free text.
- A refund needs words in the message for the issue and an explicit refund request, plus an
  explicit amount (or an explicit full-refund phrase). Missing or ambiguous: ask the buyer.
- Each business operation gets a random UUID idempotency key, stored with the request BEFORE
  dispatch and never derived from a counter or regenerated on retry.
- Money already held on an order is computed from signed receipts."""
from __future__ import annotations
import secrets
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable

from approval_receipt import Intent, Engine

from . import fx
from .arena_reader import Proposal, Reader, REFUND, NONE as ACTION_NONE
from .consent import binding_problem, raw_amounts, canonical_amounts
from .extractor import (Extractor, Facts, PURPOSES, foreign_currency, EXPLICIT, FULL, AMBIGUOUS, NONE as AMOUNT_NONE, normalize_order_ref,
                        has_negative_amount, FALLBACK_LABEL)
from .ledgerview import held_on_capture
from .orders import OrderBook
from .store import Store

AGENT_ID = "refund-agent"
MAX_MESSAGE_CHARS = 2000

SUBMITTED, NEEDS_INFO, DUPLICATE = "SUBMITTED", "NEEDS_INFO", "DUPLICATE"

PER_ORDER_PER_HOUR = 5        # default limits, the owner sets them
GLOBAL_PER_HOUR = 60


@dataclass
class Prefetch:
    """What the model said about one message, obtained BEFORE any app-wide lock is taken. It holds only
    readings: facts from the extractor, a proposal from a Reader, a consent reading. They depend on the text, on the readers in force
    and, for a Reader's "full refund", on the order's fixed total; never on balances, policy, capability or session. It decides
    nothing. The decision (order binding, balances, the gate, the signed record) is still made under the lock, from the same
    inputs as before. The throttle slot was taken when this was made, so a flood cannot spend model calls it was refused.
    It is single use (`used`), bound to the readers actually in force (`eff_*`), and an unexpected
    failure of the model stage is carried (`error`) and raised once, in the decision, instead of being retried under the lock."""
    text: str
    order_ref: str
    reader: object = None
    extractor: object = None
    consent_reader: object = None
    facts: object = None
    facts_exchange: dict | None = None
    proposal: Proposal | None = None
    consent_done: bool = False
    reading: object = None
    consent_exchange: dict | None = None
    eff_extractor: object = None          # the extractor and consent reader in force when the model was asked (defaults resolved)
    eff_consent: object = None
    error: BaseException | None = None    # an unexpected failure of the model stage, raised once by the decision
    used: bool = False

    def matches(self, text: str, order_ref: str, reader, extractor, consent_reader, eff_extractor=None, eff_consent=None) -> bool:
        return (self.text == text and self.order_ref == order_ref and self.reader is reader and self.extractor is extractor
                and self.consent_reader is consent_reader and self.eff_extractor is eff_extractor and self.eff_consent is eff_consent)


class MessageRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RefundAgent:
    def __init__(self, engine: Engine, extractor: Extractor, orders: OrderBook, store: Store,
                 clock: Callable[[], datetime],
                 token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(12),
                 key_factory: Callable[[], str] = lambda: str(uuid.uuid4()), authority=None, consent_reader=None):
        if authority is None:
            raise ValueError("the refund agent needs the refund authority")
        self.authority = authority
        self.consent_reader = consent_reader     # a ConsentReader (app/consent_reader.py) or None: then the rules read consent alone
        self.engine, self.extractor, self.orders, self.store = engine, extractor, orders, store
        self.clock, self.token_factory, self.key_factory = clock, token_factory, key_factory
        self._lock = threading.RLock()
        self._hits: dict[str, list[datetime]] = {}     # throttle, memory only

    def _working_text(self, text: str) -> tuple[str, list]:
        """(the text every later step reads, the conversions made). Demo mode only: a figure in another currency becomes dollars at the
        fixed demo rate. With demo_fx off the buyer's words are read exactly as written."""
        return fx.convert_text(text) if self.authority.demo_fx else (text, [])

    def _hold_foreign_currency(self, req: dict) -> bool:
        """Not in demo mode: any message that names a currency other than the US dollar waits for a person. No proposal is built, so no
        automatic refund and no owner card with an amount that was never in the buyer's currency."""
        if self.authority.demo_fx or not foreign_currency(req["text"]):
            return False
        req["code"] = "FOREIGN_CURRENCY_HELD"
        return True

    def _throttle(self, order_ref: str) -> None:
        now = self.clock()
        cutoff = now - timedelta(hours=1)
        for k in list(self._hits):
            self._hits[k] = [t for t in self._hits[k] if t > cutoff]
            if not self._hits[k]:
                del self._hits[k]
        total = sum(len(v) for v in self._hits.values())
        if len(self._hits.get(order_ref, [])) >= PER_ORDER_PER_HOUR or total >= GLOBAL_PER_HOUR:
            raise MessageRejected("RATE_LIMITED")
        self._hits.setdefault(order_ref, []).append(now)

    def _new_ids(self) -> tuple[int, str]:
        while True:
            n = self.store.next_seq()
            iid = f"I-{n:04d}"
            if self.engine.ledger.latest_for_intent(iid) is None:
                return n, iid

    @staticmethod
    def _read_consent(consent_reader, text: str) -> tuple[object, dict | None]:
        """One call to the consent reader: (reading or None, the exchange for the evidence). Never raises."""
        try:
            reading = consent_reader.read(text)
        except Exception:
            reading = None
        last = getattr(consent_reader, "last", None)
        exchange = None
        if last:
            exchange = {"verdict": last.get("verdict"), "raw_reply": str(last.get("raw_reply", ""))[:1200],
                        "instructions_separate": not last.get("joined", True)}
        return reading, exchange

    def prefetch(self, text: str, order_ref: str, reader: Reader | None = None, extractor: Extractor | None = None,
                 consent_reader=None) -> Prefetch:
        """Take the throttle slot, then ask the model(s) what they read in the message, with no app-wide lock held. Raises
        MessageRejected for an empty or oversized message or a throttled order, exactly as handle_message would, and BEFORE any model
        call. The caller has already checked the buyer's capability (Service.prefetch)."""
        text = (text or "").strip()
        if not text:
            raise MessageRejected("EMPTY_MESSAGE")
        if len(text) > MAX_MESSAGE_CHARS:
            raise MessageRejected("MESSAGE_TOO_LONG")
        text, _ = self._working_text(text)       # demo mode only: figures in another currency become dollars at the demo rate (app/fx.py)
        with self._lock:
            self._throttle(order_ref)
        cr = consent_reader if consent_reader is not None else self.consent_reader
        ex = extractor or self.extractor
        pre = Prefetch(text, order_ref, reader, extractor, consent_reader, eff_extractor=ex, eff_consent=cr)
        try:
            wants_consent = False
            if reader is not None:
                try:
                    proposal = reader.read(text, self.orders.get(order_ref))
                except Exception:
                    proposal = Proposal(ACTION_NONE, None, None, "reader", notes=("READER_ERROR",))
                pre.proposal = proposal
                wants_consent = (proposal.action == REFUND and (proposal.amount_minor or 0) > 0
                                 and proposal.reason_code in PURPOSES and proposal.consent is None)
            else:
                pre.facts = ex.extract(text)
                last = getattr(ex, "last", None)
                if last:
                    pre.facts_exchange = dict(last)
                wants_consent = bool(pre.facts.refund_intent and pre.facts.issue_code in PURPOSES)
            if wants_consent and cr is not None:
                pre.reading, pre.consent_exchange = self._read_consent(cr, text)
                pre.consent_done = True
        except Exception as e:           # the slot is spent: keep the failure for the decision to raise ONCE, never retry the model
            pre.error = e
        return pre

    def handle_message(self, text: str, order_ref: str, reader: Reader | None = None,
                       extractor: Extractor | None = None, label: str | None = None,
                       reader_label: str | None = None, consent_reader=None, prefetched: Prefetch | None = None) -> dict:
        """`order_ref` is the order the buyer's capability proves. Never taken from the text.
        With a `reader`, its proposal is used as it is (no extractor, no clarifying questions about
        wording): this is how a scripted or compromised reader is put in front of the authority.
        `extractor` replaces the default extractor for this one message; `label` names the scenario
        (for the scoreboard) and `reader_label` the reader (for the screen). Neither affects a decision."""
        text = (text or "").strip()
        if not text:
            raise MessageRejected("EMPTY_MESSAGE")
        if len(text) > MAX_MESSAGE_CHARS:
            raise MessageRejected("MESSAGE_TOO_LONG")
        original = text
        text, conversions = self._working_text(text)    # what every later step reads (demo mode converts); the buyer's own words are kept in `original_text`
        with self._lock:
            pre = None
            if (prefetched is not None and not prefetched.used
                    and prefetched.matches(text, order_ref, reader, extractor, consent_reader,
                                           extractor or self.extractor,
                                           consent_reader if consent_reader is not None else self.consent_reader)):
                prefetched.used = True             # single use: a replayed prefetch is a message without a slot
                pre = prefetched
            if pre is None:
                self._throttle(order_ref)          # a prefetch has already taken this message's slot
            elif pre.error is not None:
                raise pre.error                    # the model stage failed unexpectedly: once, here, with no second call
            n, iid = self._new_ids()
            req = {"id": f"REQ-{n:04d}", "token": self.token_factory(), "created_at": self.clock().isoformat(),
                   "text": text, "intent_id": None, "intent": None, "capture_id": None, "order_ref": order_ref,
                   "kind": NEEDS_INFO, "code": None, "amount_source": None, "duplicate_of": None,
                   "label": label, "reader_label": reader_label or (reader.name if reader is not None else None)}
            if conversions:
                req["original_text"], req["fx"] = original, conversions
            cr = consent_reader if consent_reader is not None else self.consent_reader
            if reader is not None:
                self._from_reader(req, reader, iid, order_ref, cr, pre)
            else:
                ex = extractor or self.extractor
                if pre is not None and pre.facts is not None:
                    facts, last = pre.facts, pre.facts_exchange
                else:
                    facts = ex.extract(text)
                    last = getattr(ex, "last", None)
                req["facts"] = facts.as_dict()
                if last:       # a model exchange, kept for the evidence: what came back and how it was judged
                    req["model_exchange"] = {"verdict": last.get("verdict"), "raw_reply": str(last.get("raw_reply", ""))[:1200],
                                             "instructions_separate": not last.get("joined", True)}
                if "MODEL_OUTPUT_REJECTED" in facts.notes:
                    # the model's reply was thrown away and the rules answered: never let the screen say "model" here
                    req["reader_label"] = FALLBACK_LABEL
                self._classify(req, facts, iid, order_ref, cr, pre)
            if "MODEL_OUTPUT_REJECTED" in ((req.get("facts") or {}).get("notes") or ()):
                req["reader_label"] = FALLBACK_LABEL      # the Reader path too: never "model" over a rules answer
            if pre is not None and pre.consent_exchange and "consent_exchange" not in req:
                req["consent_exchange"] = pre.consent_exchange      # a call that was paid for keeps its evidence
            self.store.requests[req["id"]] = req
            self.store.save()
            if req["intent"]:
                self.submit_persisted(req)
            return req

    def _with_consent(self, req: dict, proposal: Proposal, consent_reader, pre: Prefetch | None = None) -> Proposal:
        """Ask the consent reader (if one is configured) for its reading of the buyer's words, once per proposal, and carry the
        reading inside the proposal so that it is signed with the decision. A reply that is thrown away is labeled (the note
        CONSENT_READ_REJECTED, the exchange kept on the request) and the rules answer alone. When the reading was already taken
        before the lock (Prefetch), it is used as it is; otherwise it is taken now."""
        if consent_reader is None or proposal.consent is not None:
            return proposal
        if pre is not None and pre.consent_done:
            reading, exchange = pre.reading, pre.consent_exchange
        else:
            reading, exchange = self._read_consent(consent_reader, req["text"])
        if exchange:
            req["consent_exchange"] = exchange
        if reading is None:
            return replace(proposal, notes=tuple(proposal.notes) + ("CONSENT_READ_REJECTED",))
        return replace(proposal, consent=reading.as_dict())

    def _classify(self, req: dict, facts, iid: str, order_ref: str, consent_reader=None, pre: Prefetch | None = None) -> None:
        mentioned = normalize_order_ref(req["text"])
        linked = self.orders.get(order_ref)
        if ((mentioned and mentioned != order_ref)
                or binding_problem(req["text"], order_ref) in ("ORDER_MISMATCH", "SEVERAL_ORDERS")):
            req["code"] = "ORDER_MISMATCH"       # the link proves one order; the text names another order number: the buyer is asked
            return
        # A PayPal transaction id that is not the ticket's is NOT asked about here (team decision 2026-11-02): the proposal goes
        # to the gate, whose rule G19 holds it for the owner. The refund can only go to the ticket's own capture.
        if self._hold_non_usd_order(req, linked) or self._hold_foreign_currency(req):
            return
        if self._hold_unverified_amount(req) or self._hold_negative_amount(req):
            return
        if facts.issue_code not in PURPOSES:
            req["code"] = "MISSING_REASON"
            return
        if not facts.refund_intent:
            req["code"] = "MISSING_INTENT"
            return
        order = self.orders.get(order_ref)
        if order is None:
            # An unknown order still goes to the gate so the attempt is blocked and leaves a receipt.
            payee, remaining = "unknown:" + order_ref, None
        else:
            payee = order.capture_id
            req["capture_id"] = order.capture_id
            held, open_intent = held_on_capture(self.engine.ledger, order.capture_id)
            if open_intent:
                other = next((r["id"] for r in self.store.requests.values() if r.get("intent_id") == open_intent), None)
                req["kind"], req["code"], req["duplicate_of"] = DUPLICATE, "ALREADY_IN_PROGRESS", other
                return
            remaining = order.total_minor - held
            if remaining <= 0:
                req["code"] = "ALREADY_REFUNDED"
                return
        if facts.amount_state == AMBIGUOUS:
            req["code"] = "AMOUNT_AMBIGUOUS"      # two figures, no clear one: ask, never guess
            return
        if facts.amount_state == EXPLICIT and facts.requested_amount_minor:
            amount, req["amount_source"] = facts.requested_amount_minor, "message"
        elif facts.amount_state == FULL and order is not None:
            amount, req["amount_source"] = remaining, "full_refund_words"
        else:
            req["code"] = "MISSING_AMOUNT"        # no amount in the words: ask, never default to full
            return
        if remaining is not None and amount > remaining:
            req["code"] = "AMOUNT_EXCEEDS_PAID"
            return
        quotes = {k: v for k, v in facts.evidence.items() if k in ("issue", "amount", "refund_intent")}
        proposal = Proposal(REFUND, amount, facts.issue_code, facts.source, quotes, tuple(facts.notes), facts.explanation)
        self._persist_intent(req, self._with_consent(req, proposal, consent_reader, pre), iid, payee)

    @staticmethod
    def _hold_unverified_amount(req: dict) -> bool:
        """When the raw-text and the canonical-text readings of the dollar amounts differ (a
        full-width dollar sign, a look-alike letter in "dollars", a superscript digit), NEITHER reading is believed: no proposal
        is built, so no card with a pre-filled amount can reach the owner. The buyer is asked to type the amount in plain
        digits, and the request keeps both readings so the owner can see exactly what was found."""
        if binding_problem(req["text"], req["order_ref"]) != "PARSE_MISMATCH_AMOUNT":
            return False
        req["code"] = "AMOUNT_UNVERIFIED"
        req["binding"] = {"reason": "PARSE_MISMATCH_AMOUNT", "raw_amounts_minor": raw_amounts(req["text"]),
                          "canonical_amounts_minor": canonical_amounts(req["text"])}
        return True

    @staticmethod
    def _hold_non_usd_order(req: dict, order) -> bool:
        """Every refund this build makes is in US dollars. An order captured in a currency with a demo rate (app/fx.py) already counts
        in dollars (its total is converted at that rate), so it is judged like any other. An order in a currency with NO demo rate
        gets no proposal at all: no automatic refund and no pre-filled owner card that would send USD against it; the owner sees it in the list of requests that need a person. The merchant's own order file must be USD
        (app/orders.py); a corpus may still carry such an order to test this."""
        if order is None or order.usd_convertible:
            return False
        req["code"] = "CURRENCY_NOT_SUPPORTED"
        return True

    @staticmethod
    def _hold_negative_amount(req: dict) -> bool:
        """A minus sign before a dollar figure ("-$20.00") is not a refund amount, and dropping the sign would put $20.00 on
        an owner card. Nothing is proposed; the buyer is asked to restate the amount (team decision, 2026-10-30, from
        Dhanush Kankanala's Milestone 1 case A04)."""
        if not has_negative_amount(req["text"]):
            return False
        req["code"] = "AMOUNT_NEGATIVE"
        return True

    def _persist_intent(self, req: dict, proposal: Proposal, iid: str, payee: str) -> None:
        excerpt = " ".join(req["text"].split())[:240]
        # Random key, created once per business operation and persisted before dispatch.
        intent = Intent(iid, AGENT_ID, payee, proposal.amount_minor, "USD", proposal.reason_code,
                        "Buyer says: " + excerpt, self.key_factory(), self.clock())
        req["kind"], req["intent_id"], req["intent"] = SUBMITTED, intent.id, intent.snapshot()
        req["proposal"] = proposal.as_dict()

    def _from_reader(self, req: dict, reader: Reader, iid: str, order_ref: str, consent_reader=None,
                     pre: Prefetch | None = None) -> None:
        """A reader handed over a ready proposal. Only what is NOT a question of authority is checked
        here (there is something to refund, with a known reason code); order, amount, balance, evidence
        and limits are the authority's job, and the authority does them on this proposal as it is."""
        order = self.orders.get(order_ref)
        if pre is not None and pre.proposal is not None:
            proposal = pre.proposal
        else:
            try:
                proposal = reader.read(req["text"], order)
            except Exception:
                proposal = Proposal("NONE", None, None, "reader", notes=("READER_ERROR",))
        req["facts"] = Facts(None, proposal.reason_code, proposal.amount_minor, proposal.source, tuple(proposal.notes),
                             proposal.explanation, EXPLICIT if proposal.amount_minor else AMOUNT_NONE,
                             proposal.action == REFUND, dict(proposal.quotes)).as_dict()
        if (self._hold_non_usd_order(req, order) or self._hold_foreign_currency(req) or self._hold_unverified_amount(req)
                or self._hold_negative_amount(req)):   # before anything is proposed from the amount
            return
        if proposal.action != REFUND:
            req["code"] = "MISSING_AMOUNT" if proposal.action == "ASK" else "MISSING_INTENT"
            return
        if (proposal.amount_minor or 0) <= 0:
            req["code"] = "MISSING_AMOUNT"
            return
        if proposal.reason_code not in PURPOSES:
            req["code"] = "MISSING_REASON"
            return
        payee = order.capture_id if order else "unknown:" + order_ref
        if order:
            req["capture_id"] = order.capture_id
        req["amount_source"] = "message"
        self._persist_intent(req, self._with_consent(req, proposal, consent_reader, pre), iid, payee)

    def submit_persisted(self, req: dict) -> None:
        """Hand the persisted request to the refund authority. Safe to repeat: same intent, same key,
        so a repeat is answered with the earlier receipt instead of creating a second payment. Used for
        the first submit and for recovery of a request saved before a crash."""
        if req.get("fx") and not self.authority.demo_fx:      # saved with a demo conversion, now outside the simulation: a person decides
            req["code"], req["intent"] = "FOREIGN_CURRENCY_HELD", None
            self.store.save()
            return
        intent = Intent.from_snapshot(req["intent"])
        if req.get("proposal"):
            proposal = Proposal.from_dict(req["proposal"])
        else:       # a request saved by an older build: rebuild the proposal from what was stored
            f = req.get("facts") or {}
            quotes = {k: v for k, v in (f.get("evidence") or {}).items() if k in ("issue", "amount", "refund_intent")}
            proposal = Proposal(REFUND, intent.amount_minor, intent.purpose_code, f.get("source", "rules"), quotes)
        self.authority.authorize(req["text"], req["order_ref"], proposal,
                                 intent=intent, request_id=req["id"],
                                 extra=self._extra(req))

    @staticmethod
    def _extra(req: dict) -> dict:
        """What is signed beside the decision besides the gate's own record: the preset, the reader and, when a figure in another
        currency was converted, the buyer's own words and every conversion with its demo rate."""
        extra = {"preset": req.get("label"), "reader": {"label": req.get("reader_label")}}
        if req.get("fx"):
            extra["fx"] = {"original_text": req.get("original_text"), "conversions": req["fx"]}
        return extra

    def orphans(self) -> list[dict]:
        """Requests persisted with an intent that never reached the signed ledger."""
        return [r for r in self.store.requests.values()
                if r.get("kind") == SUBMITTED and r.get("intent")
                and self.engine.ledger.latest_for_intent(r["intent_id"]) is None]
