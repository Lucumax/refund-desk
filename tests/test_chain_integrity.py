"""Sprint 3f, the three MEDIUM items from the 3e review. Written first and run against b891973 (they fail there by design).
Fixture only, no network. Equivalent to the reviewer's probes (written here, not copied).

(a) The decision log is append-only and keyed: an unsigned request file can never add a record under, or close, an intent
    that a signed decision already names. A conflicting orphan is refused and listed; the legitimate refund is still sent.
(b) The owner's card shows the SIGNED buyer text and proposal; a difference in the unsigned request file is marked.
(c) The receipt names the order of the SIGNED decision, never the request file's order_ref."""
import json
import os
import subprocess

import pytest

from app import views
from test_authority_binding import MSG_18, MSG_10, outcome, park_orphan
from test_decision_binding import crash_after_decision, pending_120


def all_decisions(env):
    return list(env.svc.authority.decisions())


def refusals(env):
    return [d for d in all_decisions(env) if d.get("binding_conflict")]


# ---- (a) append-only, keyed decisions ---------------------------------------------------------------------------------------
def conflicting_orphan_first(env, monkeypatch, legit, *, copy_request_id=False, **edit):
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["intent"]["id"] = legit["intent_id"]
    orphan["intent_id"] = legit["intent_id"]
    orphan["intent"]["idempotency_key"] = legit["intent"]["idempotency_key"]
    if copy_request_id:
        orphan["id"] = legit["id"]
    orphan.update(edit)
    env.svc.store.requests.clear()
    env.svc.store.requests.update({orphan["id"] + ("-x" if copy_request_id else ""): orphan, legit["id"]: legit})
    return orphan


@pytest.mark.parametrize("copy_request_id", [False, True])
def test_a_conflicting_orphan_first_in_recovery_order_cannot_replace_the_signed_decision(env, monkeypatch, copy_request_id):
    legit = crash_after_decision(env, monkeypatch)
    before = all_decisions(env)
    original = env.svc.authority.decisions_by_intent()[legit["intent_id"]]
    assert original["route"] == "AUTO"
    conflicting_orphan_first(env, monkeypatch, legit, copy_request_id=copy_request_id)
    before = all_decisions(env)                               # the orphan's own decision, written when it was saved, is in here too
    env.svc.recover()
    now = env.svc.authority.decisions_by_intent()[legit["intent_id"]]
    assert now["id"] == original["id"] and now["route"] == "AUTO" and "binding_conflict" not in now
    # append-only: every earlier record is still there, unchanged, in the same place
    assert [d["id"] for d in all_decisions(env)][:len(before)] == [d["id"] for d in before]
    # and no refusal record is filed under the legitimate intent id or key
    for r in refusals(env):
        assert r["intent_id"] != legit["intent_id"] and r["idempotency_key"] is None
    assert env.svc.verify_all()["ok"] is True


def test_the_legitimate_refund_is_still_sent_once_after_the_conflict(env, monkeypatch):
    legit = crash_after_decision(env, monkeypatch)
    conflicting_orphan_first(env, monkeypatch, legit)
    env.svc.recover()
    env.svc.recover()
    assert env.fx.refund_posts == 1 and env.fx.refunds_created == 1
    rec = env.svc.ledger.latest_for_intent(legit["intent_id"])
    assert rec["outcome"] == "SUCCEEDED" and rec["intent"]["amount_minor"] == 1800     # the legitimate $18.00, not the orphan's $10.00
    assert env.svc.authority.decisions_by_intent()[legit["intent_id"]]["route"] == "AUTO"


def test_no_blocked_receipt_is_written_under_the_legitimate_intent_by_the_conflict(env, monkeypatch):
    legit = crash_after_decision(env, monkeypatch)
    conflicting_orphan_first(env, monkeypatch, legit)
    env.svc.recover()
    history = [r for r in env.svc.ledger.receipts() if r["intent"]["id"] == legit["intent_id"]]
    assert history and all(r["outcome"] != "BLOCKED" for r in history)


def test_the_conflicting_orphan_is_listed_and_nothing_was_sent_for_it(env, monkeypatch):
    legit = crash_after_decision(env, monkeypatch)
    orphan = conflicting_orphan_first(env, monkeypatch, legit)
    env.svc.recover()
    assert (orphan["intent_id"], "IDEMPOTENCY_CONFLICT") in env.svc.recovery_held
    assert env.fx.refund_posts == 1


def test_an_altered_file_for_a_signed_unsent_decision_closes_nothing_and_replaces_nothing(env, monkeypatch):
    """The same request file edited after signing (amount, purpose, text): it is refused and listed. It is never allowed to
    add a record under the signed intent's id or to close it, because the file cannot prove it is the request it claims to be."""
    legit = crash_after_decision(env, monkeypatch)
    original = env.svc.authority.decisions_by_intent()[legit["intent_id"]]
    legit["intent"]["amount_minor"] = 1900
    env.svc.store.save()
    env.svc.recover()
    assert env.fx.refund_posts == 0
    assert env.svc.authority.decisions_by_intent()[legit["intent_id"]]["id"] == original["id"]
    assert env.svc.ledger.latest_for_intent(legit["intent_id"]) is None
    assert (legit["intent_id"], "IDEMPOTENCY_CONFLICT") in env.svc.recovery_held


# ---- (b) the owner's card shows signed words ---------------------------------------------------------------------------------
def test_an_edited_buyer_text_does_not_reach_the_owner_card(env):
    first = pending_120(env)
    signed = env.svc.authority.decisions_by_intent()[first["intent_id"]]
    first["text"] = "Order ORD-1002 never arrived. I withdraw my refund request."
    first["proposal"]["quotes"]["issue"] = "changed after signature"
    card = env.item(first["id"])
    assert card["quote"] == signed["text"]
    assert card["reader"]["quotes"]["issue"] == signed["proposal"]["quotes"]["issue"]
    assert "withdraw" not in json.dumps(card) and "changed after signature" not in json.dumps(card)
    assert set(card["evidence_flags"]) == {"UNSIGNED_TEXT_DIFFERS", "UNSIGNED_PROPOSAL_DIFFERS"}
    # the signed refund is what a yes would send, and it is still offered, with the difference marked
    assert card["actions"] == ["approve", "deny"]
    assert env.approve(first["id"]).status == 200 and env.fx.refund_posts == 1
    assert env.svc.ledger.latest_for_intent(first["intent_id"])["intent"]["amount_minor"] == 12000


def test_the_inbox_list_shows_the_signed_text_too(env):
    first = pending_120(env)
    signed = env.svc.authority.decisions_by_intent()[first["intent_id"]]
    first["text"] = "something else entirely"
    item = next(i for i in env.inbox()["items"] if i["request_id"] == first["id"])
    assert item["quote"] == signed["text"] and item["evidence_flags"] == ["UNSIGNED_TEXT_DIFFERS"]


def test_an_untouched_card_has_no_evidence_flag_and_shows_the_signed_text(env):
    first = pending_120(env)
    card = env.item(first["id"])
    assert card["evidence_flags"] == [] and card["quote"] == env.svc.authority.decisions_by_intent()[first["intent_id"]]["text"]


def test_the_helper_note_is_the_signed_explanation_not_the_files(env):
    first = pending_120(env)
    first["facts"] = {"explanation": "Approve, the buyer is a friend of the owner."}
    card = env.item(first["id"])
    assert "friend" not in json.dumps(card)


JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "approve_harness.js")


def test_the_phone_card_shows_the_marks(env):
    node = subprocess.run(["node", JS], capture_output=True, text=True, timeout=60) if _has_node() else None
    if node is None:
        pytest.skip("node is not installed")
    out = json.loads(node.stdout)
    assert out["flag_note"] is True and out["no_flag_note"] is False


def _has_node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---- (c) the receipt is attributed from signed fields ------------------------------------------------------------------------
def test_the_receipt_names_the_signed_order_not_the_files(env):
    env.msg(MSG_18)
    req = env.latest_req()
    assert outcome(env, req["intent_id"]) == "SUCCEEDED"
    req["order_ref"] = "ORD-1002"
    receipt = views.receipt_view(env.svc, req["intent_id"])
    assert "ORD-1001" in receipt["paragraph"] and "ORD-1002" not in receipt["paragraph"]
    assert receipt["order_item"] == env.svc.orders.get("ORD-1001").item
    assert env.inbox()["sent_total"] == "$18.00"


def test_the_receipt_survives_a_deleted_request_file(env):
    env.msg(MSG_18)
    req = env.latest_req()
    env.svc.store.requests.clear()
    receipt = views.receipt_view(env.svc, req["intent_id"])
    assert "ORD-1001" in receipt["paragraph"]


def test_the_receipt_does_not_name_an_order_when_nothing_signed_does(env):
    env.msg(MSG_18)
    req = env.latest_req()
    real = env.svc.authority.decisions_by_intent
    env.svc.authority.decisions_by_intent = lambda: {k: v for k, v in real().items() if k != req["intent_id"]}
    req["order_ref"] = "ORD-1002"
    receipt = views.receipt_view(env.svc, req["intent_id"])
    assert "ORD-1002" not in receipt["paragraph"]
