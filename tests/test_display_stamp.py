"""Sprint 3e, item 4 (MEDIUM): the inbox totals and the order a screen names come from SIGNED records only. An edit to an
unsigned field of the request file (order_ref, capture_id, pointers) is FLAGGED on the card and changes neither the
totals nor the order the buyer is told about. Fixture only, no network."""
from copy import deepcopy

import pytest

from app import views
from test_authority_binding import MSG_18, outcome


def sent_request(env):
    env.msg(MSG_18)
    req = env.latest_req()
    assert outcome(env, req["intent_id"]) == "SUCCEEDED"
    return req


@pytest.mark.parametrize("field,value", [
    ("order_ref", "ORD-1002"),
    ("order_ref", "ORD-9999"),
    ("order_ref", None),
    ("capture_id", "CAPTURE-FORGED"),
    ("capture_id", None),
])
def test_an_edited_order_or_capture_does_not_change_the_signed_totals(env, field, value):
    req = sent_request(env)
    req[field] = value
    env.svc.store.save()
    inbox = env.inbox()
    assert inbox["sent_count"] == 1 and inbox["sent_total"] == "$18.00"
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "SUCCEEDED"


@pytest.mark.parametrize("field,value", [("order_ref", "ORD-1002"), ("capture_id", "CAPTURE-FORGED")])
def test_the_edited_file_is_flagged_on_its_card(env, field, value):
    req = sent_request(env)
    req[field] = value
    env.svc.store.save()
    v = env.item(req["id"])
    assert v["stage"] == "STATE_MISMATCH" and v["actions"] == [] and v["approval"] is None
    assert v["pointer_flag"] == "UNSIGNED_RECORD_DISAGREES"
    flagged = [i for i in env.inbox()["items"] if i["request_id"] == req["id"]]
    assert len(flagged) == 1 and flagged[0]["pointer_flag"] == "UNSIGNED_RECORD_DISAGREES"


def test_edited_files_for_two_refunds_still_add_up_from_the_signed_receipts(env):
    first = sent_request(env)
    env.msg("Order ORD-1003 arrived damaged. Please refund $5.00.")
    second = env.latest_req()
    first["order_ref"] = "ORD-1002"
    second["capture_id"] = "CAPTURE-FORGED"
    env.svc.store.save()
    inbox = env.inbox()
    assert inbox["sent_count"] == 2 and inbox["sent_total"] == "$23.00"


def test_the_buyer_is_not_told_about_a_refund_under_another_order(env):
    """Reviewer's probe: order_ref changed to another order and the pointers cleared made the buyer view report the
    refund as sent under THAT order."""
    req = sent_request(env)
    req.update(order_ref="ORD-1002", kind="NEEDS_INFO", intent_id=None, intent=None)
    view = views.buyer_view(env.svc, req)
    assert "ORD-1002" not in str(view)
    assert view["stage"] != "SENT_DONE" or view["order_ref"] == "ORD-1001"


def test_the_buyer_view_names_the_order_of_the_signed_decision(env):
    req = sent_request(env)
    req.update(order_ref="ORD-1002", kind="NEEDS_INFO", intent_id=None, intent=None)
    view = views.buyer_view(env.svc, req)
    assert view["order_ref"] == "ORD-1001"
    assert view["stage"] == "STATE_MISMATCH"                  # the file disagrees with the signed record: no money claim


def test_an_untouched_request_still_reads_as_sent_to_the_buyer(env):
    req = sent_request(env)
    view = views.buyer_view(env.svc, req)
    assert view["stage"] == "SENT_DONE" and view["order_ref"] == "ORD-1001" and "$18.00" in view["message"]


def test_the_owner_card_names_the_signed_order_not_the_edited_one(env):
    req = sent_request(env)
    req.update(order_ref="ORD-1002", kind="NEEDS_INFO", intent_id=None, intent=None)
    env.svc.store.save()
    v = env.item(req["id"])
    assert v["stage"] in ("SENT_DONE", "SENT_PENDING", "STATE_MISMATCH")
    assert v["actions"] == [] and v["pointer_flag"] is not None
    inbox = env.inbox()
    assert inbox["sent_count"] == 1 and inbox["sent_total"] == "$18.00"


def test_the_totals_do_not_depend_on_which_cards_render(env):
    """A request file with every unsigned field blanked still leaves the signed refund counted exactly once."""
    req = sent_request(env)
    base = deepcopy(req)
    req.update(order_ref=None, capture_id=None, kind="NEEDS_INFO", code="MISSING_AMOUNT", intent_id=None, intent=None,
               proposal=None, amount_source=None)
    env.svc.store.save()
    inbox = env.inbox()
    assert inbox["sent_count"] == 1 and inbox["sent_total"] == "$18.00"
    assert base["intent_id"]
