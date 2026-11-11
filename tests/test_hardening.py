"""Adversarial regression tests for the independent review (CODEX_REVIEW_2026-10-20.md).
Each test names the finding it pins down. No network: PayPal is the synthetic fixture."""
import email.message
import hashlib
import io
import json
import os
import sys
import urllib.request
import urllib.response
import uuid

import pytest

from approval_receipt import SimulatedCrash, Ledger, LedgerIntegrityError
from app.__main__ import build_sandbox_service
from app.extractor import ModelExtractor, RuleBasedExtractor
from app.fixture_transport import FixturePayPal
from app.orders import OrderBook
from app.service import Service, StartupIntegrityError
from app.transport import UrllibTransport, HttpRequest, no_redirect_opener, SANDBOX_HOST
from conftest import ROOT, KEY, MSG_A, MSG_B, build_env

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import live_sandbox_run  # noqa: E402

M15 = "ORD-1004 arrived damaged, please refund $15.00."


def second_process(env):
    """A restarted app on the same files and the same (fixture) PayPal."""
    e2 = build_env(env.dir)
    e2.svc.client.transport = env.fx
    return e2


def posts(env):
    return env.fx.refund_posts


# ---- finding 3: ambiguous or malformed answers after acceptance are UNKNOWN ----------
@pytest.mark.parametrize("flag", ["malformed_after_accept", "odd_status_after_accept",
                                  "conflict_after_accept", "redirect_after_accept",
                                  "server_error_after_accept", "timeout_after_accept"])
def test_ambiguous_answer_after_acceptance_is_unknown_and_keeps_the_money_held(env, flag):
    setattr(env.fx, flag, True)
    env.msg(M15)
    first = env.latest_req()
    assert env.item(first["id"])["stage"] == "UNKNOWN"
    assert env.svc.ledger.latest_for_intent(first["intent_id"])["outcome"] == "UNKNOWN"
    # another buyer message must NOT create a fresh refund key while this one is unresolved
    env.msg(M15)
    assert env.latest_req()["kind"] == "DUPLICATE"
    assert posts(env) == 1 and len([r for r in env.svc.ledger.receipts() if r["event"] == "intent_decided"]) == 1
    # the safe retry reuses the same key, PayPal dedupes, one refund in total
    env.retry(first['id'])
    assert env.fx.refunds_created == 1
    assert env.item(first["id"])["stage"] in ("SENT_DONE", "SENT_PENDING")


def test_a_clear_paypal_refusal_is_still_a_definite_failure(env):
    env.fx.captures["FIXCAP-1004"] = 500
    env.msg(M15)
    assert env.item(env.latest_req()["id"])["stage"] == "FAILED" and env.fx.refunds_created == 0


def test_error_status_without_a_paypal_error_body_is_unknown_not_failed(env):
    orig = env.fx.send

    def bare_422(req):
        if req.url.endswith("/refund"):
            from app.transport import HttpResponse
            return HttpResponse(422, {}, b"<html>proxy error</html>")
        return orig(req)
    env.fx.send = bare_422
    env.msg(M15)
    assert env.item(env.latest_req()["id"])["stage"] == "UNKNOWN"


# ---- finding 4: reconciliation outcomes, no dispatch after a failed lookup or retention ----
def test_retry_never_dispatches_when_the_lookup_fails(env):
    env.fx.timeout_after_accept = True
    env.msg(M15)
    req = env.latest_req()
    ref = next(iter(env.fx.refunds))
    env.svc.adapter.remember(req["intent"]["idempotency_key"], ref)    # id known, so lookup reads PayPal
    env.fx.read_unavailable = True
    env.retry(req['id'])
    assert posts(env) == 1 and env.item(req["id"])["stage"] == "UNKNOWN"
    env.fx.read_unavailable = False
    env.retry(req['id'])        # now FOUND: resolved without any new POST
    assert posts(env) == 1 and env.item(req["id"])["stage"] in ("SENT_DONE", "SENT_PENDING")


def test_retry_after_the_replay_window_requires_a_person(env):
    env.fx.timeout_after_accept = True
    env.msg(M15)
    req = env.latest_req()
    env.clock.advance(hours=7)
    env.retry(req['id'])
    assert posts(env) == 1 and env.item(req["id"])["stage"] == "UNKNOWN"


def test_retry_inside_the_window_is_not_turned_into_expired(env):
    """Core execute_by would otherwise mark a retried UNKNOWN refund EXPIRED and release the money."""
    env.fx.timeout_after_accept = True
    env.msg(M15)
    req = env.latest_req()
    env.clock.advance(minutes=40)
    env.retry(req['id'])
    assert env.fx.refunds_created == 1 and env.item(req["id"])["stage"] in ("SENT_DONE", "SENT_PENDING")


# ---- finding 5: random, persisted refund keys ----------------------------------------
def test_refund_keys_are_random_uuids_never_derived_from_a_counter(tmp_path):
    a, b = build_env(tmp_path / "a"), build_env(tmp_path / "b")
    a.msg(MSG_A)
    b.msg(MSG_A)
    ka, kb = a.latest_req()["intent"]["idempotency_key"], b.latest_req()["intent"]["idempotency_key"]
    assert ka != kb
    assert uuid.UUID(ka).version == 4 and uuid.UUID(kb).version == 4 and len(ka) <= 36
    assert ka != "rf-" + hashlib.sha256(b"REQ-0001").hexdigest()[:24]
    a.msg("ORD-1003 arrived damaged, please refund $20.00.")
    assert a.latest_req()["intent"]["idempotency_key"] not in (ka, kb)


def test_the_key_is_persisted_before_dispatch_and_survives_restart(env):
    env.fx.timeout_before_accept = True
    env.msg(M15)
    req = env.latest_req()
    key = req["intent"]["idempotency_key"]
    sent = []
    e2 = second_process(env)
    orig = env.fx.send

    def spy(r):
        if r.url.endswith("/refund"):
            sent.append(r.headers["PayPal-Request-Id"])
        return orig(r)
    env.fx.send = spy
    e2.retry(req['id'])
    assert sent == [key] and env.fx.refunds_created == 1


# ---- finding 7: ambiguous or absent amounts never become a full refund ------------------
@pytest.mark.parametrize("text", [
    "ORD-1004 arrived damaged, please refund me, I paid $5 and $10",
    "ORD-1004 arrived damaged. Paid $5.00, other thing $10.00, please refund",
    "ORD-1004 arrived damaged, please refund me",
])
def test_ambiguous_or_missing_amount_asks_the_buyer_and_moves_no_money(env, text):
    out = env.msg(text)
    req = env.latest_req()
    assert req["kind"] == "NEEDS_INFO" and req["code"] in ("AMOUNT_AMBIGUOUS", "MISSING_AMOUNT")
    assert not env.svc.ledger.receipts() and posts(env) == 0
    assert out["status"]["stage"].startswith("NEEDS_")


def test_an_explicit_full_refund_request_is_the_only_way_to_get_the_whole_total(env):
    env.msg("ORD-1002 never arrived. Please send me a full refund.")
    item = env.inbox()["items"][0]
    assert item["amount"] == "$120.00" and item["amount_note"] and item["stage"] == "PENDING_APPROVAL"


# ---- finding 8: the model needs evidence in the buyer's own words ----------------------------
def fake(**fields):
    base = {"order_ref": None, "order_quote": None, "issue_code": None, "issue_quote": None,
            "requested_amount_dollars": None, "amount_quote": None, "refund_intent_quote": None,
            "explanation": None}      # all eight keys: the reply contract is literal since Codex pass 2 (F8)
    base.update(fields)
    return lambda prompt: json.dumps(base)


def test_model_cannot_invent_a_damage_claim_from_hello():
    f = ModelExtractor(fake(order_ref="ORD-1004", order_quote="ORD-1004", issue_code="refund_damaged",
                            issue_quote="damaged", refund_intent_quote="refund")).extract("ORD-1004 hello")
    assert f.issue_code is None and "MODEL_OUTPUT_REJECTED" in f.notes and f.source == "rules"


def test_model_quote_that_does_not_say_what_it_claims_is_rejected():
    msg = "ORD-1004 hello, please refund $5.00"
    f = ModelExtractor(fake(issue_code="refund_damaged", issue_quote="hello", refund_intent_quote="please refund",
                            requested_amount_dollars=5, amount_quote="$5.00")).extract(msg)
    assert "MODEL_OUTPUT_REJECTED" in f.notes and f.issue_code is None


def test_model_quote_not_in_the_message_is_rejected():
    f = ModelExtractor(fake(issue_code="refund_damaged", issue_quote="smashed to pieces",
                            refund_intent_quote="refund")).extract("ORD-1004 is damaged, refund me")
    assert "MODEL_OUTPUT_REJECTED" in f.notes


def test_model_null_amount_is_not_a_full_refund(env_factory=None):
    f = ModelExtractor(fake(issue_code="refund_damaged", issue_quote="is damaged",
                            refund_intent_quote="refund me")).extract("ORD-1004 is damaged, refund me")
    assert f.source == "model" and f.amount_state == "NONE" and f.requested_amount_minor is None


def test_model_with_two_figures_and_no_tied_amount_is_ambiguous_not_guessed():
    msg = "ORD-1004 is damaged, I paid $5 and $10, refund me"
    f = ModelExtractor(fake(issue_code="refund_damaged", issue_quote="is damaged",
                            refund_intent_quote="refund me")).extract(msg)
    assert f.amount_state == "AMBIGUOUS"
    g = ModelExtractor(fake(issue_code="refund_damaged", issue_quote="is damaged", refund_intent_quote="refund me",
                            requested_amount_dollars=5, amount_quote="$5")).extract(msg)
    assert "MODEL_OUTPUT_REJECTED" in g.notes      # the $5 quote is not next to a refund request


def test_model_picking_one_of_several_stated_reasons_loses_the_fact():
    msg = "ORD-1004 is damaged and also the wrong size, please refund $5.00"
    f = ModelExtractor(fake(issue_code="refund_wrong_item", issue_quote="wrong size", refund_intent_quote="refund")).extract(msg)
    assert f.issue_code is None and "MODEL_CONFLICT_ISSUE" in f.notes
    assert RuleBasedExtractor().extract(msg).issue_code is None


def test_model_without_a_refund_request_cannot_open_a_refund(tmp_path):
    env = build_env(tmp_path / "d", extractor=ModelExtractor(
        fake(issue_code="refund_damaged", issue_quote="arrived damaged")))
    env.msg("ORD-1004 arrived damaged, $15.00")
    assert env.latest_req()["code"] == "MISSING_INTENT" and posts(env) == 0


# ---- finding 6: buyer capability and throttling ----------------------------------------------
def test_no_capability_no_refund(env):
    r = env.post_public("/api/buyer/messages", {"text": M15, "order": "ORD-1004"})
    assert r.status == 403 and r.json()["error"] == "PROOF_REQUIRED"
    r = env.post_public("/api/buyer/messages", {"text": M15, "order": "ORD-1004", "cap": "0" * 32})
    assert r.status == 403
    assert not env.svc.store.requests and posts(env) == 0


def test_a_capability_opens_only_its_own_order(env):
    other = env.svc.buyer_capability("ORD-1002")
    r = env.post_public("/api/buyer/messages", {"text": M15, "order": "ORD-1004", "cap": other})
    assert r.status == 403 and posts(env) == 0
    r = env.post_public("/api/buyer/messages", {"text": M15, "order": "ORD-9999", "cap": env.svc.buyer_capability("ORD-9999")})
    assert r.status == 403          # an order we do not know has no valid capability


def test_capabilities_survive_a_restart_and_differ_per_order(env):
    assert env.svc.buyer_capability("ORD-1001") != env.svc.buyer_capability("ORD-1002")
    assert second_process(env).svc.buyer_capability("ORD-1001") == env.svc.buyer_capability("ORD-1001")


def test_submissions_are_throttled_per_order(env):
    for _ in range(5):
        env.msg("hello", order="ORD-1001")
    r = env.post_public("/api/buyer/messages", {"text": "hello", "order": "ORD-1001", "cap": env.svc.buyer_capability("ORD-1001")})
    assert r.status == 429 and r.json()["error"] == "RATE_LIMITED"
    env.msg("hello", order="ORD-1002")      # another order is not affected
    env.clock.advance(hours=2)
    env.msg("hello", order="ORD-1001")      # the window moves on


# ---- finding 9: orphan states are recovered -----------------------------------------------------
def test_crash_after_request_saved_before_submit_is_recovered_with_the_same_key(env):
    def boom(intent):
        raise SimulatedCrash()
    env.svc.engine.submit = boom
    with pytest.raises(SimulatedCrash):
        env.msg(MSG_A)
    req = env.latest_req()
    key = req["intent"]["idempotency_key"]
    assert not env.svc.ledger.receipts()
    assert env.inbox()["items"][0]["stage"] == "NOT_SUBMITTED"          # views do not crash
    buyer = env.get(f"/api/buyer/requests/{req['token']}", headers={}).json()
    assert buyer["stage"] == "NOT_SUBMITTED"
    e2 = second_process(env)
    assert e2.svc.recover() == [req["intent_id"]]
    rec = e2.svc.ledger.latest_for_intent(req["intent_id"])
    assert rec["intent"]["idempotency_key"] == key and rec["outcome"] == "SUCCEEDED"
    e2.svc.recover()
    assert env.fx.refunds_created == 1 and posts(env) == 1


def test_crash_after_authorized_is_executed_once_by_recover(env):
    def boom(rec):
        raise SimulatedCrash()
    env.svc.engine._execute = boom
    with pytest.raises(SimulatedCrash):
        env.msg(MSG_A)
    req = env.latest_req()
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "AUTHORIZED"
    env.msg(MSG_A)                                          # reserved: not a second refund
    assert env.latest_req()["kind"] == "DUPLICATE"
    e2 = second_process(env)
    e2.svc.recover()
    e2.svc.recover()
    assert e2.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] == "SUCCEEDED"
    assert env.fx.refunds_created == 1 and posts(env) == 1


# ---- finding 10: startup never launders truncation -------------------------------------------------
def truncate(env, n=2):
    path = env.svc.ledger.path
    lines = open(path, encoding="utf-8").read().splitlines()
    open(path, "w", encoding="utf-8").write("\n".join(lines[:-n]) + "\n")


def test_restart_after_truncation_stops_and_preserves_the_evidence(env):
    env.msg(MSG_A)
    truncate(env)
    cp = env.svc.checkpoint_path
    before = (open(env.svc.ledger.path, "rb").read(), open(cp, "rb").read())
    with pytest.raises(StartupIntegrityError) as e:
        build_env(env.dir)
    assert e.value.reason_code == "TRUNCATED"
    assert (open(env.svc.ledger.path, "rb").read(), open(cp, "rb").read()) == before


def test_missing_checkpoint_stops_startup_and_is_not_recreated(env):
    env.msg(MSG_A)
    os.remove(env.svc.checkpoint_path)
    with pytest.raises(StartupIntegrityError) as e:
        build_env(env.dir)
    assert e.value.reason_code == "CHECKPOINT_MISSING" and not os.path.exists(env.svc.checkpoint_path)


def test_an_emptied_ledger_is_refused_at_startup(env):
    env.msg(MSG_A)
    open(env.svc.ledger.path, "w").close()
    with pytest.raises(LedgerIntegrityError):
        build_env(env.dir)


def test_main_exits_nonzero_and_changes_nothing_on_truncation(env, monkeypatch, capsys):
    from app.__main__ import main
    env.msg(MSG_A)
    truncate(env)
    monkeypatch.setenv("APPROVAL_RECEIPT_HMAC_KEY", KEY)
    before = open(env.svc.ledger.path, "rb").read()
    assert main(["--data-dir", env.dir]) == 3
    assert open(env.svc.ledger.path, "rb").read() == before and "STOPPED" in capsys.readouterr().out


def test_checkpoint_lives_outside_the_data_directory_and_must(env, tmp_path):
    assert os.path.exists(env.svc.checkpoint_path)
    assert not os.path.abspath(env.svc.checkpoint_path).startswith(os.path.abspath(env.dir) + os.sep)
    ob = OrderBook.load()
    with pytest.raises(ValueError):
        Service(str(tmp_path / "x"), FixturePayPal(), ob, key=KEY, checkpoint_dir=str(tmp_path / "x" / "cp"))


def test_a_tampered_ledger_stops_every_action(env):
    env.msg(MSG_A)
    text = open(env.svc.ledger.path, encoding="utf-8").read().replace('"amount_minor":1800', '"amount_minor":1900', 1)
    open(env.svc.ledger.path, "w", encoding="utf-8").write(text)
    n = posts(env)
    assert env.msg(MSG_A, expect=409) == {"error": "LEDGER_INTEGRITY_FAILED"}
    assert posts(env) == n


def test_there_is_no_fallback_signing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("APPROVAL_RECEIPT_HMAC_KEY", raising=False)
    with pytest.raises(RuntimeError):
        Ledger(str(tmp_path / "l.jsonl"))
    with pytest.raises(RuntimeError):
        Service(str(tmp_path / "s"), FixturePayPal(), OrderBook.load(), mode="sandbox")
    demo = Service(str(tmp_path / "d"), FixturePayPal(), OrderBook.load(), mode="fixture")   # demo mode is allowed
    assert demo.demo_key and demo.ledger.records()[0].get("key_mode") == "DEMO"


# ---- finding 11 and 12: webhooks persist, read back, and pending refunds are polled ------------------
def pending(env):
    env.fx.next_refund_status = "PENDING"
    env.msg(MSG_A)
    env.fx.next_refund_status = "COMPLETED"
    return next(iter(env.fx.refunds))


def hook(env, ref, status, eid):
    h, b = env.fx.make_webhook(ref, "x", status, eid)
    r = env.app.handle("POST", "/api/webhooks/paypal", h, b)
    return r.status, r.json()


def test_stale_failed_payload_during_a_read_outage_does_not_release_the_reservation(env):
    ref = pending(env)
    env.fx.settle(ref, "COMPLETED")
    env.fx.read_unavailable = True
    code, res = hook(env, ref, "FAILED", "WH-S1")
    assert code == 503 and env.svc.sync.status(ref) == "PENDING"
    assert env.svc.store.events["WH-S1"]["state"] == "VERIFIED"           # persisted for retry
    env.msg("ORD-1001 arrived damaged, please refund $30.00")             # $18 still held: 18 + 30 > 42
    assert env.latest_req()["code"] == "AMOUNT_EXCEEDS_PAID" and posts(env) == 1
    env.fx.read_unavailable = False
    env.svc.recover()                                                      # queued event, read back, applied
    assert env.svc.sync.status(ref) == "COMPLETED" and env.svc.store.events["WH-S1"]["state"] == "DONE"


def test_queued_event_is_delivered_again_by_paypal_after_the_outage(env):
    ref = pending(env)
    env.fx.settle(ref, "COMPLETED")
    h, b = env.fx.make_webhook(ref, "x", "COMPLETED", "WH-S2")
    env.fx.read_unavailable = True
    assert env.app.handle("POST", "/api/webhooks/paypal", h, b).status == 503
    env.fx.read_unavailable = False
    r = env.app.handle("POST", "/api/webhooks/paypal", h, b)
    assert r.status == 200 and r.json()["result"] == "APPLIED"


def test_pending_refund_is_polled_to_completion_without_any_webhook(env):
    ref = pending(env)
    env.fx.settle(ref, "COMPLETED")
    assert env.svc.poll() and env.svc.sync.status(ref) == "COMPLETED"
    assert env.inbox()["items"][0]["stage"] == "SENT_DONE"
    posted = len(env.fx.log)
    env.svc.poll()                                                         # nothing pending any more
    assert not [m for m, p in env.fx.log[posted:] if p.startswith("/v2/payments/refunds/")]


def test_polling_applies_a_failure_and_frees_the_order(env):
    ref = pending(env)
    env.fx.settle(ref, "FAILED")
    env.svc.poll()
    assert env.inbox()["items"][0]["stage"] == "PROVIDER_FAILED"
    env.msg("ORD-1001 arrived damaged, please refund $30.00")
    assert env.latest_req()["kind"] == "SUBMITTED"


def test_poll_endpoint_needs_a_session_and_csrf(env):
    assert env.app.handle("POST", "/api/poll", {}, b"{}").status == 401
    assert env.app.handle("POST", "/api/poll", {"cookie": env.headers["cookie"]}, b"{}").status == 403
    assert env.post("/api/poll").status == 200


# ---- finding 13: accounting comes from signed receipts, not the state file ------------------------------
def test_held_amount_ignores_edits_to_the_unsigned_state_file(env):
    pending(env)                                              # $18 held and PENDING at PayPal
    env.svc.store.requests.clear()                            # attacker deletes the request mapping
    env.svc.store.data["refund_status"] = {"anything": "FAILED"}
    env.svc.store.refund_index.clear()
    env.msg("ORD-1001 arrived damaged, please refund $30.00")
    assert env.latest_req()["code"] == "AMOUNT_EXCEEDS_PAID" and posts(env) == 1


def test_request_record_that_disagrees_with_its_signed_receipt_is_refused(env):
    env.msg(MSG_B)
    rid = env.latest_req()["id"]
    env.svc.store.requests[rid]["intent"]["amount_minor"] = 100
    assert env.item(rid)["stage"] == "STATE_MISMATCH" and env.item(rid)["actions"] == []
    nonce = env.svc.ledger.latest_for_intent(env.latest_req()["intent_id"])
    r = env.act(rid, "approve", {"nonce": nonce["approval_nonce"], "digest": nonce["intent_digest"]})
    assert r.status == 409 and r.json()["error"] == "STATE_MISMATCH" and posts(env) == 0


# ---- finding 14: redirects ----------------------------------------------------------------------------
class RedirectingHandler(urllib.request.HTTPSHandler):
    """Synthetic: answers every request with a 302 to another host. Records what it was asked."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def https_open(self, req):
        self.seen.append((req.full_url, req.get_header("Authorization")))
        headers = email.message.Message()
        headers["Location"] = "https://evil.example/steal"
        resp = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        resp.msg = "Found"
        return resp


def test_a_redirect_is_never_followed_so_credentials_are_never_forwarded():
    h = RedirectingHandler()
    t = UrllibTransport(opener=no_redirect_opener(h))
    r = t.send(HttpRequest("POST", f"https://{SANDBOX_HOST}/v2/payments/captures/C/refund",
                           {"Authorization": "Bearer secret-token"}, b"{}"))
    assert r.status == 302
    assert [u for u, _ in h.seen] == [f"https://{SANDBOX_HOST}/v2/payments/captures/C/refund"]   # evil.example never asked


def test_the_default_transport_has_redirects_disabled():
    from app.transport import _NoRedirect
    t = UrllibTransport()
    assert any(isinstance(x, _NoRedirect) for x in t._opener.handlers)
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


# ---- finding 15: the live script cannot pass on a bad run ------------------------------------------------
def good_env():
    return {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": "secret", "PAYPAL_ENV": "sandbox"}


def run_live(fx, amount="1.00"):
    return live_sandbox_run.main(["--capture-id", "CAP-LIVE", "--amount", amount, "--yes"], env=good_env(), transport=fx)


def test_live_script_passes_only_when_everything_passes(capsys):
    fx = FixturePayPal({"CAP-LIVE": 5_000})
    assert run_live(fx) == 0 and "ALL CHECKS PASSED" in capsys.readouterr().out


def test_live_script_fails_on_an_unknown_outcome(capsys):
    fx = FixturePayPal({"CAP-LIVE": 5_000})
    fx.timeout_after_accept = True
    assert run_live(fx) == 1 and "FAIL outcome is SUCCEEDED" in capsys.readouterr().out


def test_live_script_fails_on_a_refused_refund(capsys):
    class Refuse(FixturePayPal):
        def send(self, req):
            if req.url.endswith("/refund"):
                return super()._refund("CAP-LIVE", HttpRequest("POST", req.url, req.headers,
                                                                json.dumps({"amount": {"value": "999.00"}}).encode()), {})
            return super().send(req)
    assert run_live(Refuse({"CAP-LIVE": 5_000})) == 1


def test_live_script_fails_when_the_refund_is_bigger_than_the_capture(capsys):
    assert run_live(FixturePayPal({"CAP-LIVE": 50})) == 1


def test_live_script_fails_on_a_failed_provider_status(capsys):
    fx = FixturePayPal({"CAP-LIVE": 5_000})
    fx.next_refund_status = "FAILED"
    assert run_live(fx) == 1


def test_live_script_fails_when_the_replay_returns_a_different_refund_id(capsys):
    class Drift(FixturePayPal):
        def send(self, req):
            r = super().send(req)
            if req.url.endswith("/refund") and self.refund_posts == 2:
                from app.transport import HttpResponse
                d = json.loads(r.body)
                d["id"] = "SOME-OTHER-REFUND"
                return HttpResponse(201, r.headers, json.dumps(d).encode())
            return r
    assert run_live(Drift({"CAP-LIVE": 5_000})) == 1
    assert "FAIL replay" in capsys.readouterr().out


def test_live_script_uses_fresh_storage_and_random_keys_each_run():
    keys = []
    for _ in range(2):
        fx = FixturePayPal({"CAP-LIVE": 5_000})
        assert run_live(fx) == 0
        keys.append(next(iter(fx.by_request_id)))
    assert keys[0] != keys[1]


def test_sandbox_builder_has_no_demo_routes_and_needs_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("APPROVAL_RECEIPT_HMAC_KEY", raising=False)
    env = {**good_env(), "APP_PASSCODE": "x"}
    ob = OrderBook.load()
    with pytest.raises(RuntimeError):
        build_sandbox_service(str(tmp_path / "a"), ob, RuleBasedExtractor(), env=env, transport=FixturePayPal())
    from app.server import App
    svc = build_sandbox_service(str(tmp_path / "b"), ob, RuleBasedExtractor(), env={**env, "APPROVAL_RECEIPT_HMAC_KEY": KEY},
                                transport=FixturePayPal())
    a = App(svc)
    assert svc.mode == "sandbox" and a.demo is None
    assert a.handle("GET", "/api/demo/links").status == 401 or a.handle("GET", "/api/demo/links").status == 404


# ---- submission readiness --------------------------------------------------------------------------------
def test_the_default_demo_is_labelled_simulation_on_screen():
    doc = json.load(open(os.path.join(ROOT, "copy", "strings.draft.json"), encoding="utf-8"))["strings"]
    assert doc["banner.fixture"].startswith("SIMULATION") and "banner.draft" not in doc
    assert "SIMULATION" in doc["demo.intro"]


def test_license_is_the_mit_license_walter_chose():
    text = open(os.path.join(ROOT, "LICENSE"), encoding="utf-8").read()
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 Walter Guevara, Xin Liang Chia and Dhanush Kankanala" in text


def test_demo_links_are_the_only_way_a_judge_gets_a_buyer_capability(env):
    links = env.get("/api/demo/links", headers={}).json()["links"]
    assert len(links) == 11 and all("c=" in l["url"] and l["url"].startswith("/buyer.html?o=ORD-") for l in links)
    assert env.get("/api/buyer/requests/REQ-0001", headers={}).status == 404


def test_fresh_flag_deletes_receipts_and_checkpoint_together_and_nothing_else_does(tmp_path, monkeypatch):
    import argparse
    from app.__main__ import build
    monkeypatch.setenv("APPROVAL_RECEIPT_HMAC_KEY", KEY)
    d = str(tmp_path / "data")
    ns = dict(orders=None, mode="fixture", extractor="rules", checkpoint_dir=None, data_dir=d, fresh=False)
    a, _ = build(argparse.Namespace(**ns))
    a.svc.message(MSG_A, "ORD-1001", a.svc.buyer_capability("ORD-1001"))
    cp = a.svc.checkpoint_path
    assert os.path.exists(cp)
    b, _ = build(argparse.Namespace(**ns))                   # restart without --fresh keeps everything
    assert len(b.svc.ledger.receipts()) == len(a.svc.ledger.receipts()) and os.path.exists(cp)
    c, _ = build(argparse.Namespace(**{**ns, "fresh": True}))
    assert c.svc.ledger.receipts() == [] and len(c.svc.ledger.records()) == 1      # new genesis, new checkpoint
