import json

import pytest

from app.fixture_transport import VALID_SIG
from conftest import MSG_A


def pending_refund(env):
    env.fx.next_refund_status = "PENDING"
    env.msg(MSG_A)
    env.fx.next_refund_status = "COMPLETED"
    return next(iter(env.fx.refunds))


def send(env, headers, body):
    r = env.app.handle("POST", "/api/webhooks/paypal", headers, body)
    return r.status, r.json()


def status_of(env, ref):
    return env.svc.sync.status(ref)      # derived from signed receipts


def test_valid_webhook_moves_pending_to_completed_and_writes_a_receipt(env):
    ref = pending_refund(env)
    assert status_of(env, ref) == "PENDING"
    env.fx.settle(ref, "COMPLETED")
    h, b = env.fx.make_webhook(ref, "PAYMENT.CAPTURE.REFUNDED", "COMPLETED", "WH-1")
    code, res = send(env, h, b)
    assert (code, res["result"]) == (200, "APPLIED") and status_of(env, ref) == "COMPLETED"
    iid = env.latest_req()["intent_id"]
    last = env.svc.ledger.latest_for_intent(iid)
    assert last["event"] == "provider_status" and last["provider_status"] == "COMPLETED"
    assert env.inbox()["items"][0]["stage"] == "SENT_DONE"
    assert env.get("/api/verify").json()["ok"]


def test_bad_signature_is_rejected_and_changes_nothing(env):
    ref = pending_refund(env)
    env.fx.settle(ref, "COMPLETED")
    h, b = env.fx.make_webhook(ref, "PAYMENT.CAPTURE.REFUNDED", "COMPLETED", "WH-2", signature="forged")
    code, res = send(env, h, b)
    assert code == 400 and res["result"] == "INVALID_SIGNATURE" and status_of(env, ref) == "PENDING"


def test_duplicate_event_is_ignored(env):
    ref = pending_refund(env)
    env.fx.settle(ref, "COMPLETED")
    h, b = env.fx.make_webhook(ref, "PAYMENT.CAPTURE.REFUNDED", "COMPLETED", "WH-3")
    send(env, h, b)
    n = len(env.svc.ledger.receipts())
    code, res = send(env, h, b)
    assert res["result"] == "DUPLICATE_EVENT" and len(env.svc.ledger.receipts()) == n


def test_old_event_arriving_late_cannot_move_status_backward(env):
    ref = pending_refund(env)
    env.fx.settle(ref, "COMPLETED")
    h1, b1 = env.fx.make_webhook(ref, "PAYMENT.CAPTURE.REFUNDED", "COMPLETED", "WH-4", create_time="2026-11-01T09:00:00Z")
    send(env, h1, b1)
    # an older PENDING event with a NEWER timestamp shows up afterwards
    h2, b2 = env.fx.make_webhook(ref, "PAYMENT.CAPTURE.PENDING", "PENDING", "WH-5", create_time="2026-11-01T10:00:00Z")
    code, res = send(env, h2, b2)
    assert res["result"] == "NOOP" and status_of(env, ref) == "COMPLETED"     # PayPal read says COMPLETED


def test_stale_payload_is_never_applied_when_paypal_cannot_be_read(env):
    ref = pending_refund(env)
    env.fx.settle(ref, "COMPLETED")
    h1, b1 = env.fx.make_webhook(ref, "x", "COMPLETED", "WH-6")
    send(env, h1, b1)
    env.fx.read_unavailable = True
    h2, b2 = env.fx.make_webhook(ref, "x", "PENDING", "WH-7", create_time="2030-01-01T00:00:00Z")
    code, res = send(env, h2, b2)
    assert code == 503 and res["result"] == "READBACK_UNAVAILABLE" and status_of(env, ref) == "COMPLETED"


def test_conflicting_final_status_is_kept_for_a_person(env):
    ref = pending_refund(env)
    env.fx.settle(ref, "COMPLETED")
    send(env, *env.fx.make_webhook(ref, "x", "COMPLETED", "WH-8"))
    env.fx.settle(ref, "FAILED")           # PayPal itself now says FAILED after COMPLETED
    code, res = send(env, *env.fx.make_webhook(ref, "x", "FAILED", "WH-9"))
    assert res["result"] == "CONFLICT" and status_of(env, ref) == "COMPLETED"
    last = env.svc.ledger.latest_for_intent(env.latest_req()["intent_id"])
    assert "PROVIDER_STATUS_CONFLICT" in last["flags"]
    assert env.svc.store.data["conflicts"]


def test_failed_refund_frees_the_order_and_is_flagged(env):
    ref = pending_refund(env)
    env.fx.settle(ref, "FAILED")
    send(env, *env.fx.make_webhook(ref, "x", "FAILED", "WH-10"))
    assert env.inbox()["items"][0]["stage"] == "PROVIDER_FAILED" and env.inbox()["items"][0]["attention"]
    env.msg(MSG_A)    # the held amount is released, so a new request is accepted
    assert env.inbox()["items"][0]["kind"] == "SUBMITTED"


def test_unknown_refund_and_other_resources_are_acknowledged_and_ignored(env):
    ref = pending_refund(env)
    h, b = env.fx.make_webhook(ref, "x", "COMPLETED", "WH-11")
    ev = json.loads(b)
    ev["resource"]["id"] = "SOMEONE-ELSES"
    ev["resource"]["custom_id"] = "not-ours"
    assert send(env, h, json.dumps(ev).encode())[1]["result"] == "UNKNOWN_REFUND"
    ev2 = json.loads(b)
    ev2["id"], ev2["resource_type"] = "WH-12", "capture"
    assert send(env, h, json.dumps(ev2).encode())[1]["result"] == "IGNORED_EVENT"
    assert status_of(env, ref) == "PENDING"


def test_verification_outage_returns_non_2xx_so_paypal_retries(env):
    ref = pending_refund(env)
    env.fx.settle(ref, "COMPLETED")
    h, b = env.fx.make_webhook(ref, "x", "COMPLETED", "WH-13")
    env.fx.verify_unavailable = True
    assert send(env, h, b)[0] == 503 and status_of(env, ref) == "PENDING"
    env.fx.verify_unavailable = False
    assert send(env, h, b)[1]["result"] == "APPLIED"        # same event delivered again is processed, not "duplicate"


def test_garbage_body_is_a_400(env):
    code, res = send(env, {"paypal-transmission-sig": VALID_SIG}, b"not json")
    assert code == 400


def test_webhook_lets_an_unknown_payment_resolve_without_a_second_refund(env):
    env.fx.timeout_after_accept = True
    env.msg("ORD-1004 arrived damaged, refund $15.00")
    rid = env.latest_req()["id"]
    assert env.item(rid)["stage"] == "UNKNOWN"
    ref = next(iter(env.fx.refunds))
    code, res = send(env, *env.fx.make_webhook(ref, "x", "COMPLETED", "WH-14"))
    assert code == 200
    assert env.item(rid)["stage"] == "SENT_DONE"
    assert env.fx.refund_posts == 1 and env.fx.refunds_created == 1


def test_status_persists_across_restart(env):
    from conftest import build_env
    ref = pending_refund(env)
    env2 = build_env(env.dir)
    assert env2.svc.sync.status(ref) == "PENDING"
