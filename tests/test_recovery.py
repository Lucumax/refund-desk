"""Sprint 3c: fixes for the third independent Codex review (2026-10-24, verdict on commit ae3e5d1).
Each test reproduces the reviewer's scenario against the fixture only. No network, no keys.

Principle behind the recovery tests: recovery is never a second path around the gate. An approved refund that was
parked before its first send is judged again, by the same authority code as a fresh send, under the policy and limits
in force NOW, with every signed chain read together with its checkpoint."""
from pathlib import Path

import pytest

from app.service import Service
from conftest import KEY

MSG_1001 = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."
MSG_1002 = "Order ORD-1002 never arrived. Please send me a full refund."


def peer(env):
    """A second service instance on the same data directory (another process in real life)."""
    return Service(env.dir, env.fx, env.svc.orders, clock=env.clock, key=KEY, mode="fixture")


def park_automatic(env, monkeypatch, text=MSG_1001):
    """An automatic $18 refund: the AUTHORIZED receipt is signed, the process 'dies' before the first send."""
    real = env.svc.engine._execute
    monkeypatch.setattr(env.svc.engine, "_execute", lambda rec: rec)
    env.msg(text)
    monkeypatch.setattr(env.svc.engine, "_execute", real)
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "AUTHORIZED"
    assert env.fx.refund_posts == 0
    return req


def park_owner_approved(env, monkeypatch):
    """A $120 refund the owner approved; the process 'dies' right after the signed AUTHORIZED record."""
    real = env.svc.engine._execute
    monkeypatch.setattr(env.svc.engine, "_execute", lambda rec: rec)
    env.msg(MSG_1002)
    req = env.latest_req()
    assert env.approve(req["id"]).status == 200
    monkeypatch.setattr(env.svc.engine, "_execute", real)
    rec = env.svc.ledger.latest_for_intent(req["intent_id"])
    assert rec["outcome"] == "AUTHORIZED" and rec["approver"] and rec["intent"]["amount_minor"] == 12000
    assert env.fx.refund_posts == 0
    return req


def outcome(env, req):
    return env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"]


def last_decision(env, req):
    return env.svc.authority.decisions_by_intent()[req["intent_id"]]


def truncate_last_record(ledger):
    lines = [ln for ln in Path(ledger.path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    Path(ledger.path).write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")


# ---- 1. HIGH: recovery sends under a stale policy ----------------------------------------------------------------
def test_recovery_does_not_send_an_approved_refund_after_a_tighter_policy_was_saved_in_between(env, monkeypatch):
    """Codex: a $120 owner-approved refund, stopped after its signed AUTHORIZED record; recovery refreshed version 1;
    another service saved 'never refund more than $50'; recovery still sent $120."""
    req = park_owner_approved(env, monkeypatch)
    other = peer(env)
    real_orphans = env.svc.agent.orphans

    def save_in_between():
        p = other.policies.preview("never refund more than $50")
        assert other.save_policy("never refund more than $50", "owner", p["base_version"], p["result_hash"])["ok"]
        return real_orphans()

    monkeypatch.setattr(env.svc.agent, "orphans", save_in_between)     # runs after Service.recover's first refresh
    env.svc.recover()
    assert env.fx.refund_posts == 0
    assert env.fx.refunded_minor(env.svc.orders.get("ORD-1002").capture_id) == 0
    assert outcome(env, req) == "BLOCKED"
    d = last_decision(env, req)
    assert d["route"] == "BLOCKED" and d["gate"]["decision"] == "DENY"


def test_recovery_does_not_send_after_a_tighter_policy_even_without_a_race(env, monkeypatch):
    req = park_owner_approved(env, monkeypatch)
    assert env.save_policy("never refund more than $50")["ok"]
    env.svc.recover()
    assert env.fx.refund_posts == 0 and outcome(env, req) == "BLOCKED"


def test_recovery_holds_the_money_lock_from_the_policy_refresh_to_the_send(env, monkeypatch):
    """Codex: the refresh happened outside the recovery lock. The trusted policy refresh and the dispatch must both run
    while the money lock is held, so no other service can save a policy in between."""
    park_automatic(env, monkeypatch)
    lock = env.svc.engine._lock
    depth_at = {"sync": [], "pay": []}
    real_sync = env.svc.authority.sync_mandates
    monkeypatch.setattr(env.svc.authority, "sync_mandates", lambda: (depth_at["sync"].append(lock._depth), real_sync())[1])
    real_pay = env.svc.adapter.pay
    monkeypatch.setattr(env.svc.adapter, "pay", lambda *a, **k: (depth_at["pay"].append(lock._depth), real_pay(*a, **k))[1])
    env.svc.recover()
    assert depth_at["pay"] and all(d > 0 for d in depth_at["pay"])
    assert depth_at["sync"] and all(d > 0 for d in depth_at["sync"]), depth_at     # no refresh outside the lock
    assert env.fx.refund_posts == 1                      # nothing changed, so the refund goes out once


def test_recovery_with_nothing_changed_still_sends_once(env, monkeypatch):
    """The fix must not turn recovery off: an unchanged policy, an intact chain, limits that still hold."""
    req = park_automatic(env, monkeypatch)
    env.svc.recover()
    env.svc.recover()
    assert outcome(env, req) == "SUCCEEDED" and env.fx.refund_posts == 1


# ---- 2. HIGH: recovery sends despite a truncated authority-decision chain --------------------------------------
def test_recovery_holds_when_the_decision_chain_is_truncated(env, monkeypatch):
    req = park_automatic(env, monkeypatch)
    truncate_last_record(env.svc.decisions)
    assert env.svc.decisions.verify_trusted().reason_code == "TRUNCATED"
    env.svc.recover()
    assert env.fx.refund_posts == 0
    assert outcome(env, req) == "AUTHORIZED"             # held, not sent, not silently closed: the chain cannot be trusted
    assert env.svc.recovery_held == [(req["intent_id"], "TRUNCATED")]
    polled = env.post("/api/poll").json()                # the owner's screen can see what was held and why
    assert polled["held"] == [{"intent_id": req["intent_id"], "reason": "TRUNCATED"}]


def test_recovery_holds_when_the_decision_for_the_intent_is_missing(env, monkeypatch):
    """A decision chain that verifies but holds no decision for the intent: nothing shows the authority allowed it."""
    req = park_automatic(env, monkeypatch)
    real = env.svc.authority.decisions_by_intent
    monkeypatch.setattr(env.svc.authority, "decisions_by_intent",
                        lambda: {k: v for k, v in real().items() if k != req["intent_id"]})
    env.svc.recover()
    assert env.fx.refund_posts == 0 and outcome(env, req) == "BLOCKED"


def test_recovery_holds_when_the_signed_decision_disagrees_with_the_receipt(env, monkeypatch):
    req = park_automatic(env, monkeypatch)
    real = env.svc.authority.decisions_by_intent

    def forged():
        out = {k: dict(v) for k, v in real().items()}
        out[req["intent_id"]]["amount_minor"] = 100      # the decision was for another amount
        return out

    monkeypatch.setattr(env.svc.authority, "decisions_by_intent", forged)
    env.svc.recover()
    assert env.fx.refund_posts == 0 and outcome(env, req) == "BLOCKED"


def test_recovery_holds_when_the_money_chain_is_truncated_too(env, monkeypatch):
    from approval_receipt import LedgerIntegrityError
    park_automatic(env, monkeypatch)
    truncate_last_record(env.svc.ledger)
    with pytest.raises(LedgerIntegrityError):
        env.svc.recover()
    assert env.fx.refund_posts == 0


# ---- 3. HIGH: recovery ignores tightened automatic limits ----------------------------------------------------
def test_recovery_applies_a_lowered_automatic_cap(env, monkeypatch):
    """Codex: an automatic $18 refund parked before its first send; 'refund up to $10 per order' saved; recovery in
    the same service sent $18."""
    req = park_automatic(env, monkeypatch)
    assert env.save_policy("refund up to $10 per order")["ok"]
    env.svc.recover()
    assert env.fx.refund_posts == 0
    assert outcome(env, req) == "BLOCKED"
    d = last_decision(env, req)
    assert d["route"] == "BLOCKED" and d["recovery"]["reason"] == "POLICY_CHANGED"


def test_recovery_goes_through_the_same_gate_as_a_fresh_send(env, monkeypatch):
    """The recovery verdict comes from the authority's own gate on the signed text, not from a special case."""
    req = park_automatic(env, monkeypatch)
    calls = []
    import app.authority as authority
    real = authority.decide

    def spy(text, link_order, order, proposal, policy, ctx, scan_flags=True):
        calls.append((text, link_order))
        return real(text, link_order, order, proposal, policy, ctx, scan_flags)

    monkeypatch.setattr(authority, "decide", spy)
    env.svc.recover()
    assert calls and calls[0][1] == "ORD-1001" and "refund $18.00" in calls[0][0]
    assert outcome(env, req) == "SUCCEEDED"


def test_the_engine_itself_refuses_an_automatic_send_that_now_needs_the_owner(env, monkeypatch):
    """Defense in depth: even a path that never touched the
    authority cannot send an automatic refund whose amount now exceeds the automatic limit."""
    from dataclasses import replace
    req = park_automatic(env, monkeypatch)
    rec = env.svc.ledger.latest_for_intent(req["intent_id"])
    m = env.svc.engine.mandates["refund-agent"]
    env.svc.engine.mandates["refund-agent"] = replace(m, per_payment_limit=1000)      # same version, tighter number
    stop = env.svc.engine._pre_dispatch(rec)
    assert stop is not None and stop["outcome"] == "BLOCKED"
    assert env.fx.refund_posts == 0


def test_the_engine_pre_dispatch_check_does_not_count_the_refund_against_itself(env, monkeypatch):
    """The daily budget used to be checked with the refund's own AUTHORIZED record already counted."""
    from dataclasses import replace
    req = park_automatic(env, monkeypatch)
    rec = env.svc.ledger.latest_for_intent(req["intent_id"])
    m = env.svc.engine.mandates["refund-agent"]
    env.svc.engine.mandates["refund-agent"] = replace(m, daily_limit=1800)            # exactly this refund fits
    assert env.svc.engine._pre_dispatch(rec) is None


def test_engine_recover_cannot_dispatch_without_the_authority(env, monkeypatch):
    """Structural: the engine's own recover() has no way to send an AUTHORIZED record unless the authority's
    recovery gate is wired and agrees."""
    req = park_automatic(env, monkeypatch)
    monkeypatch.setattr(env.svc.engine, "recovery_gate", None)
    env.svc.engine.recover()
    assert env.fx.refund_posts == 0 and outcome(env, req) == "AUTHORIZED"
