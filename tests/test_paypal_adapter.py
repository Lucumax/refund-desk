import json

import pytest

from approval_receipt import PaymentRejected, PaymentTimeout, LookupResult, INDETERMINATE
from app.fixture_transport import FixturePayPal
from app.money import to_minor, to_value, fmt
from app.paypal_adapter import PayPalAdapter, PayPalClient
from app.transport import UrllibTransport, HostNotAllowed, HttpRequest


def make(captures=None):
    fx = FixturePayPal(captures or {"CAP-1": 10_000})
    return fx, PayPalAdapter(PayPalClient(fx))


def test_money_helpers():
    assert to_minor("18.00") == 1800 and to_minor("0.1") == 10 and to_value(1805) == "18.05"
    assert fmt(123456) == "$1,234.56"
    with pytest.raises(ValueError):
        to_minor("abc")


def test_refund_sends_request_id_prefer_header_and_custom_id_not_invoice_id():
    """Sprint 1 (W1, W3): the key travels as PayPal-Request-Id, our intent id as custom_id, the full
    resource is asked for with Prefer, and invoice_id is not sent at all."""
    fx, ad = make()
    ref = ad.pay("CAP-1", 1800, "USD", "key-1")
    assert ref.startswith("FIXREF-") and fx.refunds_created == 1
    assert fx.refunds[ref]["minor"] == 1800 and fx.refunds[ref]["custom_id"] == "key-1"
    assert ("POST", "/v2/payments/captures/CAP-1/refund") in fx.log
    sent = fx.refund_requests[0]
    assert sent["headers"]["paypal-request-id"] == "key-1"
    assert sent["headers"]["prefer"] == "return=representation"
    assert sent["body"] == {"amount": {"value": "18.00", "currency_code": "USD"}, "custom_id": "key-1"}
    assert "invoice_id" not in sent["body"]


def test_custom_id_is_the_intent_id_when_the_service_supplies_it():
    fx = FixturePayPal({"CAP-1": 10_000})
    ad = PayPalAdapter(PayPalClient(fx), custom_id_for_key=lambda key: "I-0042")
    ad.pay("CAP-1", 100, "USD", "key-1")
    assert fx.refund_requests[0]["body"]["custom_id"] == "I-0042"
    assert fx.refund_requests[0]["headers"]["paypal-request-id"] == "key-1"


def test_the_answer_is_minimal_without_the_prefer_header_and_the_adapter_still_reads_it():
    """PayPal's default answer is id, status, links only. The fixture follows that, and the adapter
    copes with it (it never needs more than the id and the status)."""
    fx = FixturePayPal({"CAP-1": 10_000})
    PayPalClient(fx).refund("CAP-1", 500, "USD", "k0", "I-1")             # sent WITH Prefer by the client
    from app.transport import HttpRequest
    import json as _json
    token = PayPalClient(fx)
    r = token.call("POST", "/v2/payments/captures/CAP-1/refund",
                   {"amount": {"value": "5.00", "currency_code": "USD"}, "custom_id": "I-2"}, {"PayPal-Request-Id": "k1"})
    assert set(r.json()) == {"id", "status", "links"}                     # minimal: no Prefer header sent
    full = PayPalClient(fx).refund("CAP-1", 300, "USD", "k2", "I-3")
    assert full.json()["amount"]["value"] == "3.00" and full.json()["custom_id"] == "I-3"


def test_token_is_cached_across_calls():
    fx, ad = make()
    ad.pay("CAP-1", 100, "USD", "k1")
    ad.pay("CAP-1", 100, "USD", "k2")
    assert fx.token_calls == 1


def test_expired_token_is_refreshed_and_refund_not_duplicated():
    fx, ad = make()
    ad.pay("CAP-1", 100, "USD", "k0")
    fx.expire_token_once = True
    ad.pay("CAP-1", 200, "USD", "k1")
    assert fx.token_calls == 2 and fx.refunds_created == 2


def test_same_key_same_refund_no_duplicate():
    fx, ad = make()
    r1 = ad.pay("CAP-1", 1800, "USD", "k1")
    r2 = ad.pay("CAP-1", 1800, "USD", "k1")
    assert r1 == r2 and fx.refunds_created == 1 and fx.refund_posts == 2


def test_same_key_different_amount_is_unknown_not_a_definite_refusal():
    """Sprint 1: a mismatch on a re-sent request id proves nothing about the first send, so it
    is UNKNOWN, never a definite failure that would release the reservation."""
    fx, ad = make()
    ad.pay("CAP-1", 1800, "USD", "k1")
    with pytest.raises(PaymentTimeout):
        ad.pay("CAP-1", 1900, "USD", "k1")
    assert fx.refunds_created == 1


def test_over_refund_is_a_definite_rejection():
    fx, ad = make({"CAP-1": 1000})
    with pytest.raises(PaymentRejected):
        ad.pay("CAP-1", 1001, "USD", "k1")
    assert ad.last_error["k1"] == "REFUND_AMOUNT_EXCEEDED" and fx.refunds_created == 0


def test_unknown_capture_is_rejected():
    _, ad = make()
    with pytest.raises(PaymentRejected):
        ad.pay("NOPE", 100, "USD", "k1")


def test_timeout_before_accept_is_unknown_and_nothing_created():
    fx, ad = make()
    fx.timeout_before_accept = True
    with pytest.raises(PaymentTimeout):
        ad.pay("CAP-1", 100, "USD", "k1")
    assert fx.refunds_created == 0


def test_timeout_after_accept_is_unknown_and_retry_reuses_key():
    fx, ad = make()
    fx.timeout_after_accept = True
    with pytest.raises(PaymentTimeout):
        ad.pay("CAP-1", 100, "USD", "k1")
    assert fx.refunds_created == 1               # PayPal did take it; we did not hear
    ref = ad.pay("CAP-1", 100, "USD", "k1")       # retry, same key
    assert fx.refunds_created == 1 and ref in fx.refunds


@pytest.mark.parametrize("flag", ["server_error_before_accept", "server_error_after_accept"])
def test_5xx_is_treated_as_unknown(flag):
    fx, ad = make()
    setattr(fx, flag, True)
    with pytest.raises(PaymentTimeout):
        ad.pay("CAP-1", 100, "USD", "k1")


def test_pending_refund_is_returned_with_status():
    fx, ad = make()
    fx.next_refund_status = "PENDING"
    ref = ad.pay("CAP-1", 100, "USD", "k1")
    assert ad.statuses[ref] == "PENDING"


def test_lookup_reads_known_refund_and_is_indeterminate_for_unknown_key():
    fx, ad = make()
    ref = ad.pay("CAP-1", 100, "USD", "k1")
    assert ad.lookup("k1") == LookupResult.found(ref)
    assert ad.lookup("other").status == INDETERMINATE      # never dispatched, no time record: not provable
    assert ("GET", f"/v2/payments/refunds/{ref}") in fx.log


def test_lookup_of_a_failed_refund_is_found_so_the_key_is_never_reused():
    fx, ad = make()
    ref = ad.pay("CAP-1", 100, "USD", "k1")
    fx.settle(ref, "FAILED")
    assert ad.lookup("k1") == LookupResult.found(ref)


def test_lookup_read_outage_is_indeterminate_not_an_exception_and_not_absent():
    fx, ad = make()
    ad.pay("CAP-1", 100, "USD", "k1")
    fx.read_unavailable = True
    assert ad.lookup("k1").status == INDETERMINATE


def test_urllib_transport_refuses_other_hosts_without_any_network():
    t = UrllibTransport()
    for url in ["https://api-m.paypal.com/v1/oauth2/token", "http://api-m.sandbox.paypal.com/x",
                "https://evil.example/x"]:
        with pytest.raises(HostNotAllowed):
            t.send(HttpRequest("GET", url, {}))


def test_fixture_files_are_labeled_synthetic():
    import glob
    import os
    from app.fixture_transport import FIX_DIR
    files = glob.glob(os.path.join(FIX_DIR, "*.json"))
    assert len(files) >= 8
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        assert "SYNTHETIC" in d["_label"], f
        if os.path.basename(f) != "orders.json":
            assert d["_unverified_fields"], f
