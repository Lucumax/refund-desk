"""Session 3 (2026-11-01): a wall-clock deadline on every model call, outside the vendored core. Written first (red).

The socket timeout (30 s) bounds each wait on the network, not the whole call: a provider that sends one byte every 29 s keeps a
call alive for ever. Now that model calls run outside the app-wide lock, such a call holds a thread, not the shop; but threads must
not pile up either. So: one wall-clock deadline per call, and a cap on calls in flight (a hung call keeps its place until it ends,
so a stuck provider fills the cap and further calls are refused at once instead of starting more threads). Both end as the
exceptions the readers already turn into "the rules answer alone" (extractor) or "the rules read consent alone" (consent reader).
Fixture only: no network, no key."""
import threading
import time

import pytest

from app import model_client as mc
from app.model_deadline import ModelBusy, ModelTimeout, with_deadline


def _hang(release, seen=None):
    def fn(user, system=None):
        if seen is not None:
            seen.append((user, system))
        release.wait(10)
        return "late"
    fn.supports_system, fn.provider, fn.model = True, "scripted", "hang-v1"
    return fn


def test_a_call_that_finishes_in_time_is_passed_through_unchanged():
    def fn(user, system=None):
        return f"{system}|{user}"
    fn.supports_system, fn.provider, fn.model = True, "p", "m"
    g = with_deadline(fn, seconds=2.0, max_in_flight=2)
    assert g("U", system="S") == "S|U"
    assert (g.supports_system, g.provider, g.model) == (True, "p", "m")


def test_a_completer_without_system_support_is_called_with_the_user_text_only():
    def fn(user):
        return user.upper()
    g = with_deadline(fn, seconds=2.0, max_in_flight=2)
    assert g("abc") == "ABC"
    assert not getattr(g, "supports_system", False)


def test_an_exception_of_the_call_is_raised_to_the_caller_as_it_was():
    def fn(user, system=None):
        raise ValueError("provider said no")
    g = with_deadline(fn, seconds=2.0, max_in_flight=2)
    with pytest.raises(ValueError, match="provider said no"):
        g("U", system="S")


def test_a_call_past_the_deadline_is_abandoned_at_the_deadline():
    release = threading.Event()
    g = with_deadline(_hang(release), seconds=0.2, max_in_flight=2)
    t0 = time.time()
    try:
        with pytest.raises(ModelTimeout):
            g("U", system="S")
        assert time.time() - t0 < 2.0
    finally:
        release.set()


def test_a_hung_call_keeps_its_place_so_threads_cannot_pile_up():
    release = threading.Event()
    g = with_deadline(_hang(release), seconds=0.1, max_in_flight=2)
    try:
        for _ in range(2):
            with pytest.raises(ModelTimeout):
                g("U", system="S")
        t0 = time.time()
        with pytest.raises(ModelBusy):                 # the cap is full of hung calls: refused at once, no new thread
            g("U", system="S")
        assert time.time() - t0 < 0.05
    finally:
        release.set()


def test_places_are_freed_when_calls_end():
    release = threading.Event()
    g = with_deadline(_hang(release), seconds=0.1, max_in_flight=1)
    with pytest.raises(ModelTimeout):
        g("U", system="S")
    release.set()
    end = time.time() + 5
    while time.time() < end:
        try:
            if g("U", system="S") == "late":
                return
        except (ModelBusy, ModelTimeout):
            time.sleep(0.05)
    pytest.fail("the place was never freed")


def test_both_errors_are_ordinary_exceptions_the_readers_already_catch():
    assert issubclass(ModelTimeout, Exception) and issubclass(ModelBusy, Exception)


def test_the_extractor_answers_by_rules_when_the_model_times_out():
    from app.extractor import ModelExtractor
    release = threading.Event()
    ex = ModelExtractor(with_deadline(_hang(release), seconds=0.1, max_in_flight=2))
    try:
        f = ex.extract("Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00.")
    finally:
        release.set()
    assert f.source == "rules" and "MODEL_OUTPUT_REJECTED" in f.notes and "no answer within" in ex.last["verdict"]


def test_the_consent_reader_gives_no_reading_when_the_model_times_out():
    from app.consent_reader import ModelConsentReader
    release = threading.Event()
    cr = ModelConsentReader(with_deadline(_hang(release), seconds=0.1, max_in_flight=2))
    try:
        assert cr.read("Please refund $18.00") is None
    finally:
        release.set()
    assert "ModelTimeout" in cr.last["verdict"]


def test_a_buyer_message_whose_model_hangs_is_answered_after_the_deadline(env):
    from app.consent_reader import ModelConsentReader
    release = threading.Event()
    env.svc.agent.consent_reader = ModelConsentReader(with_deadline(_hang(release), seconds=0.2, max_in_flight=2))
    text = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."
    t0 = time.time()
    try:
        r = env.post_public("/api/buyer/messages", {"text": text, "order": "ORD-1001", "cap": env.svc.buyer_capability("ORD-1001")})
    finally:
        release.set()
    assert r.status == 201 and time.time() - t0 < 3.0
    assert "ModelTimeout" in env.latest_req()["consent_exchange"]["verdict"]


def test_make_completer_returns_a_completer_with_a_deadline(monkeypatch):
    monkeypatch.setenv("APP_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODEL_ID", "claude-opus-5-5")
    release = threading.Event()

    class SlowOpener:
        def open(self, req, timeout=None):
            release.wait(10)
            raise RuntimeError("late")
    monkeypatch.setattr(mc, "no_redirect_opener", lambda *a: SlowOpener())
    monkeypatch.setenv("APP_MODEL_DEADLINE_SECONDS", "0.2")
    c = mc.make_completer()
    assert c.supports_system and c.provider == "anthropic" and c.model == "claude-opus-5-5"
    t0 = time.time()
    try:
        with pytest.raises(ModelTimeout):
            c("U", system="S")
    finally:
        release.set()
    assert time.time() - t0 < 3.0


def test_the_deadline_has_a_default_and_a_bad_value_is_refused(monkeypatch):
    monkeypatch.setenv("APP_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODEL_ID", "claude-opus-5-5")
    monkeypatch.delenv("APP_MODEL_DEADLINE_SECONDS", raising=False)
    assert 5 <= mc.model_deadline_seconds() <= 120
    monkeypatch.setenv("APP_MODEL_DEADLINE_SECONDS", "0")
    with pytest.raises(RuntimeError):
        mc.make_completer()
    monkeypatch.setenv("APP_MODEL_DEADLINE_SECONDS", "soon")
    with pytest.raises(RuntimeError):
        mc.make_completer()
