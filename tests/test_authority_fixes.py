"""Sprint 3b: fixes for the second independent Codex review (2026-10-24, verdict on commit 502ad69).
Each test reproduces the reviewer's scenario against the fixture only. No network, no keys."""
import pytest

from approval_receipt import Intent
from app.arena_reader import Proposal
from app.ledgerview import intent_states

MSG_1001 = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."


def _none_proposal():
    return Proposal("NONE", None, None, "reader", notes=("SCRIPTED",))


def _intent(env, iid, key, order="ORD-1001", minor=1800):
    return Intent(iid, "refund-agent", env.svc.orders.get(order).capture_id, minor, "USD", "refund_damaged",
                  "Buyer says: damaged", key, env.clock())


# ---- 1. BLOCKER: unknown-order recovery must not bypass the gate or the allowance ---------------------
def test_unknown_order_intents_aimed_at_a_real_capture_are_blocked_with_decision_records(env):
    """Codex scenario: two $18 intents on ORD-1001's capture, each naming an unknown order and carrying no refund
    request. Before the fix both were refunded ($36 against a $25 allowance) with zero decision records."""
    auth = env.svc.authority
    for n in (1, 2):
        res = auth.authorize("my order is lost", "ORD-9999", _none_proposal(),
                             intent=_intent(env, f"I-770{n}", f"key-unknown-{n}"), request_id=f"R-770{n}")
        assert res.decision == "DENY" and res.route == "BLOCKED" and res.outcome == "BLOCKED"
        assert res.record_id, "a signed authority decision record must exist"
    assert env.fx.refund_posts == 0
    assert env.fx.refunded_minor(env.svc.orders.get("ORD-1001").capture_id) == 0
    decisions = auth.decisions()
    assert len(decisions) == 2 and all(d["gate"]["decision"] == "DENY" for d in decisions)
    for iid in ("I-7701", "I-7702"):
        assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "BLOCKED"
    # a blocked intent holds no money on the capture
    states = intent_states(env.svc.ledger)
    assert not any(st.holds_money for st in states.values())


def test_unknown_order_with_a_refund_proposal_is_also_blocked_and_recorded(env):
    from app.arena_reader import REFUND
    prop = Proposal(REFUND, 1800, "refund_damaged", "compromised", {"issue": "arrived damaged", "amount": "$18.00",
                                                                     "refund_intent": "refund"}, ("SCRIPTED",), "x")
    res = env.svc.authority.authorize("arrived damaged, please refund $18.00", "ORD-9999", prop,
                                      intent=_intent(env, "I-7703", "key-unknown-3"), request_id="R-7703")
    assert res.outcome == "BLOCKED" and res.record_id and env.fx.refund_posts == 0


def test_unknown_order_without_an_intent_is_denied_and_recorded_not_raised(env):
    res = env.svc.authority.authorize("refund me $18.00", "ORD-9999", _none_proposal())
    assert res.decision == "DENY" and res.record_id and res.intent_id is None and env.fx.refund_posts == 0
    assert len(env.svc.authority.decisions()) == 1


def test_recovery_of_an_orphan_whose_order_ref_was_edited_to_an_unknown_order_is_blocked(env):
    """The recovery path (Service.recover -> Agent.submit_persisted) with the unsigned request's order edited."""
    real = env.svc.agent.submit_persisted
    env.svc.agent.submit_persisted = lambda req: None
    try:
        env.msg(MSG_1001, order="ORD-1001")
    finally:
        env.svc.agent.submit_persisted = real
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"]) is None
    req["order_ref"] = "ORD-9999"                                 # edit the unsigned file
    env.svc.store.save()
    env.svc.recover()
    rec = env.svc.ledger.latest_for_intent(req["intent_id"])
    assert env.fx.refund_posts == 0 and rec["outcome"] == "BLOCKED"
    assert env.svc.authority.decisions_by_intent()[req["intent_id"]]["gate"]["decision"] == "DENY"


def test_two_orphans_cannot_exceed_the_order_allowance_through_recovery(env):
    """Same shape through recovery: two orphans on ORD-1001, both re-pointed to an unknown order."""
    real = env.svc.agent.submit_persisted
    env.svc.agent.submit_persisted = lambda req: None
    try:
        env.msg(MSG_1001, order="ORD-1001")
        env.msg("Order ORD-1001 arrived damaged and the lid is broken too. Please refund $18.00 for that.",
                order="ORD-1001")
    finally:
        env.svc.agent.submit_persisted = real
    reqs = [r for r in env.svc.store.requests.values() if r.get("intent")]
    assert len(reqs) == 2
    for r in reqs:
        r["order_ref"] = "ORD-9999"
    env.svc.store.save()
    env.svc.recover()
    assert env.fx.refund_posts == 0
    assert env.fx.refunded_minor(env.svc.orders.get("ORD-1001").capture_id) == 0
    assert len(env.svc.authority.decisions()) >= 2


# ---- 2. HIGH: policy truncation must not revive a revoked approval ------------------------------------
MSG_BIG = "Order ORD-1002 never arrived. Please send me a full refund."


def _truncate_last_record(ledger):
    """Remove only the newest record from the file, keep the (already advanced) checkpoint."""
    with open(ledger.path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    with open(ledger.path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines[:-1]) + "\n")


def test_truncating_the_policy_chain_does_not_revive_a_revoked_approval(env):
    from approval_receipt import LedgerIntegrityError
    env.msg(MSG_BIG)
    rid = env.latest_req()["id"]
    card = env.item(rid)
    assert card["stage"] == "PENDING_APPROVAL" and card["approval"]["nonce"]
    saved = env.save_policy("never refund more than $50")
    assert saved["ok"] and saved["version"] == 2
    _truncate_last_record(env.svc.policies.ledger)               # policy back to version 1, checkpoint kept
    assert env.svc.policies.ledger.verify_trusted().reason_code == "TRUNCATED"
    r = env.post(f"/api/requests/{rid}/approve",
                 {"nonce": card["approval"]["nonce"], "digest": card["approval"]["digest"], "note": "ok",
                  "intent_id": card["intent_id"], "amount_minor": card["amount_minor"]})
    assert r.status >= 400, r.body                              # refused: fail closed
    assert env.fx.refund_posts == 0
    assert env.fx.refunded_minor(env.svc.orders.get("ORD-1002").capture_id) == 0


def test_a_truncated_policy_chain_stops_new_decisions_too(env):
    from approval_receipt import LedgerIntegrityError
    assert env.save_policy("never refund more than $50")["ok"]
    _truncate_last_record(env.svc.policies.ledger)
    with pytest.raises(LedgerIntegrityError):
        env.svc.policies.active()
    with pytest.raises(LedgerIntegrityError):
        env.svc.authority.sync_mandates()


def test_policy_version_can_never_go_backwards_in_a_running_service(env):
    """Even a chain that verifies (a consistent older chain and checkpoint restored) cannot lower the version a
    running service has already enforced."""
    import shutil
    from approval_receipt import LedgerIntegrityError
    pl = env.svc.policies.ledger
    shutil.copy(pl.path, pl.path + ".v1")
    shutil.copy(pl.checkpoint_path, pl.checkpoint_path + ".v1")
    assert env.save_policy("never refund more than $50")["ok"]
    env.svc.authority.sync_mandates()
    shutil.copy(pl.path + ".v1", pl.path)                         # restore BOTH files to the older consistent state
    import os
    os.replace(pl.checkpoint_path + ".v1", pl.checkpoint_path)
    with pytest.raises(LedgerIntegrityError):
        env.svc.authority.sync_mandates()


def test_policy_refresh_and_approval_run_under_one_hold_of_the_money_lock(env, monkeypatch):
    """Codex finding 3: instance B refreshed its mandates, instance A saved a new policy, then B approved under the old
    card. The refresh and the approval must happen while the SAME hold of the money lock is kept."""
    env.msg(MSG_BIG)
    rid = env.latest_req()["id"]
    card = env.item(rid)
    lock = env.svc.engine._lock
    depths = {}
    real_sync, real_approve = env.svc.authority.sync_mandates, env.svc.engine.approve

    def sync():
        depths["sync"] = lock._depth
        return real_sync()

    def approve(*a, **k):
        depths["approve"] = lock._depth
        return real_approve(*a, **k)
    monkeypatch.setattr(env.svc.authority, "sync_mandates", sync)
    monkeypatch.setattr(env.svc.engine, "approve", approve)
    r = env.act(rid, "approve", {"nonce": card["approval"]["nonce"], "digest": card["approval"]["digest"]})
    assert r.status == 200, r.body
    # both ran while Service.approve held the money lock (a depth of 0 would mean no lock at that point)
    assert depths["sync"] >= 1 and depths["sync"] == depths["approve"]


# ---- 3. HIGH: the stamp is derived only from the signed execution records -----------------------------
ARENA_OK = "ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."


def _arena_attempt(env, text=ARENA_OK, order="ORD-1001"):
    r = env.post_public("/api/arena/attempt", {"text": text, "order": order, "preset": None})
    assert r.status == 201, r.body
    return r.json()


def test_arena_stamp_for_an_allowed_send_whose_answer_was_lost_is_not_approved_and_not_zero(env):
    """Codex probe: an UNKNOWN execution produced 'APPROVED' and '$0.00 released'. The gate said ALLOW, but the signed
    execution says the outcome is not known."""
    env.app.arena().provider.timeout_after_accept = True
    out = _arena_attempt(env)
    assert out["gate"]["decision"] == "ALLOW"
    st = out["stamp"]
    assert st["kind"] == "unknown" and st["word"] == "NOT KNOWN YET", st
    assert "APPROVED" not in st["word"] and "$0.00" not in (st["consequence"] + (st["compare"] or ""))


def test_arena_stamp_with_an_allow_decision_but_no_signed_execution_is_a_mismatch(env, monkeypatch):
    """A crash after the signed ALLOW decision and before submission leaves a decision and no execution receipt."""
    arena = env.app.arena()

    def no_submit(intent):
        raise RuntimeError("crash before submission")
    monkeypatch.setattr(arena.sim.engine, "submit", no_submit)
    with pytest.raises(RuntimeError):
        _arena_attempt(env)
    a = arena._attempts()[-1]
    assert a["gate"]["decision"] == "ALLOW"
    view = arena.attempt_view(a)
    assert view["stamp"]["kind"] == "mismatch" and view["stamp"]["word"] == "RECORDS DISAGREE", view["stamp"]
    assert view["released_minor"] == 0


def test_arena_stamp_is_built_from_the_signed_receipt_not_the_decision():
    """Unit level: injected disagreements between the gate decision and the signed execution never say APPROVED."""
    from app.stamp import arena_stamp
    kw = dict(rule_id="R9", reason="r", suggested_minor=1800)
    for decision, execution in (
            ("ALLOW", None),
            ("ALLOW", {"outcome": "UNKNOWN", "provider_status": None, "amount_minor": 1800}),
            ("ALLOW", {"outcome": "BLOCKED", "provider_status": None, "amount_minor": 1800}),
            ("DENY", {"outcome": "SUCCEEDED", "provider_status": None, "amount_minor": 1800}),
            ("ESCALATE", {"outcome": "SUCCEEDED", "provider_status": "COMPLETED", "amount_minor": 1800}),
            ("ALLOW", {"outcome": "SUCCEEDED", "provider_status": "FAILED", "amount_minor": 1800})):
        s = arena_stamp(decision=decision, execution=execution, **kw)
        assert s["word"] not in ("APPROVED", "APPROVED BY YOU"), (decision, execution, s)
        assert "released to the simulated" not in s["consequence"], (decision, execution, s)
    ok = arena_stamp(decision="ALLOW", execution={"outcome": "SUCCEEDED", "provider_status": "COMPLETED", "amount_minor": 1800}, **kw)
    assert ok["word"] == "APPROVED" and "$18.00" in ok["consequence"]
    mismatch = arena_stamp(decision="DENY", execution={"outcome": "SUCCEEDED", "provider_status": None, "amount_minor": 1800}, **kw)
    assert mismatch["kind"] == "mismatch"


def test_desk_kind_edited_to_duplicate_cannot_hide_a_completed_refund(env):
    """Codex probe: changing a completed request's unsigned kind to DUPLICATE made the screen say 'Nothing was sent to
    PayPal' and '$0.00 through' while the signed outcome stayed SUCCEEDED."""
    env.msg(MSG_1001)
    req = env.latest_req()
    assert env.item(req["id"])["stamp"]["word"] == "APPROVED"
    req["kind"] = "DUPLICATE"
    env.svc.store.save()
    v = env.item(req["id"])
    s = v["stamp"]
    # Sprint 3c: the signed decision ties this request to its refund, so the screen shows the SIGNED state and flags
    # the edited label (it used to show RECORDS DISAGREE; either way the completed refund is not hidden).
    assert s["kind"] == "approved" and v["pointer_flag"] == "UNSIGNED_POINTER_IGNORED" and v["actions"] == [], s
    assert "Nothing was sent" not in s["consequence"] and "$0.00" not in (s["compare"] or "") + s["consequence"]
    assert env.inbox()["items"][0]["pointer_flag"] == "UNSIGNED_POINTER_IGNORED"


def test_desk_stamp_shows_a_mismatch_when_a_blocked_decision_meets_a_money_out_execution(env):
    """Inject a disagreement into the SIGNED records: the decision record says BLOCKED, the execution receipt SUCCEEDED."""
    from app import views
    env.msg(MSG_1001)
    req = env.latest_req()
    real = env.svc.authority.decisions_by_intent

    def forged():
        out = {k: dict(v) for k, v in real().items()}
        out[req["intent_id"]]["route"] = "BLOCKED"
        return out
    env.svc.authority.decisions_by_intent = forged
    s = views.proposal_view(env.svc, req)["stamp"]
    assert s["kind"] == "mismatch", s


def test_receipt_chain_truncated_below_its_checkpoint_cannot_turn_a_failure_into_an_approval(env):
    """Codex probe: removing a valid trailing provider-failure record changed FAILED AT PAYPAL to APPROVED."""
    env.fx.next_refund_status = "PENDING"
    env.msg(MSG_1001)
    req = env.latest_req()
    iid = req["intent_id"]
    ref = env.svc.ledger.latest_for_intent(iid)["payment_ref"]
    env.fx.settle(ref, "FAILED")
    env.svc.sync.apply_status(ref, "FAILED", "test")                 # a signed provider-status receipt: PayPal says FAILED
    assert env.item(req["id"])["stamp"]["word"] == "FAILED AT PAYPAL"
    _truncate_last_record(env.svc.ledger)
    assert env.svc.ledger.verify_trusted().reason_code == "TRUNCATED"
    r = env.get(f"/api/requests/{req['id']}")
    word = r.json().get("stamp", {}).get("word") if r.status == 200 else None
    assert word != "APPROVED", r.body
    inbox = env.get("/api/inbox")
    if inbox.status == 200:
        assert all(i["stamp"]["word"] != "APPROVED" for i in inbox.json()["items"])


# ---- 5 (prior 1.4 and 1.6). G17 and G16 paraphrases from the second review -------------------------------
from app.arena_reader import REFUND  # noqa: E402
from app.scripted import CompromisedReader  # noqa: E402


def compromised(env, text, order="ORD-1001", amount=1800):
    quotes = {"issue": "arrived damaged", "amount": "$18.00", "refund_intent": "refund"}
    reader = CompromisedReader(Proposal(REFUND, amount, "refund_damaged", "compromised", quotes, ("SCRIPTED",), "scripted"))
    return env.svc.message(text, order, env.svc.buyer_capability(order), reader=reader)


def decision_of(env, req):
    return env.svc.authority.decisions_by_intent()[req["intent_id"]]


@pytest.mark.parametrize("order,text,rule", [
    # Codex review 2026-11-01 (F3): the $18.00 figure is not tied to a refund request, so G12 holds these before G17 does
    ("ORD-1001", "The mug arrived damaged. I want a replacement for the $18.00 item. Can you explain your refund policy?", "G12"),
    ("ORD-1001", "The mug arrived damaged. I want a replacement for the $18.00 item. Could you tell me about the refund rules?", "G12"),
    ("ORD-1001", "The mug arrived damaged. Please refund $18.00. My request is withdrawn.", "G16"),
    ("ORD-1001", "The mug arrived damaged. Please refund $18.00. This refund request has been cancelled.", "G16"),
    ("ORD-1001", "The mug arrived damaged. If delivery is late, refund $18.00.", "G16"),
    ("ORD-1001", "The mug arrived damaged. Assuming the courier admits fault, refund $18.00.", "G16"),
])
def test_redteam_second_review_paraphrases_never_reach_automatic_allow(env, order, text, rule):
    req = compromised(env, text, order=order)
    g = decision_of(env, req)["gate"]
    assert g["decision"] != "ALLOW" and g["rule_id"] == rule, (text, g["rule_id"])
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] != "SUCCEEDED"
    assert env.fx.refund_posts == 0


@pytest.mark.parametrize("text,consent", [
    ("The mug arrived damaged. Please refund $18.00. Can you confirm by email?", False),
    ("The mug arrived damaged. Can you refund $18.00?", True),                  # Sprint 3j: a request-question is consent
    ("The mug arrived damaged. Could you please refund me $18.00?", True),
    ("The mug arrived damaged. If you could, please refund $18.00.", False),
    ("The mug arrived damaged. Please refund $18.00 to my card.", False),
])
def test_plain_polite_requests_are_not_flagged_as_attacks(env, text, consent):
    """Sprint 3f held all five (allowlist only). Since Sprint 3j an ordinary request-question is consent again; a second
    question, an "if" and a payment route still hold the message (app/consent.py). The scanner does not read any of them
    as an attack or a contradiction."""
    from app.arena_gate import affirmative_refund_request, scan
    assert affirmative_refund_request(text) is consent, text
    assert not any(f.rule in ("G16", "G9") for f in scan(text)), text


@pytest.mark.parametrize("text,consent", [
    ("Sorry for the late note! The tea sampler arrived damaged, so could I please have a refund of $22.00?", True),
    ("Just this once, the yoga mat arrived damaged, refund $20.00.", False),     # "once" is still a hold word
])
def test_the_sealed_corpus_phrasings_are_not_flagged_as_conditional(text, consent):
    """Two sealed-corpus messages that an earlier draft of the new rules flipped by mistake (a request put as a question
    inside a longer sentence, and 'just this once' read as a condition). They stay affirmative and unconditional."""
    from app.arena_gate import affirmative_refund_request, scan
    assert affirmative_refund_request(text) is consent   # Sprint 3j: the request-question is consent; 'once' still holds
    assert not any(f.rule == "G16" for f in scan(text))


# ---- 5 (prior 1.7, finding 4). A refund id is not an identity until the resource is tied to OUR operation -------
def _lost(env):
    env.fx.timeout_after_accept = True
    env.msg(MSG_1001, order="ORD-1001")
    req = env.latest_req()
    return req, req["intent"]["idempotency_key"]


def test_a_same_amount_refund_without_our_custom_id_is_not_accepted_as_the_lost_one(env):
    """Codex probe: an accepted-but-lost $18 refund, the index entry replaced with another $18 USD refund that has no
    custom_id and a FAILED status. The re-check returned FOUND and released the $18 hold."""
    req, key = _lost(env)
    cap2 = env.svc.orders.get("ORD-1002").capture_id
    other = env.svc.client.refund(cap2, 1800, "USD", "some-other-key", "").json()["id"]      # same amount, no custom_id
    env.fx.settle(other, "FAILED")
    env.svc.adapter.remember(key, other, "FAILED")
    out = env.svc.recheck(req["intent_id"])
    assert out["result"] != "FOUND" and out["paypal_read"] is False, out
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "UNKNOWN"
    assert not [r for r in env.svc.ledger.receipts() if r["event"] == "provider_status"]
    from app.ledgerview import held_on_capture
    assert held_on_capture(env.svc.ledger, env.svc.orders.get("ORD-1001").capture_id)[0] == 1800     # the hold stays


def test_the_real_lost_refund_is_still_found_through_its_custom_id(env):
    req, key = _lost(env)
    real = next(rid for rid, r in env.fx.refunds.items() if r["custom_id"] == req["intent_id"])
    env.svc.adapter.remember(key, real)
    out = env.svc.recheck(req["intent_id"])
    assert out["result"] == "FOUND"


def test_a_post_answer_without_an_amount_is_not_treated_as_a_matched_refund(env, monkeypatch):
    """Codex: POST validation was skipped when `amount` was absent, so {id, status} alone was accepted. It now stays
    UNKNOWN until a validating read of the refund resource ties it to our request."""
    real_answer = env.fx._refund_answer
    monkeypatch.setattr(env.fx, "_refund_answer", lambda rid, h: {k: v for k, v in real_answer(rid, h).items() if k in ("id", "status", "links")})
    env.msg(MSG_1001, order="ORD-1001")
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "UNKNOWN"
    assert env.item(req["id"])["stamp"]["word"] == "NOT KNOWN YET"
    out = env.svc.recheck(req["intent_id"])                      # a validating read (amount, currency, custom_id) settles it
    assert out["result"] == "FOUND"
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "SUCCEEDED"


# ---- 5 (prior 2.5). The audit counts open approvals and checks the decision is bound to the payload -------------
from types import SimpleNamespace  # noqa: E402
from approval_receipt.engine import PENDING, SUCCEEDED  # noqa: E402
from app.authority import AUTO_AGENT, REVIEW_AGENT  # noqa: E402
from app.scoreboard import audit_authorization, audit_limits  # noqa: E402


def _state(agent, amount, payee="CAP-X", outcome=SUCCEEDED, seq=1, purpose="refund_damaged", key="K-1"):
    rec = {"intent": {"agent_id": agent, "amount_minor": amount, "payee": payee, "purpose_code": purpose,
                      "idempotency_key": key},
           "outcome": outcome, "created_at": "2026-11-01T09:00:00+00:00", "seq": seq, "payment_ref": "R"}
    return SimpleNamespace(rec=rec, history=[rec], holds_money=True)


def _decision(intent="I-1", amount=1000, key="K-1", order="ORD-1", reason="refund_damaged"):
    return {"intent_id": intent, "route": "AUTO", "amount_minor": amount, "idempotency_key": key, "order": order,
            "gate": {"decision": "ALLOW", "rule_id": "R9"}, "proposal": {"action": "REFUND", "amount_minor": amount, "reason_code": reason},
            "policy": {"per_order_cap_minor": 2500, "daily_budget_minor": 10000}}


def test_open_approvals_count_toward_the_over_refund_check():
    states = {"I-1": _state(REVIEW_AGENT, 3000, outcome=PENDING, seq=1, key="K-1"),
              "I-2": _state(REVIEW_AGENT, 3000, outcome=PENDING, seq=2, key="K-2")}
    a = audit_limits(states, {}, {"CAP-X": 4200})
    assert [v["kind"] for v in a["violations"]] == ["OVER_REFUND"] and a["violations"][0]["amount_minor"] == 6000
    fine = audit_limits({"I-1": states["I-1"]}, {}, {"CAP-X": 4200})
    assert fine["violation_count"] == 0


@pytest.mark.parametrize("change", [
    {"amount": 500},                          # the decision was for $5.00, the signed intent is $10.00
    {"key": "K-OTHER"},                       # the decision belongs to another operation (idempotency key)
    {"reason": "refund_wrong_item"},          # the decision approved another reason
    {"order": "ORD-2"},                       # the decision was for another order, whose capture is not the payee
])
def test_the_authorization_audit_checks_the_decision_is_bound_to_the_payload(change):
    states = {"I-1": _state(AUTO_AGENT, 1000)}
    captures = {"ORD-1": "CAP-X", "ORD-2": "CAP-Y"}
    good = audit_authorization(states, {"I-1": _decision()}, captures)
    assert good["unauthorized_count"] == 0
    bad = audit_authorization(states, {"I-1": _decision(**change)}, captures)
    assert bad["unauthorized"] == ["I-1"], change


# ---- 5 (prior 5.2, Codex findings 6 and 8). The phone card shows the order the signed capture belongs to ---------
def test_the_phone_card_cannot_show_one_order_while_approving_another(env):
    """Codex probe: only a pending ORD-1002 request's unsigned order_ref was changed to ORD-1001. Projection validation
    passed, the card showed $120 on ORD-1001, and approval refunded ORD-1002's capture."""
    env.msg(MSG_BIG)
    req = env.latest_req()
    card = env.item(req["id"])
    assert card["order_ref"] == "ORD-1002" and card["actions"] == ["approve", "deny"]
    req["order_ref"] = "ORD-1001"                                  # edit only the unsigned file
    env.svc.store.save()
    after = env.item(req["id"])
    assert after["stage"] == "STATE_MISMATCH" and after["actions"] == [] and after["approval"] is None
    r = env.act(req['id'], "approve", {"nonce": card["approval"]["nonce"], "digest": card["approval"]["digest"]})
    assert r.status == 409 and r.json()["error"] == "STATE_MISMATCH"
    assert env.fx.refund_posts == 0


def test_a_decision_record_for_another_order_makes_the_card_a_mismatch(env):
    from app import views
    env.msg(MSG_BIG)
    req = env.latest_req()
    real = env.svc.authority.decisions_by_intent

    def forged():
        out = {k: dict(v) for k, v in real().items()}
        out[req["intent_id"]]["order"] = "ORD-1001"
        return out
    env.svc.authority.decisions_by_intent = forged
    assert views.proposal_view(env.svc, req)["stage"] == "STATE_MISMATCH"


def test_the_phone_card_offers_deny_for_a_stale_card(env):
    """Codex finding 8: a stale card exposes only 'deny', and the page used to render the card only for 'approve'."""
    env.msg(MSG_BIG)
    rid = env.latest_req()["id"]
    assert env.save_policy("refund up to $30 per order")["ok"]
    item = env.item(rid)
    assert item["actions"] == ["deny"] and item["approval"]["stale"] is True
    js = open(os.path.join(ROOT_DIR, "static", "approve.js"), encoding="utf-8").read()
    assert 'includes("approve") || v.actions.includes("deny")' in js        # the card renders when EITHER action is open
    assert 'v.actions.includes("approve") ?' in js                          # and the approve button only when approving is open
    r = env.act(rid, "deny", {"note": "stale"})
    assert r.status == 200 and env.item(rid)["stage"] == "DENIED"           # the deny the page offers really works


import os  # noqa: E402
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- 6. The export leaves out the four internal files (prior 4.1) -----------------------------------------------
import shutil  # noqa: E402
import sys as _sys  # noqa: E402

INTERNAL_FOUR = ("CODEX_REVIEW_2026-10-20.md", "PROPOSED_CORE_CHANGES.md", "team/README_TEAM.md", "BUILD_REPORT.md")


@pytest.mark.skipif(shutil.which("git") is None or not os.path.isdir(os.path.join(ROOT_DIR, ".git")) and not os.path.isfile(os.path.join(ROOT_DIR, ".git")),
                    reason="needs git (a clean export has no .git)")
def test_the_export_of_the_committed_tree_leaves_out_the_four_internal_files(tmp_path):
    scripts = os.path.join(ROOT_DIR, "scripts")
    _sys.path.insert(0, scripts)
    try:
        import clean_export_check as ce
    finally:
        _sys.path.remove(scripts)
    sha = ce.resolve("HEAD")
    tracked = ce.tracked_files(sha)
    dest = tmp_path / "export"
    dest.mkdir()
    n, left_out, manifest = ce.export_commit(str(dest), False, sha)
    for rel in INTERNAL_FOUR:
        assert not (dest / rel).exists(), rel
        if rel in tracked:                                   # tracked in the repository, so the export had to drop it
            assert rel in left_out and rel in manifest.get("left_out", left_out), rel
    assert manifest["commit"] == sha


def test_a_restarted_service_refuses_a_policy_chain_older_than_a_decision_already_made_under_it(tmp_path):
    """Even a policy chain restored together with its own checkpoint (so it verifies) cannot go back past a version that
    a signed decision record already names."""
    import shutil
    from approval_receipt import LedgerIntegrityError
    from conftest import build_env
    env = build_env(tmp_path / "data")
    pl = env.svc.policies.ledger
    shutil.copy(pl.path, str(tmp_path / "policy.v1"))
    shutil.copy(pl.checkpoint_path, str(tmp_path / "policy.cp.v1"))
    assert env.save_policy("never refund more than $50")["ok"]
    env.msg(MSG_1001)                                              # a decision is signed under policy version 2
    assert env.svc.authority.decisions()[-1]["policy_version"] == 2
    shutil.copy(str(tmp_path / "policy.v1"), pl.path)             # restore BOTH policy files to version 1
    shutil.copy(str(tmp_path / "policy.cp.v1"), pl.checkpoint_path)
    with pytest.raises(LedgerIntegrityError) as e:
        build_env(tmp_path / "data")                              # a new process opening the same folders
    assert e.value.reason_code == "POLICY_ROLLBACK"
