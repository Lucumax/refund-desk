"""Sprint 2: "cut the line". When PayPal's answer to a refund request is lost (outcome UNKNOWN) the desk shows the
refund as unresolved, keeps the money held, blocks a repeat, and offers ONE owner action: re-check PayPal's own
record. It never offers a blind re-send, and a re-check sends nothing. Fixture only (the fault is simulated)."""
import os
from datetime import timedelta

import pytest

from app import scripted
from app.desk import Desk
from app.ledgerview import dispatch_count, intent_states
from app.paypal_adapter import REPLAY_WINDOW
from conftest import ROOT

CUT = "/api/desk/send"


def cut(env):
    r = env.post(CUT, {"scenario": "cut_line"})
    assert r.status == 201, r.body
    return r.json()["request"]


def sent_requests(env):
    return sum(1 for r in env.svc.ledger.receipts() if r["event"] == "dispatch")


def test_the_cut_line_demo_is_offered_on_the_fixture_and_labeled_as_a_simulation(env):
    st = env.get("/api/desk/state").json()
    assert [f["id"] for f in st["faults"]] == ["cut_line"]
    strings = env.get("/api/strings", headers={}).json()
    assert "simulation" in strings["desk.fault_title"].lower() and "only in the simulation" in strings["desk.fault_note"]


def test_a_lost_answer_shows_unresolved_with_money_held_and_no_second_request(env):
    v = cut(env)
    assert v["stage"] == "UNKNOWN" and v["unresolved"] is True and v["attention"] is True
    assert "not sure" in v["headline"].lower() and "check before trying again" in v["headline"].lower()
    assert v["refund_id"] is None and v["amount"] == "$12.00" and v["requests_sent"] == 1
    assert env.fx.refund_posts == 1 and sent_requests(env) == 1
    # PayPal's fixture did create the refund; the desk does NOT know that, which is the whole point
    assert env.fx.refunds_created == 1
    assert env.get("/api/desk/state").json()["refund_requests_sent"] == 1
    held = env.get("/api/desk/state").json()["orders"]
    assert next(o for o in held if o["order_ref"] == "ORD-1004")["remaining_minor"] == 300       # $15 - $12 held


def test_the_only_owner_action_offered_is_a_recheck_never_a_retry(env):
    v = cut(env)
    assert v["actions"] == ["recheck"]
    desk_js = open(os.path.join(ROOT, "static", "desk.js"), encoding="utf-8").read()
    assert "retry" not in desk_js.lower() and "/reconcile" not in desk_js
    assert "/recheck" in desk_js and "desk.recheck_button" in desk_js
    s = env.get("/api/strings", headers={}).json()
    assert "sends nothing" in s["desk.recheck_button"].lower()


def test_a_repeat_message_for_the_order_is_blocked_while_unresolved(env):
    cut(env)
    again = env.post(CUT, {"scenario": "cut_line"}).json()["request"]
    assert again["stage"] == "DUPLICATE" and again["refund_id"] is None
    other = env.msg("The notebook arrived damaged, the cover is cracked and torn. Please refund $12.00 again.", order="ORD-1004")
    assert other["status"]["stage"] == "DUPLICATE"
    third = env.msg("ORD-1004 arrived damaged. Please refund $3.00 more.", order="ORD-1004")
    assert third["status"]["stage"] == "DUPLICATE"
    assert env.fx.refund_posts == 1 and sent_requests(env) == 1 and env.fx.refunds_created == 1


def test_recheck_sends_nothing_and_says_what_it_could_not_find(env):
    v = cut(env)
    r = env.post(f"/api/requests/{v['request_id']}/recheck")
    assert r.status == 200
    after = r.json()
    assert after["stage"] == "UNKNOWN" and after["unresolved"] is True
    assert after["recheck"]["result"] == "NO_KNOWN_ID" and after["recheck"]["sent_again"] is False
    assert after["recheck"]["paypal_read"] is False and after["recheck"]["read_attempted"] is False
    assert env.fx.refund_posts == 1 and sent_requests(env) == 1 and env.fx.refunds_created == 1
    s = env.get("/api/strings", headers={}).json()
    text = s["desk.recheck.NO_KNOWN_ID"].lower()
    assert "made no request to paypal" in text and "read nothing" in text and "does not show whether paypal made the refund" in text
    assert env.fx.log.count(("GET", "/v2/payments/refunds/")) == 0 and not [p for m, p in env.fx.log if p.startswith("/v2/payments/refunds")]
    # the re-check is a signed receipt; all three chains still verify
    assert any(r["event"] == "recheck" for r in env.svc.ledger.receipts())
    assert env.svc.verify_all()["ok"] is True
    assert env.get("/api/verify").json()["ok"] is True


def test_a_same_key_resend_is_offered_only_after_a_recheck_found_nothing(env):
    v = cut(env)
    assert "retry" not in v["actions"]
    after = env.post(f"/api/requests/{v['request_id']}/recheck").json()
    assert after["actions"] == ["recheck", "retry"]
    # the desk page itself never shows it (see the JS test above); the inbox may, with the existing explanation
    inbox_js = open(os.path.join(ROOT, "static", "inbox.js"), encoding="utf-8").read()
    assert 'includes("recheck")' in inbox_js


def test_recheck_finds_the_refund_when_its_id_becomes_known(env):
    v = cut(env)
    iid = env.item(v["request_id"])["intent_id"]
    key = env.svc.ledger.latest_for_intent(iid)["intent"]["idempotency_key"]
    ref = next(iter(env.fx.refunds))
    env.svc.adapter.remember(key, ref, "COMPLETED")           # for example a webhook told us the id
    r = env.post(f"/api/requests/{v['request_id']}/recheck")
    assert r.status == 200 and r.json()["stage"] in ("SENT_DONE", "SENT_PENDING")
    assert r.json()["refund_id"] == ref and not r.json()["unresolved"]
    assert env.fx.refund_posts == 1 and sent_requests(env) == 1            # found by reading, not by sending again
    rb = [x for x in env.svc.ledger.receipts() if x["event"] == "readback"]
    assert rb and rb[-1]["readback"]["ok"] is True


def test_recheck_after_the_lookup_window_is_unreadable_and_offers_no_resend(env):
    v = cut(env)
    env.clock.advance(seconds=int((REPLAY_WINDOW + timedelta(minutes=5)).total_seconds()))
    after = env.post(f"/api/requests/{v['request_id']}/recheck").json()
    assert after["recheck"]["result"] == "UNREADABLE" and after["stage"] == "UNKNOWN"
    assert after["actions"] == ["recheck"]
    assert env.fx.refund_posts == 1


def test_recheck_when_paypal_cannot_be_read_stays_unresolved(env):
    v = cut(env)
    iid = env.item(v["request_id"])["intent_id"]
    key = env.svc.ledger.latest_for_intent(iid)["intent"]["idempotency_key"]
    env.svc.adapter.remember(key, next(iter(env.fx.refunds)))
    env.fx.read_unavailable = True
    after = env.post(f"/api/requests/{v['request_id']}/recheck").json()
    assert after["recheck"]["result"] == "UNREADABLE" and after["unresolved"] is True and after["actions"] == ["recheck"]
    assert env.fx.refund_posts == 1


def test_recheck_on_a_settled_refund_does_nothing(env):
    r = env.post(CUT, {"scenario": "legit_small"}).json()["request"]
    assert r["stage"] == "SENT_DONE"
    out = env.svc.recheck(r["intent_id"])
    assert out["result"] == "NOT_UNRESOLVED" and out["sent_again"] is False
    assert env.fx.refund_posts == 1


def test_the_unresolved_refund_survives_a_restart_and_is_still_blocked(tmp_path):
    from conftest import build_env
    e1 = build_env(tmp_path / "data")
    v = cut(e1)
    e1.svc.checkpoint()
    e2 = build_env(tmp_path / "data")                          # a restart: fresh process state, same signed ledgers
    st = e2.get("/api/desk/state").json()
    assert st["requests"][0]["stage"] == "UNKNOWN" and st["requests"][0]["unresolved"] is True
    again = e2.msg("ORD-1004 arrived damaged, a cracked cover. Please refund $12.00 now.", order="ORD-1004")
    assert again["status"]["stage"] == "DUPLICATE"
    assert e2.fx.refund_posts == 0                              # the second process sent nothing at all
    assert v["stage"] == "UNKNOWN"


def test_the_fault_is_refused_when_the_transport_is_not_the_fixture(env):
    env.svc.client.transport = object()                         # anything that is not the in-memory fixture
    desk = Desk(env.svc)
    assert desk.faults() == []
    with pytest.raises(KeyError):
        desk.send("cut_line", None, None)
    assert env.post(CUT, {"scenario": "cut_line"}).status == 400


def test_the_scoreboard_counts_an_unresolved_refund_on_its_own_line(env):
    cut(env)
    sc = env.get("/api/desk/state").json()["scoreboard"]
    assert sc["legit"]["labeled"] == 1 and sc["legit"]["unresolved"] == 1
    assert sc["legit"]["refused"] == 0 and sc["legit"]["completed_automatically"] == 0
    assert sc["limits"]["violation_count"] == 0 and sc["authorization"]["unauthorized_count"] == 0


def test_cut_line_is_not_one_of_the_ten_core_scenarios():
    assert len(scripted.SCENARIOS) == 10 and [f["id"] for f in scripted.FAULT_DEMOS] == ["cut_line"]
    assert scripted.LABELS["cut_line"] == {"attack": False, "legit_claim_minor": 1200}
