"""Sprint 2b: the policy save API itself enforces the preview binding and the compare-and-save is atomic. Before this, a request with only {"text": ...} saved, and two saves from the same base could both
append version 2."""
import threading

from approval_receipt import Ledger

from app import arena_policy as ap
from app.policy_store import PolicyStore

PREVIEW, SAVE = "/api/policy/preview", "/api/policy/save"
TEXT = "refund up to $30 per order"


def versions(env):
    return [r["version"] for r in env.svc.policies.ledger.receipts() if r["event"] == "policy_version"]


def test_a_save_without_the_preview_version_and_result_hash_is_refused(env):
    for body in ({"text": TEXT}, {"text": TEXT, "base_version": 1}, {"text": TEXT, "result_hash": "x" * 64},
                 {"text": TEXT, "base_version": None, "result_hash": None}):
        r = env.post(SAVE, body)
        assert r.status == 422 and r.json()["errors"][0]["code"] == "PREVIEW_REQUIRED", body
    assert versions(env) == [1]


def test_a_malformed_version_or_hash_is_refused_not_crashed_on(env):
    p = env.post(PREVIEW, {"text": TEXT}).json()
    for bad in ("one", [1], {"v": 1}, True):
        r = env.post(SAVE, {"text": TEXT, "base_version": bad, "result_hash": p["result_hash"]})
        assert r.status in (400, 422), (bad, r.body)
    assert versions(env) == [1]


def test_two_saves_from_the_same_preview_cannot_both_succeed(env):
    p = env.post(PREVIEW, {"text": TEXT}).json()
    body = {"text": TEXT, "base_version": p["base_version"], "result_hash": p["result_hash"]}
    first, second = env.post(SAVE, body), env.post(SAVE, body)
    assert first.status == 200 and second.status == 409 and second.json()["errors"][0]["code"] == "POLICY_CHANGED"
    assert versions(env) == [1, 2]


def test_concurrent_saves_from_two_service_instances_never_produce_a_duplicate_version(env, monkeypatch):
    led = env.svc.policies.ledger
    other = PolicyStore(Ledger(led.path, led._key, led.key_id, checkpoint_path=led.checkpoint_path), env.clock)
    store_a, store_b = env.svc.policies, other
    p = store_a.preview(TEXT)
    real = ap.compile_text
    a_has_read, b_done = threading.Event(), threading.Event()

    def slow_for_a(text, base):
        if threading.current_thread().name == "A":
            a_has_read.set()
            b_done.wait(1.0)               # A pauses between reading the version and writing the next one
        return real(text, base)
    monkeypatch.setattr(ap, "compile_text", slow_for_a)
    results = {}

    def run(name, store):
        results[name] = store.save(TEXT, name, p["base_version"], p["result_hash"])
        if name == "B":
            b_done.set()
    ta = threading.Thread(target=run, args=("A", store_a), name="A")
    tb = threading.Thread(target=run, args=("B", store_b), name="B")
    ta.start()
    a_has_read.wait(2.0)
    tb.start()
    ta.join(10)
    tb.join(10)
    assert sorted(r["ok"] for r in results.values()) == [False, True], results
    assert versions(env) == [1, 2]
    loser = next(r for r in results.values() if not r["ok"])
    assert loser["errors"][0]["code"] == "POLICY_CHANGED"


def test_saving_updates_the_enforced_mandates_before_anyone_else_can_decide(env):
    p = env.post(PREVIEW, {"text": TEXT}).json()
    r = env.post(SAVE, {"text": TEXT, "base_version": p["base_version"], "result_hash": p["result_hash"]})
    assert r.status == 200
    assert env.svc.engine.mandates["refund-agent"].version == 2 == env.svc.engine.mandates["refund-review"].version
    assert env.svc.enforced_limits()["matches_policy"] is True
