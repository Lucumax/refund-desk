"""Sprint 3e, items 2 and 3 (MEDIUM): a signed decision binds the PURPOSE as well as intent id, key and amount, and the
refusal of a conflicting attempt is recorded under its own record, never under the legitimate intent's id.
Fixture only, no network. Equivalent to the reviewer's probes (written here, not copied)."""
import pytest

from approval_receipt.engine import IdempotencyConflict
from test_authority_binding import MSG_18, MSG_10, outcome, park_orphan
from test_recovery import park_automatic

MSG_NOT_RECEIVED_18 = "Order ORD-1001 never arrived. Please refund $18.00."


def crash_after_decision(env, monkeypatch, text=MSG_18):
    """The decision is signed, the process stops before the engine writes any receipt."""
    real = env.svc.engine.submit
    monkeypatch.setattr(env.svc.engine, "submit", lambda intent: (_ for _ in ()).throw(RuntimeError("process stopped")))
    with pytest.raises(RuntimeError):
        env.msg(text)
    monkeypatch.setattr(env.svc.engine, "submit", real)
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"]) is None and env.fx.refund_posts == 0
    return req


def to_not_received(req, *, intent=True, proposal=True, text=True):
    if text:
        req["text"] = MSG_NOT_RECEIVED_18
    if intent:
        req["intent"]["purpose_code"] = "refund_not_received"
        req["intent"]["reason_text"] = "Buyer says: " + MSG_NOT_RECEIVED_18
    if proposal:
        req["proposal"]["reason_code"] = "refund_not_received"
        req["proposal"]["quotes"]["issue"] = "never arrived"


# ---- 2. the signed decision binds the purpose --------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    dict(),                                            # intent, proposal and text all changed (the reviewer's case)
    dict(proposal=False, text=False),                  # only the stored intent's purpose
    dict(intent=False, text=False),                    # only the proposal's reason
    dict(intent=False, proposal=False),                # only the buyer text
])
def test_a_signed_decision_is_not_reused_for_another_purpose(env, monkeypatch, kw):
    """Sprint 3f: the altered file is REFUSED and listed, never sent, and it neither replaces nor closes the signed decision
    (before 3f it was closed with a BLOCKED decision and receipt under the signed intent id; a conflicting orphan could abuse that)."""
    req = crash_after_decision(env, monkeypatch)
    before = env.svc.authority.decisions_by_intent()[req["intent_id"]]
    assert before["proposal"]["reason_code"] == "refund_damaged"
    to_not_received(req, **kw)
    env.svc.recover()
    assert env.fx.refund_posts == 0 and env.fx.refunds_created == 0                 # nothing was sent
    assert env.svc.ledger.latest_for_intent(req["intent_id"]) is None               # and nothing was written under the signed id
    assert env.svc.authority.decisions_by_intent()[req["intent_id"]]["id"] == before["id"]
    assert (req["intent_id"], "IDEMPOTENCY_CONFLICT") in env.svc.recovery_held
    assert env.svc.verify_all()["ok"] is True


def test_the_unchanged_orphan_is_still_sent_once_under_its_signed_decision(env, monkeypatch):
    """The binding must not turn recovery of a genuine crash off."""
    req = crash_after_decision(env, monkeypatch)
    before = env.svc.authority.decisions_by_intent()[req["intent_id"]]
    env.svc.recover()
    env.svc.recover()
    assert env.fx.refund_posts == 1 and outcome(env, req["intent_id"]) == "SUCCEEDED"
    assert env.svc.authority.decisions_by_intent()[req["intent_id"]]["id"] == before["id"]
    sent = env.svc.ledger.latest_for_intent(req["intent_id"])
    assert sent["intent"]["purpose_code"] == "refund_damaged"


def test_the_authority_refuses_a_supplied_intent_whose_purpose_differs_from_the_signed_decision(env, monkeypatch):
    from dataclasses import replace
    from approval_receipt import Intent
    from app.arena_reader import Proposal
    req = crash_after_decision(env, monkeypatch)
    before = env.svc.authority.decisions_by_intent()[req["intent_id"]]
    intent = replace(Intent.from_snapshot(req["intent"]), purpose_code="refund_not_received")
    proposal = replace(Proposal.from_dict(req["proposal"]), reason_code="refund_not_received")
    with pytest.raises(IdempotencyConflict):
        env.svc.authority.authorize(req["text"], req["order_ref"], proposal, intent=intent, request_id=req["id"])
    assert env.fx.refund_posts == 0
    assert env.svc.authority.decisions_by_intent()[req["intent_id"]]["id"] == before["id"]
    assert env.svc.ledger.latest_for_intent(req["intent_id"]) is None


def test_approve_and_deny_refuse_a_decision_that_names_another_purpose(env, monkeypatch):
    from approval_receipt import ApprovalError
    env.msg("Order ORD-1002 never arrived. Please send me a full refund.")
    req = env.latest_req()
    real = env.svc.authority.decisions_by_intent

    def other_purpose():
        out = {k: dict(v) for k, v in real().items()}
        out[req["intent_id"]]["proposal"] = {**out[req["intent_id"]]["proposal"], "reason_code": "refund_damaged"}
        return out

    monkeypatch.setattr(env.svc.authority, "decisions_by_intent", other_purpose)
    assert env.svc.authority.review_binding(req["intent_id"]) == "NO_AUTHORITY_DECISION"
    session = env.svc.auth.session(env.headers["cookie"].split("=", 1)[1])
    with pytest.raises(ApprovalError) as e:
        env.svc.deny(req["intent_id"], session, "no")
    assert e.value.code == "NO_AUTHORITY_DECISION" and outcome(env, req["intent_id"]) == "PENDING_APPROVAL"


def test_recovery_of_an_authorized_refund_refuses_a_decision_that_names_another_purpose(env, monkeypatch):
    req = park_automatic(env, monkeypatch)
    real = env.svc.authority.decisions_by_intent

    def other_purpose():
        out = {k: dict(v) for k, v in real().items()}
        out[req["intent_id"]]["proposal"] = {**out[req["intent_id"]]["proposal"], "reason_code": "refund_not_received"}
        return out

    monkeypatch.setattr(env.svc.authority, "decisions_by_intent", other_purpose)
    env.svc.recover()
    assert env.fx.refund_posts == 0 and outcome(env, req["intent_id"]) == "BLOCKED"


# ---- 3. a refused conflicting attempt does not overwrite the legitimate decision -------------------------------------------
def pending_120(env):
    env.msg("Order ORD-1002 never arrived. Please send me a full refund.")
    first = env.latest_req()
    assert env.item(first["id"])["stage"] == "PENDING_APPROVAL"
    return first


def rejections(env):
    return [d for d in env.svc.authority.decisions() if d.get("binding_conflict")]


def test_a_conflicting_snapshot_id_in_real_recovery_leaves_the_legitimate_decision_alone(env, monkeypatch):
    first = pending_120(env)
    legit = env.svc.authority.decisions_by_intent()[first["intent_id"]]
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["intent"]["id"] = first["intent_id"]                  # only the stored snapshot's id is edited
    env.svc.recover()
    assert env.fx.refund_posts == 0
    now = env.svc.authority.decisions_by_intent()[first["intent_id"]]
    assert now["id"] == legit["id"] and now["route"] == "REVIEW"
    v = env.item(first["id"])
    assert v["stage"] == "PENDING_APPROVAL" and v["actions"] == ["approve", "deny"]
    r = env.deny(first["id"])
    assert r.status == 200 and outcome(env, first["intent_id"]) == "DENIED"


def test_the_legitimate_pending_refund_can_still_be_approved_after_the_conflict(env, monkeypatch):
    first = pending_120(env)
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["intent"]["id"] = first["intent_id"]
    env.svc.recover()
    assert env.approve(first["id"]).status == 200
    assert outcome(env, first["intent_id"]) == "SUCCEEDED" and env.fx.refund_posts == 1


def test_the_refusal_is_recorded_under_its_own_record(env, monkeypatch):
    first = pending_120(env)
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["intent"]["id"] = first["intent_id"]
    env.svc.recover()
    env.svc.recover()
    rej = rejections(env)
    assert len(rej) == 1                                          # recorded, signed, and not once per recovery pass
    r = rej[0]
    assert r["route"] == "BLOCKED" and r["intent_id"] != first["intent_id"]
    assert r["binding_conflict"]["reason"] == "INTENT_ID_REUSED"
    assert r["binding_conflict"]["intent_id_claimed"] == first["intent_id"]
    assert env.svc.verify_all()["ok"] is True
    assert (orphan["intent_id"], "IDEMPOTENCY_CONFLICT") in env.svc.recovery_held


def test_a_direct_conflicting_submit_raises_and_leaves_the_decision_alone(env, monkeypatch):
    first = pending_120(env)
    legit = env.svc.authority.decisions_by_intent()[first["intent_id"]]
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["intent_id"] = first["intent_id"]
    orphan["intent"]["id"] = first["intent_id"]
    with pytest.raises(IdempotencyConflict):
        env.svc.agent.submit_persisted(orphan)
    assert env.svc.authority.decisions_by_intent()[first["intent_id"]]["id"] == legit["id"]
    assert env.item(first["id"])["stage"] == "PENDING_APPROVAL"
    assert env.deny(first["id"]).status == 200


def test_reusing_the_legitimate_key_under_a_new_intent_id_does_not_block_the_legitimate_retry(env, monkeypatch):
    """The refusal record must not carry the legitimate key or request id, or it would itself block them."""
    first = pending_120(env)
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["intent"]["idempotency_key"] = first["intent"]["idempotency_key"]
    env.svc.recover()
    assert env.fx.refund_posts == 0
    assert env.approve(first["id"]).status == 200
    assert outcome(env, first["intent_id"]) == "SUCCEEDED"


def test_a_refused_attempt_does_not_make_the_buyers_next_identical_message_a_repeat(env, monkeypatch):
    """A refusal record is not an earlier decision on the same words."""
    first = pending_120(env)
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["intent"]["id"] = first["intent_id"]
    env.svc.recover()
    ctx = env.svc.authority.context("ORD-1001", __import__("app.authority", fromlist=["text_hash"]).text_hash(MSG_10),
                                    env.clock())
    assert ctx.seen_message is False
