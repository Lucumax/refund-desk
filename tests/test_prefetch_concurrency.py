"""Session 3 (2026-11-01): concurrent submits against the prefetch design. Fixture only: no network, no key.

Many buyers write at the same moment, the model reads overlap outside the app-wide lock, and the decisions are still made one
at a time. What must hold whatever the interleaving: no order is refunded past the automatic allowance, the day is not spent past
the daily budget, the fixture provider never sees a second refund for the same intent, every request carries ITS OWN model
exchange, and the throttle bounds the model calls of a flood."""
import json
import random
import threading
import time

from app.consent_doubles import ScriptedConsentClient, clean_reply
from app.consent_reader import ModelConsentReader

ORDERS = ["ORD-1001", "ORD-1002", "ORD-1003", "ORD-1005", "ORD-1006", "ORD-1007", "ORD-1008", "ORD-1009", "ORD-1010"]


def _text(order, dollars):
    return f"Order {order} arrived damaged, the handle is cracked. Please refund ${dollars}.00."


class JitterModel:
    """A provider stand-in that answers after a random short wait, so reads finish out of order."""
    supports_system = True
    provider, model = "scripted", "jitter-v1"

    def __init__(self, inner, seed=7):
        self.inner, self.calls, self._g, self._rng = inner, 0, threading.Lock(), random.Random(seed)

    def __call__(self, user, system=None):
        with self._g:
            self.calls += 1
            wait = self._rng.uniform(0.0, 0.05)
        time.sleep(wait)
        return self.inner(user, system=system)


def _run_together(jobs):
    """Start every job on its own thread behind one barrier; return the results in job order."""
    barrier, out, threads = threading.Barrier(len(jobs)), [None] * len(jobs), []

    def run(i, fn):
        barrier.wait(10)
        try:
            out[i] = fn()
        except BaseException as e:           # noqa: BLE001
            out[i] = e
    for i, fn in enumerate(jobs):
        t = threading.Thread(target=run, args=(i, fn), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive(), "a request never finished"
    return out


def test_many_concurrent_submits_never_pass_the_allowance_or_the_daily_budget(env):
    replies = {}
    for order in ORDERS:
        for d in (5, 6, 7, 8):
            replies[_text(order, d)] = clean_reply(f"Please refund ${d}.00")
    env.svc.agent.consent_reader = ModelConsentReader(JitterModel(ScriptedConsentClient(replies)))
    jobs = [(lambda o=o, d=d: env.post_public("/api/buyer/messages",
                                              {"text": _text(o, d), "order": o, "cap": env.svc.buyer_capability(o)}))
            for o in ORDERS for d in (5, 6, 7, 8)]
    random.Random(3).shuffle(jobs)
    results = _run_together(jobs)
    assert all(not isinstance(r, BaseException) and r.status == 201 for r in results), [r for r in results if not hasattr(r, "status") or r.status != 201]
    limits = env.svc.enforced_limits()
    per_order = {o: env.fx.refunded_minor(env.svc.orders.get(o).capture_id) for o in ORDERS}
    assert all(v <= limits["automatic_allowance_minor"] for v in per_order.values()), per_order
    assert sum(per_order.values()) <= limits["daily_budget_minor"], per_order
    assert env.fx.refunds_created == env.fx.refund_posts          # no second POST for an intent that was already dispatched
    assert len(env.svc.store.requests) == len(jobs)


def test_every_request_keeps_its_own_exchange_under_overlap(env):
    replies = {_text(o, d): clean_reply(f"Please refund ${d}.00") for o in ORDERS for d in (5, 6)}
    env.svc.agent.consent_reader = ModelConsentReader(JitterModel(ScriptedConsentClient(replies), seed=11))
    jobs = [(lambda o=o, d=d: env.post_public("/api/buyer/messages",
                                              {"text": _text(o, d), "order": o, "cap": env.svc.buyer_capability(o)}))
            for o in ORDERS for d in (5, 6)]
    results = _run_together(jobs)
    assert all(getattr(r, "status", None) == 201 for r in results)
    for req in env.svc.store.requests.values():
        quote = json.loads(req["consent_exchange"]["raw_reply"])["request_quote"]
        assert quote in req["text"], (req["id"], quote, req["text"])          # the reading belongs to this request's words


def test_a_flood_on_one_order_costs_at_most_the_throttle_in_model_calls(env):
    model = JitterModel(ScriptedConsentClient({}, default=clean_reply("Please refund $5.00")))
    env.svc.agent.consent_reader = ModelConsentReader(model)
    o = "ORD-1005"
    jobs = [(lambda n=n: env.post_public("/api/buyer/messages",
                                         {"text": _text(o, 5) + " " * n + "x" * n, "order": o, "cap": env.svc.buyer_capability(o)}))
            for n in range(40)]
    results = _run_together(jobs)
    codes = sorted(r.status for r in results)
    assert codes.count(201) == 5 and codes.count(429) == 35, codes
    assert model.calls <= 5, model.calls


def test_a_desk_send_racing_a_buyer_message_on_one_order_refunds_it_once(env):
    replies = {_text("ORD-1001", 18): clean_reply("Please refund $18.00")}
    env.svc.agent.consent_reader = ModelConsentReader(JitterModel(ScriptedConsentClient(replies)))
    text = _text("ORD-1001", 18)
    jobs = [lambda: env.post_public("/api/buyer/messages", {"text": text, "order": "ORD-1001", "cap": env.svc.buyer_capability("ORD-1001")}),
            lambda: env.post("/api/desk/send", {"order": "ORD-1001", "text": text})]
    results = _run_together(jobs)
    assert all(getattr(r, "status", None) == 201 for r in results)
    assert env.fx.refunded_minor(env.svc.orders.get("ORD-1001").capture_id) == 1800
    assert env.fx.refunds_created == 1
