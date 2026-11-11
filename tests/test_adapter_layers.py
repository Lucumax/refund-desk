"""Sprint 3g: the refused-login-on-a-re-send protection (Xin Liang Chia, 009552e) holds in two independent layers.
Layer 1 classifies at its origin (`_pay`); layer 2 is `pay()`, which turns any rejection of a re-send into UNKNOWN.
Xin's own tests (`test_token_refusal_on_resend.py`) cover the whole path; these two pin each layer on its own so a
later refactor of one cannot silently reopen the bug. Fixture only, no network."""
import pytest

from approval_receipt import PaymentRejected, PaymentTimeout
from app.fixture_transport import FixturePayPal
from app.paypal_adapter import PayPalAdapter, PayPalClient
from app.transport import HttpResponse, Transport


class LoginDown(Transport):
    def send(self, req):
        return HttpResponse(503, {}, b'{"name": "SERVICE_UNAVAILABLE"}')


def adapter():
    return PayPalAdapter(PayPalClient(LoginDown()))


def test_layer_one_a_refused_login_on_a_resend_is_classified_unknown_at_its_origin():
    ad = adapter()
    with pytest.raises(PaymentTimeout):
        ad._pay("CAP-1", 1_000, "USD", "k1", resend=True)       # called directly, without the pay() wrapper
    assert ad.last_error["k1"] == "RESEND_REFUSED"


def test_layer_one_a_refused_login_on_a_first_send_is_still_a_definite_refusal():
    with pytest.raises(PaymentRejected):
        adapter()._pay("CAP-1", 1_000, "USD", "k1", resend=False)


def test_layer_two_pay_turns_a_rejected_resend_into_unknown_even_if_layer_one_is_bypassed(monkeypatch):
    ad = adapter()
    def reject(*a, **k):
        raise PaymentRejected("token refused")                  # what the pre-3g classification would have raised
    monkeypatch.setattr(ad, "_pay", reject)
    ad._sent["k1"] = 1                                          # a send is already recorded, so this one is a re-send
    with pytest.raises(PaymentTimeout):
        ad.pay("CAP-1", 1_000, "USD", "k1")
    assert ad.last_error["k1"] == "RESEND_REFUSED"
