"""Session 2 (2026-11-01, afternoon), part 2: the model calls leave the app-wide lock. Written first (red).

The extractor, the consent reader and the arena's model reader used to run while the app-wide lock was held, so one slow
provider call stopped every other request. They now run BEFORE the lock. What must not
change, and is pinned here: the capability is checked first, the throttle slot is taken before the model is asked, a decision is
still made one at a time under the lock, the reading still rides inside the signed proposal, and each request keeps its own
exchange. Fixture only: no network, no key; the "slow provider" is a stand-in that waits for an event."""
import json
import threading
import time

from app.consent_doubles import ScriptedConsentClient, clean_reply
from app.consent_reader import ModelConsentReader

MSG_A = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."
MSG_C = "Order ORD-1002 arrived damaged, the handle is cracked. Please refund $20.00."
CLEAN_A = clean_reply("Please refund $18.00")
CLEAN_C = clean_reply("Please refund $20.00")


class BlockingModel:
    """A stand-in for a slow provider: every call announces itself, then waits until released (never longer than 10 s)."""
    supports_system = True
    provider, model = "scripted", "blocking-v1"

    def __init__(self, inner):
        self.inner, self.calls = inner, 0
        self.started, self.release = threading.Event(), threading.Event()
        self._n = threading.Semaphore(0)
        self.in_flight, self.max_in_flight, self._g = 0, 0, threading.Lock()

    def __call__(self, user, system=None):
        with self._g:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.started.set()
        self._n.release()
        try:
            assert self.release.wait(10), "test never released the model"
            return self.inner(user, system=system)
        finally:
            with self._g:
                self.in_flight -= 1

    def wait_calls(self, n, timeout=5.0):
        end = time.time() + timeout
        for _ in range(n):
            if not self._n.acquire(timeout=max(0.0, end - time.time())):
                return False
        return True


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:               # noqa: BLE001
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _public_post(env, text, order, cap=None):
    return env.post_public("/api/buyer/messages", {"text": text, "order": order, "cap": cap or env.svc.buyer_capability(order)})


def _within(seconds, fn):
    t, out = _in_thread(fn)
    t.join(seconds)
    return (not t.is_alive()), out


def test_another_endpoint_answers_while_a_buyer_message_waits_for_the_model(env):
    model = BlockingModel(ScriptedConsentClient({MSG_A: CLEAN_A}))
    env.svc.agent.consent_reader = ModelConsentReader(model)
    t, out = _in_thread(lambda: _public_post(env, MSG_A, "ORD-1001"))
    try:
        assert model.started.wait(5)
        done, o = _within(2.0, lambda: env.get("/api/mode"))
        assert done, "the app-wide lock is held while the model is called"
        assert o["value"].status == 200
        done, o = _within(2.0, lambda: env.get("/api/inbox"))
        assert done and o["value"].status == 200
    finally:
        model.release.set()
    t.join(10)
    assert out["value"].status == 201
    assert env.fx.refund_posts == 1
    assert env.latest_req()["proposal"]["consent"]["verdict"] == "REQUEST"      # the reading still rides in the signed proposal


def test_the_extractor_call_is_outside_the_lock_too(env):
    from app.extractor import ModelExtractor
    from app.scripted import ScriptedCompleter, model_reply
    reply_a = model_reply("ORD-1001", "Order ORD-1001", "refund_damaged", "arrived damaged", 18.0, "$18.00", "Please refund $18.00",
                          "Damaged mug.")
    model = BlockingModel(ScriptedCompleter({MSG_A: reply_a}))
    env.svc.agent.extractor = ModelExtractor(model)
    t, out = _in_thread(lambda: _public_post(env, MSG_A, "ORD-1001"))
    try:
        assert model.started.wait(5)
        done, o = _within(2.0, lambda: env.get("/api/mode"))
        assert done, "the app-wide lock is held while the extractor is called"
    finally:
        model.release.set()
    t.join(10)
    assert out["value"].status == 201 and env.fx.refund_posts == 1


def test_the_arena_model_reader_is_outside_the_lock_too(env):
    from app.arena_reader import ModelReader
    from app.extractor import ModelExtractor
    from app.scripted import ScriptedCompleter
    model = BlockingModel(ScriptedCompleter({}))
    env.app._arena_reader = ModelReader(ModelExtractor(model))
    t, out = _in_thread(lambda: env.post_public("/api/arena/attempt", {"text": MSG_A, "order": "ORD-1001"}))
    try:
        assert model.started.wait(5)
        done, o = _within(2.0, lambda: env.get("/api/mode"))
        assert done, "the app-wide lock is held while the arena reader calls the model"
    finally:
        model.release.set()
    t.join(10)
    assert out["value"].status == 201


def test_a_desk_send_is_outside_the_lock_too(env):
    model = BlockingModel(ScriptedConsentClient({MSG_A: CLEAN_A}))
    env.svc.agent.consent_reader = ModelConsentReader(model)
    t, out = _in_thread(lambda: env.post("/api/desk/send", {"order": "ORD-1001", "text": MSG_A}))
    try:
        assert model.started.wait(5)
        done, o = _within(2.0, lambda: env.get("/api/mode"))
        assert done, "the app-wide lock is held while a desk send waits for the model"
    finally:
        model.release.set()
    t.join(10)
    assert out["value"].status == 201 and env.fx.refund_posts == 1


def test_two_slow_reads_overlap_but_each_keeps_its_own_exchange(env):
    model = BlockingModel(ScriptedConsentClient({MSG_A: CLEAN_A, MSG_C: CLEAN_C}))
    env.svc.agent.consent_reader = ModelConsentReader(model)
    t1, o1 = _in_thread(lambda: _public_post(env, MSG_A, "ORD-1001"))
    t2, o2 = _in_thread(lambda: _public_post(env, MSG_C, "ORD-1002"))
    try:
        assert model.wait_calls(2), "the second read could not start while the first waited"
        assert model.max_in_flight == 2
    finally:
        model.release.set()
    t1.join(10), t2.join(10)
    assert o1["value"].status == 201 and o2["value"].status == 201
    assert env.fx.refund_posts == 2
    reqs = {r["order_ref"]: r for r in env.svc.store.requests.values()}
    # each request keeps ITS OWN exchange (the reader's "last" must not be shared between threads)
    assert json.loads(reqs["ORD-1001"]["consent_exchange"]["raw_reply"])["request_quote"] == "Please refund $18.00"
    assert json.loads(reqs["ORD-1002"]["consent_exchange"]["raw_reply"])["request_quote"] == "Please refund $20.00"


def test_two_slow_reads_for_one_order_still_refund_once(env):
    model = BlockingModel(ScriptedConsentClient({MSG_A: CLEAN_A}, default=CLEAN_A))
    env.svc.agent.consent_reader = ModelConsentReader(model)
    t1, o1 = _in_thread(lambda: _public_post(env, MSG_A, "ORD-1001"))
    t2, o2 = _in_thread(lambda: _public_post(env, MSG_A + " ok", "ORD-1001"))
    try:
        assert model.wait_calls(2)
    finally:
        model.release.set()
    t1.join(10), t2.join(10)
    assert env.fx.refund_posts == 1
    assert len(env.svc.store.requests) == 2


def test_an_unproven_link_never_reaches_the_model(env):
    model = BlockingModel(ScriptedConsentClient({}, default=CLEAN_A))
    model.release.set()
    env.svc.agent.consent_reader = ModelConsentReader(model)
    r = _public_post(env, MSG_A, "ORD-1001", cap="x" * 20)
    assert r.status == 403 and model.calls == 0


def test_the_throttle_slot_is_taken_before_the_model_is_asked(env):
    model = BlockingModel(ScriptedConsentClient({}, default=CLEAN_A))
    model.release.set()
    env.svc.agent.consent_reader = ModelConsentReader(model)
    codes = [_public_post(env, f"Order ORD-1005 arrived damaged, the handle is cracked. Please refund $1{n}.00.", "ORD-1005").status
             for n in range(7)]
    assert codes.count(429) == 2 and model.calls <= 5, (codes, model.calls)


def test_empty_and_oversized_messages_never_reach_the_model(env):
    model = BlockingModel(ScriptedConsentClient({}, default=CLEAN_A))
    model.release.set()
    env.svc.agent.consent_reader = ModelConsentReader(model)
    assert _public_post(env, "   ", "ORD-1001").status == 400
    assert _public_post(env, "x" * 2001, "ORD-1001").status == 400
    assert model.calls == 0


def test_the_decision_is_the_same_with_the_model_read_inside_or_outside(env, tmp_path):
    """Same message, same reader reply: the service called directly (read inline) and the HTTP path (read first) agree."""
    from conftest import build_env
    env.svc.agent.consent_reader = ModelConsentReader(ScriptedConsentClient({MSG_A: CLEAN_A}))
    via_http = _public_post(env, MSG_A, "ORD-1001")
    other = build_env(tmp_path / "other")
    other.svc.agent.consent_reader = ModelConsentReader(ScriptedConsentClient({MSG_A: CLEAN_A}))
    inline = other.svc.message(MSG_A, "ORD-1001", other.svc.buyer_capability("ORD-1001"))
    a = env.latest_req()
    assert via_http.status == 201 and env.fx.refund_posts == other.fx.refund_posts == 1
    assert a["proposal"]["consent"] == inline["proposal"]["consent"] and a.get("code") == inline.get("code")
