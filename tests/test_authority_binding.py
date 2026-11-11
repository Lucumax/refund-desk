"""Sprint 3d: fixes for the fourth independent Codex review (2026-10-24, verdict on 9079329). Fixture only, no network.

Root-cause principle behind every test here: EVERY outward refund send, and every approve or deny action, is bound to
a SIGNED authority decision for that exact intent and amount, with a single-use binding. Nothing unsigned (a request
file, a pointer, an id) may select or authorize an action."""
from copy import deepcopy
from threading import Event, Thread

import pytest

from app import views
from app.arena_reader import Proposal, REFUND
from app.scripted import CompromisedReader
from conftest import KEY
from test_recovery import park_automatic, peer

MSG_10 = "Order ORD-1001 arrived damaged. Please refund $10.00."
MSG_18 = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."
MSG_1002 = "Order ORD-1002 never arrived. Please send me a full refund."


def outcome(env, intent_id):
    return env.svc.ledger.latest_for_intent(intent_id)["outcome"]


def park_orphan(env, monkeypatch, text):
    """A request saved (unsigned) but never submitted, as after a crash."""
    real = env.svc.agent.submit_persisted
    monkeypatch.setattr(env.svc.agent, "submit_persisted", lambda req: None)
    env.msg(text)
    monkeypatch.setattr(env.svc.agent, "submit_persisted", real)
    return env.latest_req()


def propose(env, text):
    reader = CompromisedReader(Proposal(REFUND, 1800, "refund_damaged", "compromised",
                                       {"issue": "arrived damaged", "amount": "$18.00", "refund_intent": "refund"}))
    return env.svc.message(text, "ORD-1001", env.svc.buyer_capability("ORD-1001"), reader=reader)


# ---- 1. HIGH: a send is bound to a signed decision for ITS intent; an orphan cannot borrow another request's id -----
def test_a_forged_orphan_request_id_cannot_reuse_a_prior_decision_to_send_a_second_refund(env, monkeypatch):
    """Codex: after a completed $10 refund, an identical second request was parked before submission and its id changed
    to the first request's id. Recovery sent a second $10 refund; the decision log held only the first intent."""
    env.msg(MSG_10)
    first = env.latest_req()
    assert outcome(env, first["intent_id"]) == "SUCCEEDED" and env.fx.refund_posts == 1
    orphan = park_orphan(env, monkeypatch, MSG_10)
    assert orphan["intent_id"] != first["intent_id"]
    orphan["id"] = first["id"]
    env.svc.store.save()
    env.svc.recover()
    assert env.fx.refund_posts == 1 and env.fx.refunds_created == 1          # no second refund
    assert outcome(env, orphan["intent_id"]) == "BLOCKED"
    dec = env.svc.authority.decisions_by_intent()[orphan["intent_id"]]          # the refusal is itself signed
    assert dec["route"] == "BLOCKED" and dec["intent_id"] == orphan["intent_id"]
    assert env.svc.authority.decisions_by_intent()[first["intent_id"]]["route"] == "AUTO"
    assert env.svc.verify_all()["ok"] is True


def test_a_second_recovery_pass_still_sends_nothing_for_the_forged_orphan(env, monkeypatch):
    env.msg(MSG_10)
    first = env.latest_req()
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["id"] = first["id"]
    env.svc.recover()
    env.svc.recover()
    assert env.fx.refund_posts == 1 and outcome(env, orphan["intent_id"]) == "BLOCKED"


def test_the_first_request_keeps_its_own_signed_story_after_the_forgery(env, monkeypatch):
    env.msg(MSG_10)
    first = env.latest_req()
    orphan = park_orphan(env, monkeypatch, MSG_10)
    orphan["id"] = first["id"]
    env.svc.recover()
    v = env.item(first["id"])
    assert v["stage"] == "SENT_DONE" and v["intent_id"] == first["intent_id"] and v.get("pointer_flag") is None


def test_a_request_id_already_bound_to_another_intent_is_refused_by_the_authority_itself(env):
    """Direct call: a new intent under a request id that a signed decision already ties to another intent."""
    env.msg(MSG_10)
    first = env.latest_req()
    from approval_receipt import Intent
    from dataclasses import replace
    other = replace(Intent.from_snapshot(first["intent"]), id="I-0900", idempotency_key="00000000-0000-4000-8000-000000000900")
    proposal = Proposal.from_dict(first["proposal"])
    res = env.svc.authority.authorize(first["text"], first["order_ref"], proposal, intent=other, request_id=first["id"])
    assert res.route == "BLOCKED" and res.outcome == "BLOCKED" and env.fx.refund_posts == 1


def test_an_orphan_with_no_decision_and_its_own_ids_is_still_recovered_and_sent_exactly_once(env, monkeypatch):
    """The fix must not turn recovery off: a genuine orphan has no decision under any id and is judged and sent once."""
    orphan = park_orphan(env, monkeypatch, MSG_18)
    assert env.fx.refund_posts == 0
    env.svc.recover()
    env.svc.recover()
    assert env.fx.refund_posts == 1 and outcome(env, orphan["intent_id"]) == "SUCCEEDED"
    decs = [d for d in env.svc.authority.decisions() if d.get("intent_id") == orphan["intent_id"]]
    assert len(decs) == 1 and decs[0]["request_id"] == orphan["id"]


def test_a_decision_written_before_a_crash_is_reused_for_the_same_intent_and_sends_once(env, monkeypatch):
    """Decision signed, process stopped before the receipt: the retry of the SAME intent, key and request id reuses the
    decision (one decision, one send)."""
    real = env.svc.engine.submit
    calls = {"n": 0}

    def crash_once(intent):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("process stopped")
        return real(intent)

    monkeypatch.setattr(env.svc.engine, "submit", crash_once)
    with pytest.raises(RuntimeError):
        env.msg(MSG_18)
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"]) is None and env.fx.refund_posts == 0
    env.svc.recover()
    assert env.fx.refund_posts == 1 and outcome(env, req["intent_id"]) == "SUCCEEDED"
    assert len([d for d in env.svc.authority.decisions() if d.get("intent_id") == req["intent_id"]]) == 1


def test_an_orphan_whose_key_belongs_to_another_intent_does_not_stop_recovery_of_the_others(env, monkeypatch):
    env.msg(MSG_10)
    first = env.latest_req()
    bad = park_orphan(env, monkeypatch, MSG_10)
    bad["intent"]["idempotency_key"] = first["intent"]["idempotency_key"]
    good = park_orphan(env, monkeypatch, "Order ORD-1003 arrived damaged. Please refund $5.00.")
    env.svc.recover()                                       # must not raise
    assert outcome(env, good["intent_id"]) == "SUCCEEDED"
    assert env.svc.ledger.latest_for_intent(bad["intent_id"]) is None and env.fx.refund_posts == 2
    assert (bad["intent_id"], "IDEMPOTENCY_CONFLICT") in env.svc.recovery_held


# ---- 2. MEDIUM: an UNKNOWN re-send goes through the same authority path as recovery -------------------------------
def unknown_owner_approved(env):
    env.msg(MSG_1002)
    req = env.latest_req()
    env.fx.timeout_before_accept = True
    assert env.approve(req["id"]).status == 200
    assert outcome(env, req["intent_id"]) == "UNKNOWN"
    return req


def refused_resends(env, intent_id):
    return [r for r in env.svc.ledger.receipts() if r["intent"]["id"] == intent_id and r["event"] == "resend_refused"]


def test_an_unknown_resend_is_refused_after_a_peer_saved_a_tighter_policy(env):
    """Codex: a $120 owner-approved refund timed out before acceptance; a peer saved a $50 ceiling; the retry created
    the $120 refund under mandate version 1 while policy version 2 was active."""
    req = unknown_owner_approved(env)
    other = peer(env)
    p = other.policies.preview("never refund more than $50")
    assert other.save_policy("never refund more than $50", "owner", p["base_version"], p["result_hash"])["ok"]
    env.svc.recheck(req["intent_id"])
    before = env.fx.refund_posts
    env.svc.retry(req["intent_id"])
    assert env.fx.refund_posts == before and env.fx.refunds_created == 0
    assert outcome(env, req["intent_id"]) == "UNKNOWN"                        # the hold stays
    assert refused_resends(env, req["intent_id"])[-1]["resend_refused"]["reason"] == "POLICY_CHANGED"


def test_an_unknown_resend_is_refused_when_the_decision_chain_is_truncated(env):
    """Codex: a re-send went out while the decision chain verified as TRUNCATED."""
    from pathlib import Path
    env.fx.timeout_before_accept = True
    env.msg(MSG_18)
    req = env.latest_req()
    assert outcome(env, req["intent_id"]) == "UNKNOWN"
    env.svc.recheck(req["intent_id"])
    p = Path(env.svc.decisions.path)
    p.write_text(p.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    assert env.svc.decisions.verify_trusted().reason_code == "TRUNCATED"
    before = env.fx.refund_posts
    env.svc.retry(req["intent_id"])
    assert env.fx.refund_posts == before and env.fx.refunds_created == 0
    assert outcome(env, req["intent_id"]) == "UNKNOWN"
    assert refused_resends(env, req["intent_id"])[-1]["resend_refused"]["reason"] == "TRUNCATED"


def test_an_unknown_resend_is_refused_when_the_signed_decision_is_missing(env, monkeypatch):
    env.fx.timeout_before_accept = True
    env.msg(MSG_18)
    req = env.latest_req()
    env.svc.recheck(req["intent_id"])
    real = env.svc.authority.decisions_by_intent
    monkeypatch.setattr(env.svc.authority, "decisions_by_intent", lambda: {k: v for k, v in real().items() if k != req["intent_id"]})
    env.svc.retry(req["intent_id"])
    assert env.fx.refunds_created == 0 and outcome(env, req["intent_id"]) == "UNKNOWN"
    assert refused_resends(env, req["intent_id"])[-1]["resend_refused"]["reason"] == "NO_DECISION_RECORD"


def test_an_unknown_resend_with_nothing_changed_still_goes_out_once_through_the_authority(env, monkeypatch):
    env.fx.timeout_before_accept = True
    env.msg(MSG_18)
    req = env.latest_req()
    env.svc.recheck(req["intent_id"])
    seen = []
    real = env.svc.authority._recovery_verdict
    monkeypatch.setattr(env.svc.authority, "_recovery_verdict", lambda *a, **k: (seen.append(a[0].id), real(*a, **k))[1])
    env.svc.retry(req["intent_id"])
    assert seen == [req["intent_id"]]                                      # the same verdict recovery uses
    assert outcome(env, req["intent_id"]) == "SUCCEEDED" and env.fx.refunds_created == 1


# ---- 3. MEDIUM: approve and deny act only on the intent the SIGNED records tie to the request --------------------
def two_pending(env):
    first = propose(env, "The mug arrived damaged. Please refund $18.00 if the courier admits fault.")
    second = propose(env, "The mug arrived damaged. Please refund $18.00 if the courier confirms the damage.")
    assert env.item(first["id"])["stage"] == "PENDING_APPROVAL"
    return first, second


def swap_pointer(first, second):
    first["intent_id"], first["intent"] = second["intent_id"], deepcopy(second["intent"])


def test_deny_does_not_act_on_a_different_intent_through_the_unsigned_pointer(env):
    """Codex: the display showed the first signed intent, a POST to the first request's Deny denied the SECOND."""
    first, second = two_pending(env)
    aid, bid = first["intent_id"], second["intent_id"]
    swap_pointer(first, second)
    v = env.item(first["id"])
    assert v["intent_id"] == aid and v["pointer_flag"] == "UNSIGNED_POINTER_IGNORED"
    r = env.post(f"/api/requests/{first['id']}/deny", {"intent_id": aid, "amount_minor": 1800})
    assert r.status == 409 and r.json()["error"] == "STATE_MISMATCH"
    assert outcome(env, aid) == "PENDING_APPROVAL" and outcome(env, bid) == "PENDING_APPROVAL"


def test_deny_aimed_at_the_pointer_intent_is_refused_too(env):
    first, second = two_pending(env)
    aid, bid = first["intent_id"], second["intent_id"]
    swap_pointer(first, second)
    r = env.post(f"/api/requests/{first['id']}/deny", {"intent_id": bid, "amount_minor": 1800})
    assert r.status == 409
    assert outcome(env, aid) == "PENDING_APPROVAL" and outcome(env, bid) == "PENDING_APPROVAL"


def test_approve_does_not_act_on_a_different_intent_through_the_unsigned_pointer(env):
    first, second = two_pending(env)
    aid, bid = first["intent_id"], second["intent_id"]
    card = env.item(second["id"])["approval"]
    swap_pointer(first, second)
    r = env.post(f"/api/requests/{first['id']}/approve", {"intent_id": bid, "amount_minor": 1800,
                                                             "nonce": card["nonce"], "digest": card["digest"]})
    assert r.status == 409
    assert outcome(env, aid) == "PENDING_APPROVAL" and outcome(env, bid) == "PENDING_APPROVAL" and env.fx.refund_posts == 0


def test_deny_and_approve_need_the_exact_binding_shown_on_the_card(env):
    first, _ = two_pending(env)
    iid = first["intent_id"]
    for body in ({}, {"intent_id": iid}, {"amount_minor": 1800}, {"intent_id": "I-9999", "amount_minor": 1800},
                 {"intent_id": iid, "amount_minor": 1}):
        r = env.post(f"/api/requests/{first['id']}/deny", body)
        assert r.status == 409 and r.json()["error"] == "STATE_MISMATCH", body
    assert outcome(env, iid) == "PENDING_APPROVAL"
    r = env.post(f"/api/requests/{first['id']}/deny", {"intent_id": iid, "amount_minor": 1800, "note": "no"})
    assert r.status == 200 and outcome(env, iid) == "DENIED"


def test_deny_with_no_signed_authority_decision_for_the_intent_is_refused_by_the_service(env, monkeypatch):
    from approval_receipt import ApprovalError
    first, _ = two_pending(env)
    real = env.svc.authority.decisions_by_intent
    monkeypatch.setattr(env.svc.authority, "decisions_by_intent", lambda: {k: v for k, v in real().items() if k != first["intent_id"]})
    session = env.svc.auth.session(env.headers["cookie"].split("=", 1)[1])
    with pytest.raises(ApprovalError) as e:
        env.svc.deny(first["intent_id"], session, "no")
    assert e.value.code == "NO_AUTHORITY_DECISION" and outcome(env, first["intent_id"]) == "PENDING_APPROVAL"


def test_the_desk_recheck_and_readback_follow_the_signed_association_too(env):
    env.msg(MSG_18)
    first = env.latest_req()
    env.msg(MSG_1002)
    second = env.latest_req()
    swap_pointer(first, second)
    assert env.post(f"/api/desk/readback/{first['id']}").status in (404, 409)


# ---- 4. MEDIUM: the shared policy cannot change between judgment and dispatch in the arena --------------------------
ARENA_MSG = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."


def save_ten_dollar_cap(env):
    p = env.svc.policies.preview("refund up to $10 per order")
    assert env.svc.save_policy("refund up to $10 per order", "owner", p["base_version"], p["result_hash"])["ok"]


def test_arena_fresh_send_cannot_dispatch_under_a_policy_saved_after_the_verdict(env, monkeypatch):
    """Codex: a $10 cap saved after an arena verdict but before dispatch still allowed an $18 automatic refund."""
    ar = env.app.arena()
    real_submit = ar.sim.engine.submit

    def save_then_submit(intent):
        save_ten_dollar_cap(env)
        return real_submit(intent)

    monkeypatch.setattr(ar.sim.engine, "submit", save_then_submit)
    out = env.post_public("/api/arena/attempt", {"text": ARENA_MSG, "order": "ORD-1001"})
    assert out.status == 201
    assert out.json()["released_minor"] == 0 and ar.provider.refund_posts == 0
    assert env.svc.policies.active()[0].per_order_cap_minor == 1000


def test_arena_recovery_cannot_dispatch_under_a_policy_saved_after_the_verdict(env, monkeypatch):
    ar = env.app.arena()
    real_execute = ar.sim.engine._execute
    monkeypatch.setattr(ar.sim.engine, "_execute", lambda rec: rec)
    ar.attempt(ARENA_MSG, "ORD-1001")
    monkeypatch.setattr(ar.sim.engine, "_execute", real_execute)
    real_verdict = ar.authority._recovery_verdict

    def save_after_verdict(*args, **kw):
        answer = real_verdict(*args, **kw)
        save_ten_dollar_cap(env)
        return answer

    monkeypatch.setattr(ar.authority, "_recovery_verdict", save_after_verdict)
    ar.sim.recover()
    assert ar.provider.refund_posts == 0


def test_a_policy_save_from_another_thread_waits_for_an_arena_dispatch_that_is_under_way(env, monkeypatch):
    """The lock side of the fix: a real concurrent save cannot pass between the arena's judgment and its dispatch."""
    ar = env.app.arena()
    real_submit = ar.sim.engine.submit
    entered, finished, result = Event(), Event(), []

    def save():
        entered.set()
        p = env.svc.policies.preview("refund up to $10 per order")
        result.append(env.svc.save_policy("refund up to $10 per order", "owner", p["base_version"], p["result_hash"]))
        finished.set()

    worker = Thread(target=save)

    def paused(intent):
        worker.start()
        assert entered.wait(2)
        assert not finished.wait(0.2)                        # the save is waiting on the arena's hold
        return real_submit(intent)

    monkeypatch.setattr(ar.sim.engine, "submit", paused)
    out = ar.attempt(ARENA_MSG, "ORD-1001")
    worker.join(5)
    assert finished.is_set() and result[0]["ok"]
    assert out["released_minor"] == 1800                      # judged and sent under ONE version
    assert env.svc.policies.active()[1]["version"] == 2


def test_desk_dispatch_is_stopped_by_the_same_version_check(env, monkeypatch):
    """The desk path has the same compare-and-swap right before dispatch (a save from outside the process lock)."""
    real_submit = env.svc.engine.submit

    def save_then_submit(intent):
        p = env.svc.policies.preview("refund up to $10 per order")
        assert env.svc.policies.save("refund up to $10 per order", "owner", p["base_version"], p["result_hash"])["ok"]
        return real_submit(intent)

    monkeypatch.setattr(env.svc.engine, "submit", save_then_submit)
    env.msg(MSG_18)
    req = env.latest_req()
    assert env.fx.refund_posts == 0 and outcome(env, req["intent_id"]) == "BLOCKED"


# ---- 5. PARTIAL items: consent look-alikes, and ids in the request file ----------------------------------------------
@pytest.mark.parametrize("tail", [
    "Can you refund $18.00 in principle, without actually doing it.",
    "I want a refund of $18.00 explained, not issued.",
    "The example reads: please refund $18.00. I am only quoting the example.",
    "If you can confirm that the courier lost it, please refund $18.00.",
    "If уou cаn confirm that the courier lost it, please rеfund $18.00.",
    "If you can confirm that the courier lost it, please re​fund $18.00.",
    "Please refund $18.00 when the courier confirms the damage.",
    "Please refund $18.00. Actually, hold off for now.",
])
def test_redteam_consent_lookalikes_are_not_an_automatic_refund(env, tail):
    req = propose(env, "The mug arrived damaged. " + tail)
    dec = env.svc.authority.decisions_by_intent().get(req["intent_id"])
    assert dec is not None and dec["gate"]["decision"] != "ALLOW", (tail, dec["gate"]["rule_id"])
    assert env.fx.refund_posts == 0


def test_a_plain_buyer_conditional_refund_without_a_compromised_reader_waits_for_the_owner(env):
    env.msg("Order ORD-1001 arrived damaged. Please refund $18.00 when the courier confirms the damage.")
    req = env.latest_req()
    if req.get("intent_id"):
        assert env.svc.authority.decisions_by_intent()[req["intent_id"]]["gate"]["decision"] != "ALLOW"
    assert env.fx.refund_posts == 0


@pytest.mark.parametrize("text", [
    "The mug arrived damaged. If you could, please refund $18.00.",
    "The mug arrived damaged. If you can, please refund $18.00.",
    "The mug was damaged when it arrived. Please refund $18.00.",
    "The mug arrived damaged. If you can refund $18.00 that would be great.",
])
def test_ordinary_polite_requests_are_held_by_the_allowlist_since_sprint_3f(text):
    """Coverage lowered on purpose (app/consent.py): a polite hedge or a remark about timing is not an allowlisted command."""
    from app.arena_gate import affirmative_refund_request
    assert not affirmative_refund_request(text), text


def sent_request(env):
    env.msg(MSG_18)
    req = env.latest_req()
    assert outcome(env, req["intent_id"]) == "SUCCEEDED"
    return req


def test_an_edited_request_id_does_not_hide_a_completed_refund(env):
    """Codex: changing the request's own id (and clearing its pointers) made a completed $18 refund say 'Nothing was sent'
    and the inbox total $0.00."""
    req = sent_request(env)
    key = req["id"]
    req.update(id="REQ-9999", intent_id=None, intent=None, kind="NEEDS_INFO", code="MISSING_AMOUNT")
    env.svc.store.save()
    v = env.item(key)
    assert v["stage"] in ("SENT_DONE", "SENT_PENDING") and v["pointer_flag"] is not None and v["actions"] == []
    assert "Nothing was sent" not in str(v["stamp"]) + v["headline"]
    inbox = env.inbox()
    assert inbox["sent_count"] == 1 and inbox["sent_total"] == "$18.00"
    assert views.buyer_view(env.svc, req)["stage"] in ("SENT_DONE", "SENT_PENDING")


def test_an_edited_request_key_and_id_still_leaves_the_signed_refund_in_the_inbox_and_totals(env):
    req = sent_request(env)
    store = env.svc.store.requests
    del store[req["id"]]
    req.update(id="REQ-9999", intent_id=None, intent=None, kind="NEEDS_INFO", code="MISSING_AMOUNT")
    store["REQ-9999"] = req
    env.svc.store.save()
    inbox = env.inbox()
    assert inbox["sent_count"] == 1 and inbox["sent_total"] == "$18.00"
    signed = [i for i in inbox["items"] if i["stage"] == "SENT_DONE"]
    assert len(signed) == 1 and signed[0]["pointer_flag"] == "REQUEST_RECORD_MISSING" and signed[0]["actions"] == []


def test_giving_another_request_a_completed_requests_id_does_not_fabricate_a_second_sent_card(env):
    """Codex: a forged id made a second successful card and a $36.00 total."""
    sent = sent_request(env)
    env.msg("Order ORD-1001 arrived damaged. How does your refund policy work?")
    other = env.latest_req()
    other_key = other["id"]
    other["id"] = sent["id"]
    env.svc.store.save()
    v = env.item(other_key)
    assert v["stage"] not in ("SENT_DONE", "SENT_PENDING") and v["intent_id"] is None
    inbox = env.inbox()
    assert inbox["sent_count"] == 1 and inbox["sent_total"] == "$18.00"


def test_a_second_request_pointing_at_a_sent_intent_is_not_a_second_sent_card(env):
    sent = sent_request(env)
    env.msg("Order ORD-1001 arrived damaged. How does your refund policy work?")
    other = env.latest_req()
    other.update(kind="SUBMITTED", intent_id=sent["intent_id"], intent=deepcopy(sent["intent"]))
    env.svc.store.save()
    inbox = env.inbox()
    assert inbox["sent_count"] == 1 and inbox["sent_total"] == "$18.00"
    assert env.item(other["id"])["stage"] == "STATE_MISMATCH"


# ---- 6. the prose scan ignores reviewer files placed in the tree -------------------------------------------------------
@pytest.mark.parametrize("name", ["PRIOR_REVIEW.md", "PRIOR_REVIEW_2026-10-24.md", "prior_review_notes.md", "CODEX_REVIEW.md",
                                  "codex_out.md", "my-codex-notes.md", "Codex.md", "review_9079329.md"])
def test_the_prose_scan_ignores_reviewer_files_in_the_tree(tmp_path, name):
    from test_copy_and_repo import prose_documents
    (tmp_path / name).write_text("reviewer prose with an em dash \u2014 and a range 3 \u2013 4 and 12 years\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("plain prose\n", encoding="utf-8")
    names = [p.name for p in map(__import__("pathlib").Path, prose_documents(str(tmp_path)))]
    assert names == ["README.md"], names
