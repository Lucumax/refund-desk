"""Turns records into JSON for the screens. All sentences come from copy/strings.draft.json.
Buyer text is passed through as data (the front end shows it with textContent only)."""
from __future__ import annotations
from datetime import datetime

from approval_receipt.engine import (PENDING, AUTHORIZED, SUBMITTING, SUCCEEDED, FAILED, UNKNOWN,
                                     BLOCKED, DENIED, EXPIRED)
from approval_receipt import Intent, LedgerIntegrityError
from approval_receipt.engine import terms_digest
from approval_receipt.policy import ESCALATE

from .agent import SUBMITTED, NEEDS_INFO, DUPLICATE
from .ledgerview import intent_states
from .money import fmt
from .refund_status import PENDING as P_PENDING, COMPLETED, FAILED as P_FAILED, CANCELLED
from .service import Service
from .stamp import request_stamp
from .strings import S, all_strings

ATTENTION = {PENDING, UNKNOWN}


def _rule_text(svc: Service, rec: dict, gate_rec: dict | None = None) -> str:
    """One plain sentence for why the money was (or was not) allowed. The authority's own rule (from its
    signed decision record) wins over the engine's: a review-routed intent always shows the engine's
    "over the limit" code, which would be the wrong reason for, say, an authority claim."""
    amount = fmt(rec["intent"]["amount_minor"])
    if gate_rec is not None:
        pol = gate_rec["policy"]
        detail = gate_rec["gate"].get("detail") or {}
        if gate_rec["gate"]["rule_id"] == "G17" and ("gate.G17." + str(detail.get("code"))) in all_strings():
            return S("gate.G17." + detail["code"])        # why consent held, in a sentence of ours: no buyer words inside it
        return S("gate." + gate_rec["gate"]["rule_id"], amount=amount, cap=fmt(pol["per_order_cap_minor"]),
                 daily=fmt(pol["daily_budget_minor"]), ceiling=fmt(pol["escalate_ceiling_minor"]))
    d = rec["decision"]
    if d["rule_id"].startswith("G"):         # gate rule recorded on a blocked receipt, decision record missing
        m = svc.mandate
        return S("gate." + d["rule_id"], amount=amount, cap=fmt(m.per_payment_limit), daily=fmt(m.daily_limit),
                 ceiling=fmt(m.per_payment_limit))
    m = svc.mandate
    return S("decision." + d["reason_code"], amount=amount,
             per_limit=fmt(m.per_payment_limit), daily_limit=fmt(m.daily_limit))


def _reader_view(req: dict, signed: dict | None = None) -> dict | None:
    """The reader's proposal as the owner sees it. With a signed decision it is the SIGNED proposal (amount, reason, quotes,
    notes, explanation, reader label); the request file's own copy is never shown next to Approve. Without one (nothing
    decided yet) it is the file's, and the card has no action. The model transcript is a debugging aid and stays the file's."""
    prop = (signed or {}).get("proposal") or req.get("proposal")
    if not prop:
        return None
    if signed is not None and signed.get("proposal"):
        explanation = prop.get("explanation")
        label = ((signed.get("reader") or {}).get("label")) or req.get("reader_label")
    else:
        explanation, label = (req.get("facts") or {}).get("explanation"), req.get("reader_label")
    return {"source": prop.get("source"), "action": prop["action"], "amount": fmt(prop["amount_minor"]) if prop.get("amount_minor") else None,
            "amount_minor": prop.get("amount_minor"), "reason_code": prop.get("reason_code"), "quotes": prop.get("quotes") or {},
            "notes": list(prop.get("notes") or []), "explanation": explanation,
            "label": label, "model_exchange": req.get("model_exchange")}


def _evidence_flags(req: dict, signed: dict) -> list[str]:
    """What in the unsigned request file differs from what was signed: the buyer's words, the reader's proposal. The card shows
    the signed version and names each difference, so an edit to the file can neither change what the owner reads nor go
    unnoticed."""
    flags = []
    if req.get("text") != signed.get("text") or (req.get("fx") or []) != ((signed.get("fx") or {}).get("conversions") or []):
        flags.append("UNSIGNED_TEXT_DIFFERS")
    sp, fp = signed.get("proposal"), req.get("proposal")
    keys = ("action", "amount_minor", "reason_code", "quotes")
    if fp is not None and sp is not None and any(fp.get(k) != sp.get(k) for k in keys):
        flags.append("UNSIGNED_PROPOSAL_DIFFERS")
    return flags


def _hold_view(gate_rec: dict | None) -> dict | None:
    """Why a request waits for the owner, for the card and the inbox: one code, one sentence of ours, and the three
    untrusted pieces the owner may want to see (the words the rules could not read, the AI reading's flags and its one line).
    Built from the SIGNED decision only. None when consent was not the reason."""
    if gate_rec is None or gate_rec["gate"]["rule_id"] != "G17":
        return None
    d = gate_rec["gate"].get("detail") or {}
    code = d.get("code")
    key = "gate.G17." + str(code)
    return {"code": code, "headline": S(key) if key in all_strings() else S("gate.G17"), "clause": d.get("clause"),
            "flags": list(d.get("flags") or []), "model_verdict": d.get("verdict"), "model_reason": d.get("model_reason"),
            "model_reason_untrusted": True}


def _gate_view(gate_rec: dict | None) -> dict | None:
    if gate_rec is None:
        return None
    g = gate_rec["gate"]
    return {"decision": g["decision"], "rule_id": g["rule_id"], "reason_code": g["reason_code"], "route": gate_rec["route"],
            "trace": g["trace"], "flags": [{"rule": f["rule"], "label": S("arena.flag." + f["category"]), "phrase": f["phrase"]}
                                           for f in g["flags"]],
            "policy_version": gate_rec["policy_version"], "record_id": gate_rec["id"],
            "without_flags": gate_rec["without_flags"]}


def _signed_reads(svc: Service) -> tuple[dict | None, dict | None]:
    """(intent states, decision records) from the SIGNED chains, each read together with its trusted checkpoint. When a
    chain is cut short, edited or its checkpoint is missing, both are None and every screen shows RECORDS DISAGREE
    instead of a verdict built from a shorter history."""
    try:
        return intent_states(svc.ledger), svc.authority.decisions_by_intent()
    except LedgerIntegrityError:
        return None, None


MONEY_OUT = (SUCCEEDED, UNKNOWN, SUBMITTING, AUTHORIZED, PENDING)     # outcomes in which money is, or may be, committed


def _signed_records_agree(rec: dict, gate_rec: dict | None, order_ref: str | None = None) -> bool:
    """The signed execution and the signed decision must tell one story. Money that is committed or may have gone out
    with no decision record at all, or under a decision that blocked it, is a disagreement: the screen then says so
    instead of choosing a side."""
    if rec["outcome"] in MONEY_OUT:
        if gate_rec is None:
            return False
        if gate_rec["route"] == "BLOCKED":
            return False
    if gate_rec is not None and order_ref is not None and gate_rec.get("order") not in (None, order_ref):
        return False                                  # the signed decision was made for another order
    return True


def _stage(rec: dict, pstatus: str | None) -> str:
    """One code for the whole story, used for the label, the buyer text and the sort order."""
    o = rec["outcome"]
    if o == SUCCEEDED:
        return {COMPLETED: "SENT_DONE", P_FAILED: "PROVIDER_FAILED", CANCELLED: "PROVIDER_FAILED"}.get(pstatus, "SENT_PENDING")
    return o


def _needs_text(req: dict) -> str:
    """The owner's line for a request that needs information. For an amount the desk could not verify it names both readings
    (taken from the request file, numbers only)."""
    if req.get("code") == "AMOUNT_UNVERIFIED":
        b = req.get("binding") or {}

        def show(key):
            vals = [v for v in (b.get(key) or []) if isinstance(v, int)]
            return ", ".join(fmt(v) for v in vals) if vals else "no amount"
        return S("needs.AMOUNT_UNVERIFIED", raw=show("raw_amounts_minor"), canonical=show("canonical_amounts_minor"))
    return S("needs." + req["code"])


def _plain(base: dict, req: dict, stage: str, headline: str, attention: bool, mode: str = "fixture") -> dict:
    base.update(stage=stage, attention=attention, stage_label=S("stage." + stage), headline=headline,
                receipt_id=None, intent_id=None, amount=None, facts=[], flags=[], approval=None, actions=[], refund_id=None,
                duplicate_of=req.get("duplicate_of"))
    base["stamp"] = request_stamp(mode=mode, stage=stage, route=None, rule_id=None, reason=headline, amount_minor=None,
                                  suggested_minor=(base.get("reader") or {}).get("amount_minor"))
    return base


def _decisions_by_request(gates: dict) -> dict:
    """The SIGNED decision that ties each request id to an intent. The authority writes `request_id` into the decision it
    records, so this is the one link from a request to its intent that is not an editable file. The FIRST intent a request id
    was bound to keeps it: a later decision cannot take the id over (the authority refuses to write one, and an old log is
    read the same way). `gates` is the newest decision per intent, in the order the intents first appeared."""
    out = {}
    for g in gates.values():
        if g.get("request_id"):
            out.setdefault(g["request_id"], g)
    return out


def _store_key(svc: Service, req: dict) -> str | None:
    """The key the request is stored under. The `id` written inside the (editable) request file must be the same string."""
    return next((k for k, v in svc.store.requests.items() if v is req), None)


def signed_intent_for(req: dict, by_request: dict, states: dict, key: str | None = None) -> tuple[str | None, bool]:
    """(the intent id the SIGNED records tie this request to, whether the request file's own pointer disagrees).
    Display state is derived from signed records only. When a signed decision exists for this request (looked up by the
    key the request is STORED under, not by the `id` written inside the file), its intent wins and the unsigned `kind` /
    `intent_id` / `intent` / `id` are ignored and FLAGGED, never believed: an edited pointer or id can neither hide a
    completed refund nor point a request at another request's refund. With no signed decision for the request (a request
    not yet submitted, or one that never produced an intent) nothing signed can contradict the file, so its own fields are
    used as before."""
    rid = key or req["id"]
    dec = by_request.get(rid)
    if dec is None or not dec.get("intent_id") or dec["intent_id"] not in states:
        return None, False
    iid = dec["intent_id"]
    return iid, not (req.get("kind") == SUBMITTED and req.get("intent_id") == iid and req.get("id") == rid)


def signed_target(svc: Service, req: dict) -> tuple[str | None, str | None]:
    """(intent id, None) when approve, deny, re-check, read-back and re-send may act for this request, else (None, error
    code). The target comes ONLY from the signed records: the intent the signed decision ties to this request, with the
    request file agreeing on every pointer. A file that disagrees acts on nothing."""
    states, gates = _signed_reads(svc)
    if states is None:
        return None, "STATE_MISMATCH"
    iid, ignored = signed_intent_for(req, _decisions_by_request(gates), states, _store_key(svc, req))
    if iid is None:
        return None, "NOT_FOUND"
    if ignored or not svc.projection_ok(req):
        return None, "STATE_MISMATCH"
    return iid, None


def proposal_view(svc: Service, req: dict, detail: bool = False, states: dict | None = None,
                  gates: dict | None = None, by_request: dict | None = None, key: str | None = None) -> dict:
    base = {"request_id": req["id"], "created_at": req["created_at"], "quote": req.get("original_text") or req["text"],
            "fx": list(req.get("fx") or []),
            "helper_note": (req.get("facts") or {}).get("explanation"),
            "order_ref": req["order_ref"], "kind": req["kind"], "reader": _reader_view(req), "gate": None,
            "pointer_flag": None, "hold": None,
            # "UNSIGNED_ONLY": these words come from the request file, no signed decision backs them (such a card has no action)
            "evidence_flags": ["UNSIGNED_ONLY"]}
    order = svc.orders.get(req["order_ref"])
    base["item"] = order.item if order else None
    if states is None or gates is None:
        states, gates = _signed_reads(svc)
    if states is None:                      # the signed chain does not verify against its checkpoint
        return _plain(base, req, "STATE_MISMATCH", S("headline.STATE_MISMATCH", order=req["order_ref"] or ""), True, svc.mode)
    key = key or _store_key(svc, req) or req["id"]
    signed_iid, pointer_ignored = signed_intent_for(req, by_request if by_request is not None else _decisions_by_request(gates),
                                                    states, key)
    if signed_iid is None:                  # nothing signed ties this request to an intent: the file is all there is
        if req.get("id") != key:
            # The id written inside the file is not the key it is stored under: someone edited it (to borrow another
            # request's signed story, or to shed its own). Nothing in it is believed.
            return _plain(base, req, "STATE_MISMATCH", S("headline.STATE_MISMATCH", order=req["order_ref"] or ""), True, svc.mode)
        if req["kind"] != SUBMITTED:
            if req.get("intent_id") or req.get("intent"):
                # The unsigned kind says nothing was submitted, but the request points at an intent. Show the
                # disagreement, never a "duplicate" or "needs information" label over a refund that may have gone out.
                return _plain(base, req, "STATE_MISMATCH", S("headline.STATE_MISMATCH", order=req["order_ref"] or ""), True, svc.mode)
            return _plain(base, req, req["kind"], _needs_text(req), False, svc.mode)
        iid = req["intent_id"]
        other = gates.get(iid)
        if other is not None and other.get("request_id") not in (None, key):
            # The file points at an intent that a signed decision ties to ANOTHER request: not this request's refund.
            return _plain(base, req, "STATE_MISMATCH", S("headline.STATE_MISMATCH", order=req["order_ref"] or ""), True, svc.mode)
    else:
        iid = signed_iid
    st = states.get(iid)
    if st is None:      # saved before a crash, not yet in the signed ledger; recovery submits it
        return _plain(base, req, "NOT_SUBMITTED", S("headline.NOT_SUBMITTED", order=req["order_ref"] or ""), True, svc.mode)
    rec, ps = st.rec, st.provider_status
    gate_rec = gates.get(iid)
    # The order a screen names is the one the SIGNED decision was made for. The request file's own order is an unsigned
    # label: where it differs, the card says so (flag) and still names the signed order.
    signed_order = (gate_rec or {}).get("order") or req["order_ref"]
    if signed_order != req["order_ref"]:
        order = svc.orders.get(signed_order)
        base["order_ref"], base["item"] = signed_order, (order.item if order else None)
    if gate_rec is not None and gate_rec.get("text") is not None:
        # Everything the owner reads next to Approve comes from the SIGNED decision: the buyer's words, the reader's proposal
        # and its quotes. The request file's own copies are compared, never shown, and any difference is marked.
        base["quote"] = (gate_rec.get("fx") or {}).get("original_text") or gate_rec["text"]      # what the buyer wrote
        base["fx"] = list((gate_rec.get("fx") or {}).get("conversions") or [])                  # and each conversion, with its demo rate
        base["reader"] = _reader_view(req, gate_rec)
        base["helper_note"] = (gate_rec.get("proposal") or {}).get("explanation")
        base["hold"] = _hold_view(gate_rec)
        base["evidence_flags"] = _evidence_flags(req, gate_rec)
    if pointer_ignored:
        base["pointer_flag"] = "UNSIGNED_POINTER_IGNORED"
    elif not svc.projection_ok(req):   # the unsigned record disagrees with the signed receipt
        base["pointer_flag"] = "UNSIGNED_RECORD_DISAGREES"
        return _plain(base, req, "STATE_MISMATCH", S("headline.STATE_MISMATCH", order=signed_order or ""), True, svc.mode)
    if not _signed_records_agree(rec, gate_rec, req["order_ref"]):
        if gate_rec is not None and gate_rec.get("order") not in (None, req["order_ref"]):
            base["pointer_flag"] = "UNSIGNED_RECORD_DISAGREES"       # the file names an order the signed decision was not made for
        return _plain(base, req, "STATE_MISMATCH", S("headline.STATE_MISMATCH", order=signed_order or ""), True, svc.mode)
    stage = _stage(rec, ps)
    issue = S("issue." + rec["intent"]["purpose_code"])
    amount = fmt(rec["intent"]["amount_minor"])
    who = (order.buyer_label + ", " if order else "") + (signed_order or "")
    facts = [{"label": S("fact.who"), "value": who},
             {"label": S("fact.amount"), "value": amount},
             {"label": S("fact.why"), "value": issue},
             {"label": S("fact.agent_reason"), "value": rec["intent"]["reason_text"], "untrusted": True},
             {"label": S("fact.rule"), "value": _rule_text(svc, rec, gate_rec)}]
    if pointer_ignored:
        facts.append({"label": S("fact.pointer"), "value": S("pointer.ignored")})
    approval = None
    stale = False
    if rec["outcome"] == PENDING:
        svc.authority.sync_mandates()
        mandate = svc.engine.mandates.get(rec["intent"]["agent_id"])
        # the digest the owner would approve covers the mandate version, which is the policy version: if a newer
        # version was saved since this card was made, a yes is refused by the engine, so the card says so
        stale = mandate is not None and terms_digest(Intent.from_snapshot(rec["intent"]), mandate) != rec["intent_digest"]
        approval = {"nonce": rec["approval_nonce"], "digest": rec["intent_digest"], "stale": stale,
                    "expires_at": rec["approval_expires_at"],
                    # the digest above covers the terms AND this policy version, so a yes cannot outlive the rule it saw
                    "policy_version": gate_rec["policy_version"] if gate_rec else None,
                    "policy_hash": gate_rec["policy_hash"] if gate_rec else None}
    actions = []
    unresolved = rec["outcome"] in (UNKNOWN, SUBMITTING)
    recheck = next((r["recheck"] for r in reversed(st.history) if r["event"] == "recheck"), None)
    if rec["outcome"] == PENDING:
        actions = ["deny"] if stale else ["approve", "deny"]
    elif unresolved:
        # The owner's action is a re-check (a read-back, nothing is sent). A same-key re-send is offered only
        # AFTER a re-check found nothing, never as the first move, and the desk page does not show it at all.
        actions = ["recheck"] + (["retry"] if rec["outcome"] == UNKNOWN and svc.resend_gate(rec["intent"]["id"]) is None else [])
    if pointer_ignored:
        # The request file points somewhere the signed records do not. Its refund is shown as signed, and nothing can be
        # approved, denied or re-checked from this card until a person has looked at the file (nothing is hidden).
        approval, actions, stale = None, [], False
        base["kind"] = SUBMITTED
    base.update(stage=stage, stage_label=S("stage." + stage), attention=rec["outcome"] in ATTENTION or stage == "PROVIDER_FAILED",
                headline=S("headline." + stage, amount=amount, order=signed_order or ""),
                receipt_id=rec["intent"]["id"], intent_id=rec["intent"]["id"], amount=amount,
                amount_minor=rec["intent"]["amount_minor"], issue=issue,
                decision=rec["decision"]["decision"], decision_label=S("decision_label." + rec["decision"]["decision"]),
                reason=_rule_text(svc, rec, gate_rec), facts=facts, gate=_gate_view(gate_rec),
                flags=[S("flag." + f) for f in rec["flags"]], approval=approval, actions=actions,
                unresolved=unresolved, recheck=recheck, requests_sent=sum(1 for r in st.history if r["event"] == "dispatch"),
                provider_status=ps, refund_id=rec["payment_ref"], duplicate_of=None,
                amount_source=None if pointer_ignored else req["amount_source"],
                amount_note=(S("amount_source." + req["amount_source"]) if req["amount_source"] and not pointer_ignored else None))
    base["stamp"] = request_stamp(mode=svc.mode, stage=stage, route=gate_rec["route"] if gate_rec else None,
                                  rule_id=gate_rec["gate"]["rule_id"] if gate_rec else rec["decision"]["rule_id"],
                                  reason=base["reason"], amount_minor=rec["intent"]["amount_minor"],
                                  suggested_minor=(base["reader"] or {}).get("amount_minor"),
                                  stale=bool(approval and approval["stale"]))
    return base


def _stub_request(g: dict, rec: dict) -> dict:
    """A request record rebuilt from the SIGNED decision and receipt, for a signed refund whose request is no longer in
    the request file (deleted, or stored under another key). It carries no pointer the signed records could contradict."""
    return {"id": g["request_id"], "token": "", "created_at": g["at"], "text": g.get("text") or "", "intent_id": rec["intent"]["id"],
            "intent": rec["intent"], "capture_id": rec["intent"]["payee"], "order_ref": g.get("order"), "kind": SUBMITTED,
            "code": None, "amount_source": None, "duplicate_of": None, "label": None, "reader_label": None,
            "fx": list((g.get("fx") or {}).get("conversions") or [])}


def _request_number(request_id: str) -> int:
    try:
        return int(request_id[4:])
    except ValueError:
        return 0


def inbox_view(svc: Service) -> dict:
    try:
        svc.expire_due()
    except LedgerIntegrityError:
        pass                                  # the screen shows RECORDS DISAGREE for every request below
    states, gates = _signed_reads(svc)
    by_request = _decisions_by_request(gates) if gates is not None else None
    items = [proposal_view(svc, r, states=states, gates=gates, by_request=by_request, key=k)
             for k, r in svc.store.requests.items()]
    if states is not None:
        # A signed decision whose request is not in the request file under its id: still shown (and counted), as signed.
        for rid, g in by_request.items():
            if rid not in svc.store.requests and g.get("intent_id") in states:
                v = proposal_view(svc, _stub_request(g, states[g["intent_id"]].rec), states=states, gates=gates,
                                  by_request=by_request, key=rid)
                v.update(pointer_flag="REQUEST_RECORD_MISSING", actions=[], approval=None)
                v["facts"] = v["facts"] + [{"label": S("fact.pointer"), "value": S("pointer.missing")}]
                items.append(v)
    items.sort(key=lambda v: (not v["attention"], _neg(v["created_at"]), -_request_number(v["request_id"])))
    waiting = sum(1 for v in items if v["stage"] == PENDING)
    # The totals come from the SIGNED receipts, one count per signed refund, not from the cards that happen to render: an
    # edit to an unsigned field of the request file flags its card and changes neither number. (`states` is None when the
    # signed chain does not verify, and then nothing is counted from it.)
    sent_ids = {iid for iid, st in (states or {}).items() if _stage(st.rec, st.provider_status) in ("SENT_DONE", "SENT_PENDING")}
    total = sum(states[iid].rec["intent"]["amount_minor"] for iid in sent_ids)
    m = svc.mandate
    pol, prec = svc.policies.active()
    return {"items": items, "waiting": waiting, "sent_count": len(sent_ids), "sent_total": fmt(total),
            "rule_sentence": S("rule.sentence", per_limit=fmt(pol.per_order_cap_minor), daily_limit=fmt(pol.daily_budget_minor),
                               ceiling=fmt(pol.escalate_ceiling_minor), version=prec["version"])}


def next_waiting(svc: Service, current_id: str) -> str | None:
    """The request id of the oldest other request that waits for the owner's yes or no, else None. Read from the SIGNED decisions
    and receipts only (no mandate refresh, no unsigned file)."""
    states, gates = _signed_reads(svc)
    if states is None:
        return None
    by_request = _decisions_by_request(gates)
    for rid in sorted(by_request, key=_request_number):
        g = by_request[rid]
        st = states.get(g.get("intent_id") or "")
        if rid != current_id and st is not None and st.rec["outcome"] == PENDING and g.get("route") == "REVIEW":
            return rid
    return None


def _when(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%b %d, %Y %H:%M UTC")


def _neg(iso: str) -> float:
    return -datetime.fromisoformat(iso).timestamp()


def buyer_view(svc: Service, req: dict) -> dict:
    """What the buyer may see: plain status, order and amount. No limits, rules or flags. Like the owner's screens, it
    follows the SIGNED records: the request file's own label or pointer cannot hide a refund that went out."""
    base = {"request_id": req["id"], "order_ref": req["order_ref"]}
    states, gates = _signed_reads(svc)
    if states is None:
        return {**base, "stage": "STATE_MISMATCH", "message": S("buyer.STATE_MISMATCH")}
    key = _store_key(svc, req) or req["id"]
    signed_iid, pointer_ignored = signed_intent_for(req, _decisions_by_request(gates), states, key)
    if signed_iid is None and req.get("id") != key:
        return {**base, "stage": "STATE_MISMATCH", "message": S("buyer.STATE_MISMATCH")}
    if signed_iid is None and req["kind"] != SUBMITTED:
        stage = "NEEDS_" + req["code"] if req["kind"] == NEEDS_INFO else "DUPLICATE"
        return {**base, "stage": stage, "message": S("buyer." + stage)}
    other = gates.get(req.get("intent_id")) if signed_iid is None else None
    if other is not None and other.get("request_id") not in (None, key):
        return {**base, "stage": "STATE_MISMATCH", "message": S("buyer.STATE_MISMATCH")}
    st = states.get(signed_iid or req["intent_id"])
    if st is None:
        return {**base, "stage": "NOT_SUBMITTED", "message": S("buyer.NOT_SUBMITTED")}
    # The order named to the buyer is the one the SIGNED decision was made for. A request file that names another order
    # (or whose pointers were cleared and its order changed) makes no claim about money: nothing is said under that order.
    gate_rec = gates.get(signed_iid or req["intent_id"])
    signed_order = (gate_rec or {}).get("order") or req["order_ref"]
    base["order_ref"] = signed_order
    if signed_order != req["order_ref"] or (not pointer_ignored and not svc.projection_ok(req)):
        return {**base, "stage": "STATE_MISMATCH", "message": S("buyer.STATE_MISMATCH")}
    rec = st.rec
    stage = _stage(rec, st.provider_status)
    amount = fmt(rec["intent"]["amount_minor"])
    return {**base, "stage": stage, "amount": amount,
            "message": S("buyer." + stage, amount=amount, order=signed_order)}


def receipt_view(svc: Service, intent_id: str) -> dict | None:
    st = intent_states(svc.ledger).get(intent_id)
    if st is None:
        return None
    recs = list(st.history)
    rec = recs[-1]
    amount = fmt(rec["intent"]["amount_minor"])
    ps = st.provider_status
    gate_rec = svc.authority.decisions_by_intent().get(intent_id)
    # The order a receipt names comes from SIGNED fields only: the order of the signed decision, else the order whose capture
    # the signed intent pays. The request file's own order_ref is an unsigned label and is never used to attribute money.
    order_ref = (gate_rec or {}).get("order") or getattr((svc.orders.by_capture.get(rec["intent"]["payee"])), "order_ref", None)
    order = svc.orders.get(order_ref)
    parts = [S("receipt.p_intent", when=_when(recs[0]["created_at"]), amount=amount, order=order_ref or "?",
               issue=S("issue." + rec["intent"]["purpose_code"])),
             S("receipt.p_decision", decision=S("decision_label." + rec["decision"]["decision"]), reason=_rule_text(svc, rec, gate_rec))]
    if rec["approver"]:
        a = rec["approver"]
        parts.append(S("receipt.p_approver_" + a["action"], approver=a["id"], note=a["note"] or S("receipt.no_note")))
    parts.append(S("receipt.p_outcome", outcome=S("outcome." + rec["outcome"])))
    if rec["payment_ref"]:
        parts.append(S("receipt.p_provider", ref=rec["payment_ref"], status=S("provider." + (ps or "UNKNOWN"))))
    if rec["flags"]:
        parts.append(S("receipt.p_flags", flags=", ".join(S("flag." + f) for f in rec["flags"])))
    provider = None
    if rec["payment_ref"]:
        try:
            d = svc.client.get_refund(rec["payment_ref"])
            provider = {"ok": True, "id": d.get("id"), "status": d.get("status"),
                        "amount": d.get("amount", {}).get("value"), "label": S("receipt.provider_record_fixture")
                        if svc.mode == "fixture" else S("receipt.provider_record_sandbox")}
        except Exception:
            provider = {"ok": False, "label": S("receipt.provider_unreadable")}
    safe_raw = {k: v for k, v in rec.items() if k not in ("approval_nonce",)}
    return {"intent_id": intent_id, "paragraph": " ".join(parts), "raw": safe_raw, "provider": provider,
            "history": [{"id": r["id"], "event": r["event"], "outcome": S("outcome." + r["outcome"]),
                         "at": r["created_at"]} for r in recs], "order_item": order.item if order else None}


def verify_view(svc: Service, path: str | None = None) -> dict:
    from approval_receipt import Ledger
    extra = 0
    if path is None:
        all_chains = svc.verify_all()          # receipts, decisions and policy versions, each against its own checkpoint
        if not all_chains["ok"]:
            bad = next(v for k, v in all_chains.items() if k != "ok" and not v["ok"])
            return {"ok": False, "records": bad["records"], "first_broken_id": bad.get("first_broken_id"),
                    "reason_code": bad.get("reason_code"),
                    "message": S("verify.broken", record=bad.get("first_broken_id") or "?",
                                 reason=S("verify.code." + (bad.get("reason_code") or "HASH_MISMATCH")))}
        extra = all_chains["decisions"]["records"] + all_chains["policy"]["records"]
        res = svc.ledger.verify_trusted()      # chain, signatures AND the separately stored checkpoint
    else:                                      # demo copy: check it against the real checkpoint
        res = Ledger(path, svc.ledger._key, svc.ledger.key_id,
                     checkpoint_path=path + ".unused").verify(svc.checkpoint())
    if res.ok:
        return {"ok": True, "records": res.records + extra, "message": S("verify.intact", records=res.records + extra)}
    return {"ok": False, "records": res.records, "first_broken_id": res.first_broken_id,
            "reason_code": res.reason_code,
            "message": S("verify.broken", record=res.first_broken_id or "?", reason=S("verify.code." + res.reason_code))}
