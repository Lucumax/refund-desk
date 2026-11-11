"""Sprint 2b: money-safety regression tests from the 2026-10-24 Codex review (items 1, 2 and the failure
interactions of section 5.4). Each one reproduces a way an UNKNOWN refund (PayPal may already have made it, we
do not know) could stop counting against the order and the budgets. Fixture only, no network."""
import json
from datetime import timedelta

import pytest

from app.arena_reader import Proposal, REFUND
from app.ledgerview import held_on_capture, intent_states, auto_reserved_on_capture
from app.paypal_adapter import REPLAY_WINDOW
from app.authority import AUTO_AGENT
from conftest import MSG_A, build_env

POLICY_SAVE = "/api/policy/save"


def outcomes(env, iid):
    return [r["outcome"] for r in env.svc.ledger.receipts() if r["intent"]["id"] == iid]


def lose_first_answer(env, text=MSG_A, order="ORD-1001"):
    """An automatic refund PayPal accepted but whose answer was lost: UNKNOWN."""
    env.fx.timeout_after_accept = True
    env.msg(text, order=order)
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "UNKNOWN"
    return req


def break_tokens(env):
    """The OAuth token endpoint refuses (no refund request is sent by an attempt that cannot get a token)."""
    real = env.fx.send

    def send(req):
        if req.url.endswith("/v1/oauth2/token"):
            from app.transport import HttpResponse
            return HttpResponse(503, {"Content-Type": "application/json"}, b'{"name":"SERVICE_UNAVAILABLE"}')
        return real(req)
    env.fx.send = send
    env.svc.client._token = None


def held(env, order="ORD-1001"):
    cap = env.svc.orders.get(order).capture_id
    return held_on_capture(env.svc.ledger, cap)[0]


# ---- 1. token failure on a retry ---------------------------------------------------------------------------
def test_token_failure_on_a_retry_keeps_the_earlier_unknown_refund_unknown(tmp_path):
    e1 = build_env(tmp_path / "data")
    req = lose_first_answer(e1)
    iid = req["intent_id"]
    assert e1.fx.refunds_created == 1 and held(e1) == 1800
    e2 = build_env(tmp_path / "data")                   # restart: a fresh process, no cached token
    break_tokens(e2)
    e2.svc.recheck(iid)
    e2.svc.retry(iid)
    final = e2.svc.ledger.latest_for_intent(iid)
    assert final["outcome"] == "UNKNOWN", outcomes(e2, iid)
    assert "FAILED" not in outcomes(e2, iid)
    assert held(e2) == 1800                              # the reservation is still held
    assert e2.fx.refund_posts == 0                       # the attempt never got as far as a refund request


def test_after_a_token_failure_a_second_18_dollar_refund_cannot_pass_the_25_dollar_allowance(tmp_path):
    e1 = build_env(tmp_path / "data")
    req = lose_first_answer(e1)
    e2 = build_env(tmp_path / "data")
    break_tokens(e2)
    e2.svc.recheck(req["intent_id"])
    e2.svc.retry(req["intent_id"])
    cap = e2.svc.orders.get("ORD-1001").capture_id
    assert auto_reserved_on_capture(e2.svc.ledger, cap, AUTO_AGENT) == 1800
    # the buyer writes again, differently worded: the inbox refuses while the first refund is unresolved
    again = e2.msg("ORD-1001 mug set is broken, the handle snapped. Please refund $18.00 for it.", order="ORD-1001")
    assert again["status"]["stage"] == "DUPLICATE"
    # and the gate on its own would not release it either: 18 + 18 is over the 25 allowance
    proposal = Proposal(REFUND, 1800, "refund_damaged", "test",
                        {"issue": "broken", "amount": "$18.00", "refund_intent": "refund"})
    res = e2.svc.authority.authorize("ORD-1001 mug set is broken, the handle snapped. Please refund $18.00 for it.",
                                     "ORD-1001", proposal)
    assert res.decision != "ALLOW" and res.outcome != "SUCCEEDED"
    assert e2.fx.refunds_created == 0


def test_a_failed_first_send_that_never_reached_paypal_is_still_a_definite_failure(env):
    """Guard: the safeguard must not make a first send's token failure UNKNOWN (nothing was ever sent)."""
    break_tokens(env)
    env.msg(MSG_A, order="ORD-1001")
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "FAILED"
    assert held(env) == 0


# ---- 2. send authorization is separate from the unresolved-money hold -----------------------------------------
def test_execution_expiry_cannot_erase_an_unknown_hold(env):
    """Authorization at T0, dispatch delayed to T0+2h (recovery), answer lost, retry at T0+7.5h: that is 5.5 h after
    the first dispatch (inside the replay window) but past the original execution validity."""
    eng = env.svc.engine
    real_execute = eng._execute
    eng._execute = lambda rec: rec                      # the process "crashes" after AUTHORIZED, before any dispatch
    env.msg(MSG_A, order="ORD-1001")
    eng._execute = real_execute
    req = env.latest_req()
    iid = req["intent_id"]
    assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "AUTHORIZED"
    env.clock.advance(hours=2)
    env.fx.timeout_after_accept = True
    env.svc.recover()
    assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "UNKNOWN"
    env.clock.advance(hours=5, minutes=30)
    env.svc.recheck(iid)
    env.svc.retry(iid)
    final = env.svc.ledger.latest_for_intent(iid)
    assert "EXPIRED" not in outcomes(env, iid) and final["outcome"] in ("UNKNOWN", "SUCCEEDED"), outcomes(env, iid)
    assert final["outcome"] == "SUCCEEDED"               # the same-key re-send is inside the replay window: PayPal's fixture dedupes
    assert env.fx.refunds_created == 1 and held(env) == 1800


def test_a_policy_change_cannot_block_away_an_approved_unknown_refund(env):
    """The owner approves a $120 refund, PayPal's answer is lost, then the owner saves a new policy version."""
    env.msg("Order ORD-1002 never arrived and the tracking has not moved in two weeks. Please send me a full refund.",
            order="ORD-1002")
    req = env.latest_req()
    iid = req["intent_id"]
    env.fx.timeout_after_accept = True
    assert env.approve(req["id"]).status == 200
    assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "UNKNOWN"
    p = env.post("/api/policy/preview", {"text": "refund up to $30 per order"}).json()
    s = env.post(POLICY_SAVE, {"text": "refund up to $30 per order", "base_version": p["base_version"],
                               "result_hash": p["result_hash"]})
    assert s.status == 200, s.body
    env.svc.recheck(iid)
    env.svc.retry(iid)
    assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "UNKNOWN", outcomes(env, iid)   # not BLOCKED
    assert held(env, "ORD-1002") == 12000
    assert env.fx.refund_posts == 1                      # the changed policy did not send a second request either


def test_removing_an_eligible_reason_cannot_block_away_an_automatic_unknown_refund(env):
    req = lose_first_answer(env)
    iid = req["intent_id"]
    p = env.post("/api/policy/preview", {"text": "refund only for wrong item"}).json()
    assert p["ok"], p
    assert env.post(POLICY_SAVE, {"text": "refund only for wrong item", "base_version": p["base_version"],
                                  "result_hash": p["result_hash"]}).status == 200
    env.svc.recheck(iid)
    env.svc.retry(iid)
    assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "UNKNOWN", outcomes(env, iid)
    assert held(env) == 1800


def test_a_refused_resend_is_recorded_and_the_hold_stays_in_the_audit(env):
    req = lose_first_answer(env)
    iid = req["intent_id"]
    p = env.post("/api/policy/preview", {"text": "refund only for wrong item"}).json()
    env.post(POLICY_SAVE, {"text": "refund only for wrong item", "base_version": p["base_version"],
                           "result_hash": p["result_hash"]})
    env.svc.recheck(iid)
    env.svc.retry(iid)
    refused = [r for r in env.svc.ledger.receipts() if r["intent"]["id"] == iid and r["event"] == "resend_refused"]
    assert refused and refused[-1]["outcome"] == "UNKNOWN"
    assert env.svc.verify_all()["ok"] is True
    sc = env.get("/api/desk/state").json()["scoreboard"]
    assert sc["limits"]["violation_count"] == 0


def test_the_resend_window_starts_at_the_first_dispatch_not_at_authorization(env):
    """After the replay window (counted from the first dispatch) a retry is not attempted at all."""
    req = lose_first_answer(env)
    iid = req["intent_id"]
    env.clock.advance(seconds=int((REPLAY_WINDOW + timedelta(minutes=5)).total_seconds()))
    env.svc.recheck(iid)
    env.svc.retry(iid)
    assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "UNKNOWN"
    assert env.fx.refund_posts == 1
