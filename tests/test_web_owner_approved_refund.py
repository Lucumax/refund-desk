"""One owner-approved refund end to end through the web routes the pages use: the buyer writes on the buyer
page, the owner sees the card in the inbox and approves it, the refund is sent once and read back from the
refund resource. When PayPal's answer is uncertain the request waits (UNKNOWN or PENDING): the background
maintenance never re-sends, and only the owner's Retry re-sends, with the same request key.

Maintainer adaptation: since Sprint 3f a message with extra substance ("and the tracking is stuck") is
outside the closed consent grammar, so the over-allowance request is held under rule G17 instead of R7, and the
owner's Retry needs a recorded recheck first. The flow and the other assertions are Xin's, unchanged."""
import json

OVER_ALLOWANCE = "Order ORD-1001 never arrived and the tracking is stuck. Please refund $30.00."


def buyer_writes(env):
    r = env.msg(OVER_ALLOWANCE, "ORD-1001")
    return r["token"], env.latest_req()["id"]


def buyer_view(env, token):
    return env.get(f"/api/buyer/requests/{token}", headers={}).json()


def test_buyer_message_owner_approval_refund_and_read_back(env):
    token, rid = buyer_writes(env)
    card = next(i for i in env.inbox()["items"] if i["request_id"] == rid)
    assert card["stage"] == "PENDING_APPROVAL" and card["approval"]["nonce"] and card["approval"]["digest"]
    assert card["gate"]["rule_id"] == "R7"                       # $30 is over the $25 allowance (Sprint 3f had G17 here; 3j consent accepts the wording)
    assert env.fx.refund_posts == 0                              # nothing moves before the owner decides

    assert env.approve(rid).status == 200
    assert env.item(rid)["stage"] == "SENT_DONE"
    assert env.fx.refund_posts == 1 and env.fx.refunds_created == 1

    rb = env.post(f"/api/desk/readback/{rid}", {}).json()
    assert rb["ok"] and rb["status"] == "COMPLETED"
    assert {c["id"]: c["ok"] for c in rb["checks"]} == {"refund_id": True, "status_known": True, "amount": True,
                                                       "currency": True, "custom_id_echo": True}
    assert env.get("/api/verify").json()["ok"]
    assert buyer_view(env, token)["request_id"] == rid


def test_lost_answer_waits_and_only_the_owners_retry_resends_with_the_same_key(env):
    token, rid = buyer_writes(env)
    env.fx.timeout_after_accept = True                           # PayPal makes the refund; its answer is lost
    env.approve(rid)
    assert env.item(rid)["stage"] == "UNKNOWN"
    assert env.fx.refund_posts == 1 and env.fx.refunds_created == 1

    assert env.post("/api/poll").status == 200                   # the background maintenance only looks
    assert env.fx.refund_posts == 1 and env.item(rid)["stage"] == "UNKNOWN"
    rb = env.post(f"/api/desk/readback/{rid}", {}).json()
    assert not rb["ok"] and rb["reason"] == "NO_REFUND_ID"       # nothing to read back yet: still waiting

    env.post(f"/api/requests/{rid}/recheck")                    # adaptation: a re-send needs a recorded recheck first
    env.post(f"/api/requests/{rid}/retry")
    assert env.item(rid)["stage"] == "SENT_DONE"
    assert env.fx.refund_posts == 2 and env.fx.refunds_created == 1    # same key: the first refund, no second
    assert env.post(f"/api/desk/readback/{rid}", {}).json()["ok"]


def test_pending_refund_waits_until_paypal_completes_it(env):
    token, rid = buyer_writes(env)
    env.fx.next_refund_status = "PENDING"
    env.approve(rid)
    assert env.item(rid)["stage"] == "SENT_PENDING"
    rb = env.post(f"/api/desk/readback/{rid}", {}).json()
    assert rb["ok"] and rb["status"] == "PENDING"                # accepted, not called completed

    (refund_id,) = env.fx.refunds
    env.fx.settle(refund_id)                                     # PayPal finishes it later
    env.post("/api/poll")
    assert env.item(rid)["stage"] == "SENT_DONE"
    assert env.post(f"/api/desk/readback/{rid}", {}).json()["status"] == "COMPLETED"
    assert env.fx.refunds_created == 1


def test_only_the_signed_in_owner_can_approve(env):
    token, rid = buyer_writes(env)
    card = env.item(rid)["approval"]
    body = {"nonce": card["nonce"], "digest": card["digest"]}
    no_session = env.app.handle("POST", f"/api/requests/{rid}/approve", {}, json.dumps(body).encode())
    no_csrf = env.post(f"/api/requests/{rid}/approve", body, headers={"cookie": env.headers["cookie"]})
    assert no_session.status in (401, 403) and no_csrf.status == 403
    assert env.item(rid)["stage"] == "PENDING_APPROVAL" and env.fx.refund_posts == 0
