"""LIVE: the PayPal integration functions against YOUR OWN PayPal sandbox.

Skipped unless TOLLGATE_LIVE_SANDBOX=1, so the normal suite and CI stay offline. When enabled it makes real
HTTPS calls to api-m.sandbox.paypal.com only (the host-locked transport) and refunds $1.00 of sandbox test
money per run (TOLLGATE_SANDBOX_AMOUNT changes it, at most $5.00). Read this file before you run it.

Needs PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_ENV=sandbox and TOLLGATE_SANDBOX_CAPTURE_ID: a capture
from scripts/seed_sandbox_capture.py with at least the refund amount left.
Optional: TOLLGATE_LIVE_EVIDENCE=path writes a sanitized evidence file (no tokens, no keys).

    TOLLGATE_LIVE_SANDBOX=1 TOLLGATE_SANDBOX_CAPTURE_ID=YOUR_CAPTURE_ID python -m pytest -q -s tests/test_live_sandbox_paypal.py

What it settles about PayPal (each was UNVERIFIED before a sandbox run): a repeated PayPal-Request-Id with
the same body returns the same refund; the refund resource reads back what was sent; the exact error body of
a refused refund. The web pages are tested by hand, not here.
"""
import json
import os
import uuid
from types import SimpleNamespace

import pytest

from approval_receipt import FOUND, PaymentRejected, PaymentTimeout
from app.money import fmt, to_minor, to_value
from app.paypal_adapter import (ENV_CLIENT_ID, ENV_CLIENT_SECRET, KNOWN_STATUSES, SANDBOX_BASE, PayPalAdapter,
                                PayPalClient)
from app.transport import Transport, UrllibTransport
from conftest import LIVE_SANDBOX_FLAG, live_sandbox_enabled

CAPTURE_ENV, AMOUNT_ENV, EVIDENCE_ENV = "TOLLGATE_SANDBOX_CAPTURE_ID", "TOLLGATE_SANDBOX_AMOUNT", "TOLLGATE_LIVE_EVIDENCE"
MAX_MINOR = 500

pytestmark = [pytest.mark.live_sandbox,
              pytest.mark.skipif(not live_sandbox_enabled(), reason=f"live sandbox test: set {LIVE_SANDBOX_FLAG}=1 to run")]


class CountingTransport(Transport):
    """Counts refund POSTs that actually leave this machine."""

    def __init__(self, inner: Transport):
        self.inner, self.refund_posts = inner, 0

    def send(self, req):
        if req.method == "POST" and req.url.endswith("/refund"):
            self.refund_posts += 1
        return self.inner.send(req)


@pytest.fixture(scope="module")
def sandbox():
    missing = [k for k in (ENV_CLIENT_ID, ENV_CLIENT_SECRET, CAPTURE_ENV) if not os.environ.get(k)]
    if missing:
        pytest.fail("missing environment variables: " + ", ".join(missing) + ". Nothing was called.")
    if os.environ.get("PAYPAL_ENV") != "sandbox":
        pytest.fail("PAYPAL_ENV must be exactly 'sandbox'. Nothing was called.")
    amount = to_minor(os.environ.get(AMOUNT_ENV, "1.00"))
    if not 0 < amount <= MAX_MINOR:
        pytest.fail(f"{AMOUNT_ENV} must be above 0 and at most 5.00. Nothing was called.")
    transport = CountingTransport(UrllibTransport())
    client = PayPalClient(transport, base_url=SANDBOX_BASE,
                          credentials=lambda: (os.environ[ENV_CLIENT_ID], os.environ[ENV_CLIENT_SECRET]))
    evidence = {"mode": "sandbox", "capture_id": os.environ[CAPTURE_ENV], "amount": to_value(amount)}
    yield SimpleNamespace(client=client, transport=transport, capture_id=os.environ[CAPTURE_ENV], amount=amount,
                          evidence=evidence)
    evidence["refund_posts"] = transport.refund_posts
    if os.environ.get(EVIDENCE_ENV):
        with open(os.environ[EVIDENCE_ENV], "w", encoding="utf-8") as f:
            json.dump(evidence, f, indent=2, default=str)
        print(f"\nsanitized evidence written to {os.environ[EVIDENCE_ENV]}")


@pytest.fixture(scope="module")
def refund(sandbox):
    """One real refund of the small amount, made once for the tests that need it."""
    adapter = PayPalAdapter(sandbox.client)
    key = str(uuid.uuid4())
    before = sandbox.transport.refund_posts
    refund_id = adapter.pay(sandbox.capture_id, sandbox.amount, "USD", key)
    sandbox.evidence["refund"] = {"id": refund_id, "status": adapter.statuses.get(refund_id),
                                  "refund_posts": sandbox.transport.refund_posts - before}
    print(f"\nrefund {refund_id}: {fmt(sandbox.amount)}, {adapter.statuses.get(refund_id)}")
    return SimpleNamespace(adapter=adapter, key=key, refund_id=refund_id)


def test_login_and_capture_read(sandbox):
    cap = sandbox.client.get_capture(sandbox.capture_id)
    sandbox.evidence["capture"] = {"status": cap.get("status"), "amount": cap.get("amount")}
    assert cap["id"] == sandbox.capture_id
    assert cap["status"] in ("COMPLETED", "PARTIALLY_REFUNDED")
    assert cap["amount"]["currency_code"] == "USD" and to_minor(cap["amount"]["value"]) >= sandbox.amount


def test_refund_returns_an_id_and_a_known_status(sandbox, refund):
    assert refund.refund_id
    assert refund.adapter.statuses[refund.refund_id] in KNOWN_STATUSES
    assert sandbox.evidence["refund"]["refund_posts"] == 1


def test_same_request_id_returns_the_same_refund(sandbox, refund):
    before = sandbox.transport.refund_posts
    again = refund.adapter.pay(sandbox.capture_id, sandbox.amount, "USD", refund.key)
    sandbox.evidence["same_key_replay"] = {"refund_id": again, "same": again == refund.refund_id}
    assert again == refund.refund_id                   # PayPal returned the first refund, made no second one
    assert sandbox.transport.refund_posts == before + 1


def test_refund_resource_reads_back_what_was_sent(sandbox, refund):
    d = sandbox.client.get_refund(refund.refund_id)
    sandbox.evidence["read_back"] = {"id": d.get("id"), "status": d.get("status"), "amount": d.get("amount"),
                                     "custom_id": d.get("custom_id")}
    assert d["id"] == refund.refund_id
    assert d["status"] in ("COMPLETED", "PENDING")
    assert d["amount"]["value"] == to_value(sandbox.amount) and d["amount"]["currency_code"] == "USD"
    assert d.get("custom_id") in (refund.key, None, "")      # the adapter sends the key as custom_id by default


def test_lookup_finds_the_refund_by_its_request_id(refund):
    found = refund.adapter.lookup(refund.key)
    assert found.status == FOUND and found.payment_ref == refund.refund_id


def test_refund_over_the_captured_amount_is_a_definite_refusal_on_the_first_send(sandbox):
    total = to_minor(sandbox.client.get_capture(sandbox.capture_id)["amount"]["value"])
    adapter, key = PayPalAdapter(sandbox.client), str(uuid.uuid4())
    with pytest.raises(PaymentRejected):               # first send: PayPal's explicit refusal, nothing created
        adapter.pay(sandbox.capture_id, total + 1, "USD", key)
    sandbox.evidence["refusal_first_send"] = {"error": adapter.last_error.get(key)}
    print(f"\nPayPal's refusal of a refund over the captured amount: {adapter.last_error.get(key)}")

    with pytest.raises(PaymentTimeout):                # the same id again: a refusal now proves nothing (UNKNOWN)
        adapter.pay(sandbox.capture_id, total + 1, "USD", key)
    sandbox.evidence["refusal_resend"] = {"classified_as": "UNKNOWN", "error": adapter.last_error.get(key)}
