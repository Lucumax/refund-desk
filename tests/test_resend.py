"""Sprint 2b: the re-check and the same-key re-send say only what they know.
"No refund id is known to us" is a fact about our own records, not a PayPal answer: no read happens, the label is
NO_KNOWN_ID (never "absent"), and a live re-send fails closed. Fixture only."""
import json
import os

from app import service
from app.paypal_adapter import PayPalAdapter
from conftest import MSG_A, ROOT

CUT = "/api/desk/send"


def cut(env):
    r = env.post(CUT, {"scenario": "cut_line"})
    assert r.status == 201, r.body
    return r.json()["request"]


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def refund_reads(env):
    return [p for m, p in env.fx.log if m == "GET" and p.startswith("/v2/payments/refunds")]


def test_a_recheck_with_no_known_id_reads_nothing_from_paypal_and_says_so(env):
    v = cut(env)
    before = len(env.fx.log)
    after = env.post(f"/api/requests/{v['request_id']}/recheck").json()
    assert len(env.fx.log) == before                          # not one request of any kind went to PayPal
    rc = after["recheck"]
    assert rc["result"] == "NO_KNOWN_ID" and rc["paypal_read"] is False and rc["sent_again"] is False
    assert "ABSENT" not in json.dumps(rc)
    assert env.svc.adapter.lookup(env.svc.ledger.latest_for_intent(v["intent_id"])["intent"]["idempotency_key"]).status == "INDETERMINATE"


def test_the_adapter_never_claims_the_provider_has_no_record():
    src = read("app/paypal_adapter.py")
    assert "LookupResult.absent()" not in src


def test_a_retry_without_a_recorded_recheck_is_refused_and_sends_nothing(env):
    v = cut(env)
    r = env.post(f"/api/requests/{v['request_id']}/retry")
    assert r.status == 409 and r.json()["error"] == "RESEND_RECHECK_REQUIRED"
    assert env.fx.refund_posts == 1 and env.item(v["request_id"])["stage"] == "UNKNOWN"


def test_a_retry_after_a_recheck_that_found_a_known_id_does_not_resend(env):
    v = cut(env)
    iid = v["intent_id"]
    key = env.svc.ledger.latest_for_intent(iid)["intent"]["idempotency_key"]
    env.post(f"/api/requests/{v['request_id']}/recheck")
    env.svc.adapter.remember(key, next(iter(env.fx.refunds)), "COMPLETED")        # an id arrives (for example by webhook)
    env.post(f"/api/requests/{v['request_id']}/retry")
    assert env.fx.refund_posts == 1                                              # it was read, not sent again
    assert env.item(v["request_id"])["stage"] in ("SENT_DONE", "SENT_PENDING")


def test_a_live_resend_fails_closed_even_after_a_recheck(env):
    v = cut(env)
    env.post(f"/api/requests/{v['request_id']}/recheck")
    assert "retry" in env.item(v["request_id"])["actions"]                       # the simulation offers it
    env.svc.mode = "sandbox"                                                     # as if this were a PayPal sandbox run
    assert service.LIVE_RESEND_VERIFIED is False
    assert "retry" not in env.item(v["request_id"])["actions"]
    r = env.post(f"/api/requests/{v['request_id']}/retry")
    assert r.status == 409 and r.json()["error"] == "RESEND_NOT_VERIFIED"
    assert env.fx.refund_posts == 1


def test_a_resend_is_not_offered_once_the_first_send_is_older_than_the_window(env):
    from datetime import timedelta
    from app.paypal_adapter import REPLAY_WINDOW
    v = cut(env)
    env.post(f"/api/requests/{v['request_id']}/recheck")
    env.clock.advance(seconds=int((REPLAY_WINDOW + timedelta(minutes=1)).total_seconds()))
    assert "retry" not in env.item(v["request_id"])["actions"]


def test_a_successful_recheck_leaves_a_signed_recheck_record(env):
    v = cut(env)
    iid = v["intent_id"]
    key = env.svc.ledger.latest_for_intent(iid)["intent"]["idempotency_key"]
    env.svc.adapter.remember(key, next(iter(env.fx.refunds)), "COMPLETED")
    out = env.post(f"/api/requests/{v['request_id']}/recheck").json()
    recs = [r for r in env.svc.ledger.receipts() if r["event"] == "recheck" and r["intent"]["id"] == iid]
    assert recs and recs[-1]["recheck"]["result"] == "FOUND" and recs[-1]["recheck"]["paypal_read"] is True
    assert out["stage"] in ("SENT_DONE", "SENT_PENDING") and out["recheck"]["result"] == "FOUND"
    assert env.svc.verify_all()["ok"] is True


def test_a_send_with_no_answer_and_no_id_becomes_unknown_when_reconciled(env):
    """A crash in the middle of a send leaves SUBMITTING. Reconciling it, with no refund id known, makes it UNKNOWN."""
    eng = env.svc.engine
    real = env.svc.adapter.pay
    from approval_receipt import SimulatedCrash

    def crash(*a, **k):
        raise SimulatedCrash()
    env.svc.adapter.pay = crash
    try:
        env.msg(MSG_A, order="ORD-1001")
    except SimulatedCrash:
        pass
    env.svc.adapter.pay = real
    iid = list(env.svc.store.requests.values())[-1]["intent_id"]
    assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "SUBMITTING"
    env.svc.recheck(iid)
    assert env.svc.ledger.latest_for_intent(iid)["outcome"] == "UNKNOWN"
    assert env.fx.refund_posts == 0


def test_no_text_promises_that_paypal_cannot_refund_twice():
    strings = read("copy/strings.draft.json").lower()
    assert "can not refund twice" not in strings and "cannot refund twice" not in strings
    assert "you will not be refunded twice" not in strings
    demo = read("DEMO_SCRIPT.md").lower()
    assert "nothing is ever sent twice" not in demo and "it reads paypal's record" not in demo
    assert "cannot query paypal because we have no refund id" in demo
