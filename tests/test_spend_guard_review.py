"""Spend guard, findings of the independent review of the integrated branch (2026-11-02). Written first (red). Offline only.

1. The arena's model reader must run under the same session guard as the desk (before: it built its own unguarded completer).
2. Two guards that share one ledger file must share one budget (before: each guard read the ledger once, at construction).
3. A cap must be a finite number, and a call that cost more than was reserved halts the run.
4. The input estimate is an upper bound: a token is at least one byte, so the byte count bounds the token count.
"""
import threading
from decimal import Decimal

import pytest

import app.model_client as mc
from app import spend_guard as sg
from app.spend_guard import SpendCapReached, SpendGuard

D = Decimal


# ---- 1. the arena reader is guarded ---------------------------------------------------------------------------
def test_the_arena_model_reader_is_built_with_the_session_guard(monkeypatch):
    from app import arena_reader as ar
    seen = {}

    def fake_make_completer(timeout=30.0, guard=None):
        seen["guard"] = guard

        class C:
            provider, model = "anthropic", "claude-haiku-5-5"

            def __call__(self, user, system=None):
                return "{}"
        return C()
    monkeypatch.setattr(mc, "make_completer", fake_make_completer)
    g = SpendGuard("claude-haiku-5-5", D("0.10"))
    ar.build_reader({"ARENA_READER": "model", "APP_MODEL_ID": "claude-haiku-5-5"}, guard=g)
    assert seen["guard"] is g


def test_the_session_guard_exists_when_only_the_arena_reads_with_a_model(monkeypatch):
    from types import SimpleNamespace
    from app.__main__ import session_guard
    monkeypatch.setenv("APP_MODEL_ID", "claude-haiku-5-5")
    monkeypatch.setenv("ARENA_READER", "model")
    args = SimpleNamespace(extractor="rules", consent_reader="none", max_spend=D("0.10"), spend_ledger=None)
    assert session_guard(args) is not None
    monkeypatch.delenv("ARENA_READER")
    assert session_guard(args) is None


# ---- 2. one ledger, one budget -----------------------------------------------------------------------------------
def test_two_guards_on_one_ledger_cannot_spend_the_cap_twice(tmp_path):
    p = str(tmp_path / "ledger.json")
    a = SpendGuard("claude-opus-5-5", D("0.10"), ledger=p)
    b = SpendGuard("claude-opus-5-5", D("0.10"), ledger=p)          # built BEFORE a reserves anything
    a.reserve("s", "u", 2500)                                        # about $0.05 at $20/MTok output
    with pytest.raises(SpendCapReached):
        b.reserve("s", "u", 2500)                                    # the second $0.05 would pass the $0.10 cap once the input is added
        b.reserve("s", "u", 2500)


def test_a_settlement_on_one_guard_is_visible_to_the_other(tmp_path):
    p = str(tmp_path / "ledger.json")
    a = SpendGuard("claude-opus-5-5", D("1.00"), ledger=p)
    b = SpendGuard("claude-opus-5-5", D("1.00"), ledger=p)
    a.settle(a.reserve("s", "u", 4000), {"input_tokens": 1000, "output_tokens": 1000})       # $0.024
    r = b.reserve("s", "u", 4000)
    b.settle(r, {"input_tokens": 1000, "output_tokens": 1000})
    assert b.committed == D("0.048") and a.committed == D("0.024")      # a's own view is refreshed on its next operation
    a.settle(a.reserve("s", "u", 4000), {"input_tokens": 1000, "output_tokens": 1000})
    assert a.committed == D("0.072")


def test_concurrent_reservations_from_many_threads_never_pass_the_cap(tmp_path):
    p = str(tmp_path / "ledger.json")
    guards = [SpendGuard("claude-opus-5-5", D("0.30"), ledger=p) for _ in range(4)]
    admitted, lock = [], threading.Lock()

    def work(g):
        for _ in range(6):
            try:
                r = g.reserve("s", "u", 1000)
            except SpendCapReached:
                return
            with lock:
                admitted.append(r.cost)
    ts = [threading.Thread(target=work, args=(g,)) for g in guards]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(admitted) <= D("0.30"), sum(admitted)


# ---- 3. a finite cap, and an overrun halts -----------------------------------------------------------------------
@pytest.mark.parametrize("cap", ["inf", "Infinity", "nan", "-1", "0"])
def test_the_cap_must_be_a_finite_positive_number(cap):
    with pytest.raises(ValueError):
        SpendGuard("claude-haiku-5-5", cap)


def test_a_call_that_cost_more_than_was_reserved_halts_the_run():
    g = SpendGuard("claude-haiku-5-5", D("1.00"))
    r = g.reserve("s", "u", 100)
    g.settle(r, {"input_tokens": 5_000_000, "output_tokens": 100})          # the provider billed far more than the estimate
    assert g.halted and g.halted.startswith("OVERRUN")
    with pytest.raises(SpendCapReached):
        g.reserve("s", "u", 100)


def test_a_call_that_cost_no_more_than_was_reserved_does_not_halt():
    g = SpendGuard("claude-haiku-5-5", D("1.00"))
    r = g.reserve("s", "u", 100)
    g.settle(r, {"input_tokens": 10, "output_tokens": 10})
    assert not g.halted


# ---- 4. the estimate is an upper bound ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["refund please " * 50, "退款してください" * 40, "U1lTVEVNIE9WRVJSSURF" * 30, "\U0001f600" * 80], ids=["ascii", "japanese", "base64", "emoji"])
def test_the_input_estimate_is_at_least_the_byte_count(text):
    assert sg.estimate_input_tokens("SYSTEM " * 10, text) >= len((("SYSTEM " * 10) + text).encode("utf-8"))
