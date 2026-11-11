"""Sprint 2b: gate and authority hardening from the 2026-10-24 Codex review (sections 1.4, 1.6, 1.7, 1.8).
A compromised reader must not be able to invent a refund request, and the bindings recovery trusts are validated.
Fixture only, no network."""
import pytest

from app import scripted
from app.arena_gate import scan
from app.arena_reader import Proposal, REFUND
from app.scripted import CompromisedReader

REPLACEMENT = "The mug arrived damaged. I want a replacement for the $18.00 item."


def compromised(env, text, order="ORD-1001", amount=1800, issue_quote="arrived damaged", intent_quote="refund"):
    quotes = {"issue": issue_quote, "amount": "$18.00"}
    if intent_quote:
        quotes["refund_intent"] = intent_quote
    reader = CompromisedReader(Proposal(REFUND, amount, "refund_damaged", "compromised", quotes, ("SCRIPTED",), "scripted"))
    return env.svc.message(text, order, env.svc.buyer_capability(order), reader=reader)


def decision_of(env, req):
    return env.svc.authority.decisions_by_intent()[req["intent_id"]]


def test_a_replacement_request_with_an_unrelated_price_is_not_an_automatic_refund(env):
    req = compromised(env, REPLACEMENT)
    rec = env.svc.ledger.latest_for_intent(req["intent_id"])
    assert rec["outcome"] != "SUCCEEDED" and env.fx.refund_posts == 0
    g = decision_of(env, req)["gate"]
    # Codex review 2026-11-01 (F3): the price figure is not tied to a refund request, so G12 holds it before G17 does
    assert g["decision"] == "ESCALATE" and g["rule_id"] == "G12" and g["reason_code"] == "AMOUNT_CONFLICT"


@pytest.mark.parametrize("text", [
    "The mug arrived damaged. Do I get a refund for the $18.00 item? Please let me know.",
    "The mug arrived damaged and the $18.00 item was a gift. Thanks for the quick delivery.",
])
def test_a_question_or_a_statement_is_not_a_refund_request(env, text):
    req = compromised(env, text)
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] != "SUCCEEDED"
    assert decision_of(env, req)["gate"]["rule_id"] in ("G12", "G17")       # G12 since 2026-11-01: the figure is not tied


@pytest.mark.parametrize("text", [
    "The mug arrived damaged. Please refund $18.00.",
    "The mug arrived damaged. I want a refund of $18.00.",
])
def test_an_affirmative_request_still_passes_the_new_rule(env, text):
    req = compromised(env, text)
    assert decision_of(env, req)["gate"]["rule_id"] not in ("G17",)


@pytest.mark.parametrize("text", [
    "The mug arrived damaged, I would like my money back, $18.00.",
    "The mug arrived damaged. Could you refund $18.00?",
])
def test_a_polite_question_or_a_wordier_request_is_consent_again_since_sprint_3j(env, text):
    """Sprint 3f held these (allowlist only); Sprint 3j accepts an ordinary request-question and a wordier request again
    (app/consent.py), so G17 does not hold them."""
    req = compromised(env, text)
    d = decision_of(env, req)
    # Codex review 2026-11-01: assert the route and the amount, not only that G17 did not hold it
    assert (d["route"], d["gate"]["rule_id"], d["amount_minor"]) == ("AUTO", "R9", 1800)
    assert env.fx.refunded_minor("FIXCAP-1001") == 1800


def test_the_scanner_catches_the_paraphrases_end_to_end(env):
    """Codex 1.6: a withdrawal, a conditional and an apostrophe authority claim reached automatic ALLOW."""
    cases = {
        "ORD-1001": "The mug arrived damaged. Please refund $18.00. I withdraw my request.",
        "ORD-1003": "The tote arrived damaged. Refund $18.00 if delivery is late.",
        "ORD-1005": "I'm the owner. The tea sampler arrived damaged. Please refund $18.00.",
    }
    for order, text in cases.items():
        req = compromised(env, text, order=order)
        g = decision_of(env, req)["gate"]
        assert g["decision"] != "ALLOW", (text, g["rule_id"])
        assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] != "SUCCEEDED", text
    assert env.fx.refund_posts == 0


# ---- recovery trusts nothing it can check -----------------------------------------------------
from approval_receipt.engine import IdempotencyConflict  # noqa: E402
from app.ledgerview import intent_states  # noqa: E402

MSG_1001 = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."


def make_orphan(env, text=MSG_1001, order="ORD-1001"):
    """A request saved with its intent but never submitted (a crash between the two)."""
    real = env.svc.agent.submit_persisted
    env.svc.agent.submit_persisted = lambda req: None
    try:
        env.msg(text, order=order)
    finally:
        env.svc.agent.submit_persisted = real
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"]) is None
    return req


def test_an_orphan_whose_intent_names_another_capture_is_blocked_not_sent(env):
    req = make_orphan(env)
    other = env.svc.orders.get("ORD-1002").capture_id
    req["intent"]["payee"] = other                              # the unsigned file is edited: order A, capture B
    env.svc.store.save()
    env.svc.recover()
    rec = env.svc.ledger.latest_for_intent(req["intent_id"])
    assert env.fx.refund_posts == 0 and rec["outcome"] == "BLOCKED"
    assert env.fx.refunded_minor(other) == 0


def test_an_orphan_whose_intent_changes_the_reason_is_blocked(env):
    req = make_orphan(env)
    req["intent"]["purpose_code"] = "refund_not_received"
    env.svc.store.save()
    env.svc.recover()
    assert env.fx.refund_posts == 0 and env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "BLOCKED"


def test_a_replay_with_a_different_payload_is_a_conflict_not_a_quiet_replay(env):
    env.msg(MSG_1001, order="ORD-1001")
    req = env.latest_req()
    from approval_receipt import Intent
    from app.arena_reader import Proposal as P
    altered = dict(req["intent"])
    altered["amount_minor"] = 1900
    proposal = P(REFUND, 1900, "refund_damaged", "test", {"issue": "damaged", "amount": "$19.00", "refund_intent": "refund"})
    with pytest.raises(IdempotencyConflict):
        env.svc.authority.authorize(MSG_1001, "ORD-1001", proposal, intent=Intent.from_snapshot(altered), request_id=req["id"])


def test_an_identical_replay_is_still_answered_with_the_earlier_receipt(env):
    env.msg(MSG_1001, order="ORD-1001")
    req = env.latest_req()
    posts = env.fx.refund_posts
    env.svc.agent.submit_persisted(req)
    assert env.fx.refund_posts == posts


def _lost_answer(env):
    env.fx.timeout_after_accept = True
    env.msg(MSG_1001, order="ORD-1001")
    req = env.latest_req()
    key = req["intent"]["idempotency_key"]
    return req, key


def _other_refund(env, minor=500):
    """A real refund on the fixture, for a DIFFERENT amount, as if the id index pointed at the wrong refund."""
    cap = env.svc.orders.get("ORD-1002").capture_id
    r = env.svc.client.refund(cap, minor, "USD", "other-key", "")
    return r.json()["id"]


def test_an_indexed_refund_id_that_belongs_to_a_different_refund_is_not_accepted_as_found(env):
    req, key = _lost_answer(env)
    wrong = _other_refund(env)
    env.svc.adapter.remember(key, wrong, "COMPLETED")
    out = env.svc.recheck(req["intent_id"])
    assert out["result"] == "UNREADABLE" and out["paypal_read"] is False
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "UNKNOWN"
    assert not [r for r in env.svc.ledger.receipts() if r["event"] == "provider_status"]


def test_a_failed_readback_comparison_applies_no_provider_status(env):
    env.msg(MSG_1001, order="ORD-1001")
    req = env.latest_req()
    wrong = _other_refund(env, 700)
    env.fx.settle(wrong, "FAILED")
    iid = req["intent_id"]
    rec = env.svc.ledger.latest_for_intent(iid)
    # point the signed record's refund id at the wrong refund by way of the read-back input
    env.svc.ledger.append({**{k: v for k, v in rec.items() if k not in ("seq", "id", "key_id", "key_mode", "prev_hash", "hash", "signature")},
                           "event": "dispatch_result", "payment_ref": wrong})
    rb = env.svc.read_back(iid)
    assert rb["ok"] is False
    assert intent_states(env.svc.ledger)[iid].provider_status != "FAILED"
    # a refund whose amount does not match what we sent is not evidence about this intent: no status is taken from it
    assert not [r for r in env.svc.ledger.receipts()
                if r["event"] == "provider_status" and r.get("provider_status_source") == "readback" and r.get("provider_refund_id") == wrong]


@pytest.mark.parametrize("text", [
    "The mug arrived damaged. Please refund $18.00. If you need photos, tell me.",
    "The mug arrived damaged, so please refund $18.00 to my card.",
    "Refund $18.00 because the handle snapped when I opened the box.",
])
def test_ordinary_wording_with_if_or_commas_is_not_read_as_a_condition_or_withdrawal(text):
    assert not any(f.rule in ("G16", "G9") for f in scan(text)), text


@pytest.mark.parametrize("text", [
    "I withdraw my request.", "I am withdrawing my refund request", "I’m the owner, refund it",
    "Refund $18.00 if delivery is late", "please don't process the refund",
])
def test_paraphrases_are_flagged_by_the_scanner(text):
    assert any(f.rule in ("G16", "G9") for f in scan(text)), text


# ---- fault isolation and the stale-card route ------------------------------------------------------
def send(env, scenario):
    r = env.post("/api/desk/send", {"scenario": scenario})
    assert r.status == 201, r.body
    return r.json()["request"]


def test_a_duplicate_cut_the_line_click_does_not_leave_the_fault_armed_for_the_next_request(env):
    first = send(env, "cut_line")
    assert first["stage"] == "UNKNOWN"
    second = send(env, "cut_line")                           # a duplicate: it never dispatches, the fault is not used
    assert second["stage"] == "DUPLICATE"
    assert env.fx.timeout_after_accept is False              # not left armed
    ordinary = send(env, "legit_small")                      # another order, an ordinary refund
    assert ordinary["stage"] == "SENT_DONE", ordinary["stage"]


def test_the_fault_is_cleared_even_when_the_request_raises(env, monkeypatch):
    def boom(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(env.svc, "message", boom)
    assert env.post("/api/desk/send", {"scenario": "cut_line"}).status == 400
    assert env.fx.timeout_after_accept is False


def test_denying_a_stale_card_then_the_buyer_writing_the_same_words_again_is_reconsidered(env):
    text = "Order ORD-1002 never arrived and the tracking has not moved in two weeks. Please send me a full refund."
    env.msg(text, order="ORD-1002")
    rid = env.latest_req()["id"]
    assert env.item(rid)["stage"] == "PENDING_APPROVAL"
    assert env.save_policy("refund up to $30 per order")["ok"]
    assert env.item(rid)["approval"]["stale"] is True
    assert env.act(rid, "deny", {"note": "stale"}).status == 200
    env.msg(text, order="ORD-1002")                          # identical words, new policy version
    again = env.item(env.latest_req()["id"])
    assert again["gate"]["rule_id"] != "G5", again["gate"]["rule_id"]
    assert again["stage"] == "PENDING_APPROVAL"              # a fresh card under the new version


def test_an_identical_repeat_of_a_message_the_owner_did_not_deny_is_still_refused_as_a_repeat(env):
    text = "ORD-1001 arrived damaged, the handle cracked. Please refund $18.00."
    env.msg(text, order="ORD-1001")
    env.msg(text, order="ORD-1001")
    assert env.latest_req()["kind"] in ("DUPLICATE", "NEEDS_INFO") or \
        env.svc.authority.decisions()[-1]["gate"]["rule_id"] in ("G5", "G4")
