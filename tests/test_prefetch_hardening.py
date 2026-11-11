"""Session 3 (2026-11-01): fixes for the independent Codex review of the prefetch design (7 findings, C1 to C7). Written first (red).

C1 a response cannot run on for ever or fill memory (a reply byte limit; the deadline and the cap are in test_model_deadline_*).
C2 the "someone else's order" rule has wording bypasses (a name, "had ordered", "belongs to Alice", a grandmother, mixed language).
C3 an unexpected failure of the model stage must cost ONE throttle slot and must not call the model again under the app-wide lock.
C4 desk scenarios still called the configured models under the lock.
C5 a consent call made before the lock keeps its exchange on the request even when the locked decision refused early.
C6 a Prefetch is single use and bound to the readers actually in force.
C7 a reader never reports the exchange of an earlier call on the same thread.
Fixture only: no network, no key."""
import threading

import pytest

from app import consent
from app.consent_doubles import ScriptedConsentClient, clean_reply
from app.consent_reader import ModelConsentReader
from app.extractor import ModelExtractor, RuleBasedExtractor

BASE = "The mug arrived cracked. Please refund $18.00. "
MSG_A = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."
CLEAN_A = clean_reply("Please refund $18.00")


def _slots(env):
    return sum(len(v) for v in env.svc.agent._hits.values())


# ---- C1 -------------------------------------------------------------------------------------------------------------------
def test_c1_a_reply_larger_than_the_limit_is_rejected_not_read_to_the_end(monkeypatch):
    import app.model_client as mc

    class Big:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1):
            return b"x" * (n if n and n > 0 else 50_000_000)

    class Opener:
        def open(self, req, timeout=None): return Big()
    monkeypatch.setattr(mc, "no_redirect_opener", lambda *a: Opener())
    monkeypatch.setenv("APP_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODEL_ID", "claude-opus-5-5")
    with pytest.raises(mc.ModelReplyRejected, match="REPLY_TOO_LARGE"):
        mc.make_completer()("U", system="S")
    assert 10_000 <= mc.MAX_REPLY_BYTES <= 4_000_000


# ---- C2 -------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("tail", [
    "This is my sister Jane's order.",
    "The package my sister had ordered never came.",
    "The order belongs to Alice.",
    "The shipment was received by my grandmother.",
    "This order belongs to mi hermana.",
    "My sister has just bought the mug.",
    "It was ordered by Maria.",
    "My sister Jane ordered it.",
    "The package my aunt Rosa already paid for never came.",
    "Her order was lost.",
    "The parcel was picked up by his colleague.",
])
def test_c2_more_wordings_of_someone_elses_order_are_not_passed_by_the_rules(tail):
    a = consent.assess(BASE + tail)
    assert a["tier"] != "PASS" and a["allowed"] is False, (tail, a)


@pytest.mark.parametrize("tail", [
    "I placed the order by phone.",
    "My husband and I ordered two mugs.",
    "It was delivered to my door.",
    "The order was placed by mistake.",
    "The seller's package arrived late.",
    "The courier's delivery was late.",
    "My sister also bought her one.",
    "I bought this mug for my sister. It arrived late.",
    "The package was received by the courier.",
])
def test_c2_ordinary_wordings_keep_their_tier(tail):
    assert consent.assess(BASE + tail)["tier"] == "PASS", tail


def test_c2_rules_alone_send_nothing_for_these(env):
    for n, tail in enumerate(["This is my sister Jane's order.", "The package my sister had ordered never came."]):
        env.msg(BASE + tail, ["ORD-1001", "ORD-1002"][n])
    assert env.fx.refund_posts == 0


# ---- C3 -------------------------------------------------------------------------------------------------------------------
class FailsOnce(RuleBasedExtractor):
    def __init__(self, env):
        super().__init__()
        self.env, self.calls, self.owned = env, 0, []

    def extract(self, message):
        self.calls += 1
        self.owned.append(self.env.app._lock._is_owned())
        if self.calls == 1:
            raise RuntimeError("provider blew up")
        return super().extract(message)


def test_c3_an_unexpected_extractor_failure_costs_one_slot_and_one_call_and_never_runs_under_the_lock(env):
    ex = FailsOnce(env)
    env.svc.agent.extractor = ex
    with pytest.raises(RuntimeError):
        env.post_public("/api/buyer/messages", {"text": MSG_A, "order": "ORD-1001", "cap": env.svc.buyer_capability("ORD-1001")})
    assert ex.calls == 1 and ex.owned == [False], (ex.calls, ex.owned)
    assert _slots(env) == 1


def test_c3_at_the_edge_of_the_throttle_the_failure_does_not_turn_into_a_429(env):
    for n in range(4):
        env.msg(f"Order ORD-1005 arrived damaged, the handle is cracked. Please refund $1{n}.00.", "ORD-1005")
    ex = FailsOnce(env)
    env.svc.agent.extractor = ex
    with pytest.raises(RuntimeError):
        env.post_public("/api/buyer/messages", {"text": MSG_A.replace("1001", "1005"), "order": "ORD-1005",
                                                "cap": env.svc.buyer_capability("ORD-1005")})
    assert ex.calls == 1 and _slots(env) == 5


def test_c3_an_unexpected_arena_reader_failure_costs_one_slot_and_one_call_and_never_runs_under_the_lock(env):
    from app.arena_reader import RuleReader

    class FailsOnceReader(RuleReader):
        calls, owned = 0, []

        def read(self, text, order):
            FailsOnceReader.calls += 1
            FailsOnceReader.owned.append(env.app._lock._is_owned())
            if FailsOnceReader.calls == 1:
                raise RuntimeError("reader blew up")
            return super().read(text, order)
    env.app._arena_reader = FailsOnceReader()
    with pytest.raises(RuntimeError):
        env.post_public("/api/arena/attempt", {"text": MSG_A, "order": "ORD-1001"})
    assert FailsOnceReader.calls == 1 and FailsOnceReader.owned == [False]
    assert len(env.app.arena()._hits) == 1


# ---- C4 -------------------------------------------------------------------------------------------------------------------
class Blocking:
    supports_system = True
    provider, model = "scripted", "blocking-v1"

    def __init__(self, inner):
        self.inner, self.calls, self.started, self.release = inner, 0, threading.Event(), threading.Event()

    def __call__(self, user, system=None):
        self.calls += 1
        self.started.set()
        assert self.release.wait(10), "test never released the model"
        return self.inner(user, system=system)


def _responsive_while_blocked(env, model, start):
    out = {}

    def run():
        out["r"] = start()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        assert model.started.wait(5), "the model was never called"
        done = {}
        t2 = threading.Thread(target=lambda: done.setdefault("r", env.get("/api/mode")), daemon=True)
        t2.start()
        t2.join(2.0)
        assert not t2.is_alive(), "the app-wide lock is held while the model is called"
    finally:
        model.release.set()
    t.join(10)
    return out["r"]


def test_c4_a_desk_scenario_read_by_the_configured_extractor_is_outside_the_lock(env):
    from app.scripted import ScriptedCompleter
    model = Blocking(ScriptedCompleter({}))
    env.svc.agent.extractor = ModelExtractor(model)
    r = _responsive_while_blocked(env, model, lambda: env.post("/api/desk/send", {"scenario": "hostile_text"}))
    assert r.status == 201


def test_c4_a_desk_scenario_read_by_the_configured_consent_reader_is_outside_the_lock(env):
    model = Blocking(ScriptedConsentClient({}, default=clean_reply("Please refund $20.00")))
    env.svc.agent.consent_reader = ModelConsentReader(model)
    r = _responsive_while_blocked(env, model, lambda: env.post("/api/desk/send", {"scenario": "compromised_quote"}))
    assert r.status == 201


def test_c4_a_scenario_send_decides_exactly_as_before(env):
    r = env.post("/api/desk/send", {"scenario": "legit_small"})
    assert r.status == 201 and r.json()["scenario"] == "legit_small"


# ---- C5 -------------------------------------------------------------------------------------------------------------------
def test_c5_a_consent_call_made_before_the_lock_keeps_its_exchange_even_if_the_decision_refused_early(env):
    model = Blocking(ScriptedConsentClient({}, default=CLEAN_A))
    model.release.set()
    env.svc.agent.consent_reader = ModelConsentReader(model)
    # the link proves ORD-1001 but the text names ORD-1002: refused as a mismatch
    env.post_public("/api/buyer/messages", {"text": MSG_A.replace("ORD-1001", "ORD-1002"), "order": "ORD-1001",
                                            "cap": env.svc.buyer_capability("ORD-1001")})
    req = env.latest_req()
    if model.calls:
        assert req.get("consent_exchange"), "a model call was made and paid for, but its exchange was not kept"


# ---- C6 -------------------------------------------------------------------------------------------------------------------
def test_c6_a_prefetch_is_used_once(env):
    model = Blocking(ScriptedConsentClient({}, default=CLEAN_A))
    model.release.set()
    env.svc.agent.consent_reader = ModelConsentReader(model)
    cap = env.svc.buyer_capability("ORD-1001")
    pre = env.svc.prefetch(MSG_A, "ORD-1001", cap)
    assert _slots(env) == 1 and model.calls == 1
    env.svc.message(MSG_A, "ORD-1001", cap, prefetched=pre)
    env.svc.message(MSG_A, "ORD-1001", cap, prefetched=pre)         # replay: no free slot, no free reading
    assert _slots(env) == 2 and model.calls == 2


def test_c6_a_prefetch_is_not_used_after_the_consent_reader_in_force_changed(env):
    a = Blocking(ScriptedConsentClient({}, default=CLEAN_A))
    b = Blocking(ScriptedConsentClient({}, default=CLEAN_A))
    a.release.set(), b.release.set()
    env.svc.agent.consent_reader = ModelConsentReader(a)
    cap = env.svc.buyer_capability("ORD-1001")
    pre = env.svc.prefetch(MSG_A, "ORD-1001", cap)
    env.svc.agent.consent_reader = ModelConsentReader(b)
    env.svc.message(MSG_A, "ORD-1001", cap, prefetched=pre)
    assert b.calls == 1


# ---- C7 -------------------------------------------------------------------------------------------------------------------
def test_c7_a_failed_consent_call_never_reports_the_earlier_calls_exchange():
    cr = ModelConsentReader(ScriptedConsentClient({}, default=CLEAN_A))
    assert cr.read("Please refund $18.00") is not None and cr.last["verdict"] == "ACCEPTED"
    cr.user_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("could not prepare"))
    assert cr.read("Please refund $18.00") is None
    assert "raw_reply" not in cr.last and cr.last["verdict"].startswith("REJECTED:")


def test_c7_a_failed_extraction_never_reports_the_earlier_calls_reply():
    from app.scripted import ScriptedCompleter, model_reply
    reply = model_reply("ORD-1001", "Order ORD-1001", "refund_damaged", "arrived damaged", 18.0, "$18.00", "Please refund $18.00", "Damaged.")
    ex = ModelExtractor(ScriptedCompleter({MSG_A: reply}))
    assert ex.extract(MSG_A).source != "rules" and ex.last["verdict"] == "ACCEPTED"
    ex.user_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("could not prepare"))
    f = ex.extract(MSG_A)
    assert f.source == "rules" and "raw_reply" not in ex.last and ex.last["verdict"].startswith("REJECTED:")


# ---- public export: no private folder path ----------------------------------------------------------------------------------
PRIVATE_MARKERS = ("Outreach" + "/docs", "Outreach" + chr(92) + "docs", "(Outreach" + " repo)")


def test_no_public_file_names_the_private_review_folder():
    """A public reader cannot open the maintainer's private notes folder. Internal files (left out of the export) may name it; the rest may not.
    (Written after the fix, as a guard: the session-2 report had listed CHANGES.md, a test docstring and two eval files.)"""
    import os
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "scripts"))
    from clean_export_check import is_internal
    run = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
    names = [n for n in run.stdout.decode("utf-8").split(chr(0)) if n] if run.returncode == 0 else []
    if not names:
        pytest.skip("not a git checkout (the clean export has no .git)")
    bad = []
    for f in names:
        if is_internal(f) or f.startswith("vendor/") or f.endswith((".png", ".jpg", ".ico", ".woff", ".woff2", ".gif")):
            continue
        if f == os.path.relpath(__file__, root).replace(os.sep, "/"):
            continue
        try:
            text = open(os.path.join(root, f), encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        if any(p in text for p in PRIVATE_MARKERS):
            bad.append(f)
    assert bad == [], bad
