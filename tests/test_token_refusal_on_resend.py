"""A refused PayPal login during a RE-SEND leaves the refund UNKNOWN (money held), never FAILED.

The login refusal only proves that THIS attempt sent no refund request. On a re-send an earlier attempt
may already have created the refund, so treating it as a definite failure would release the hold and let
a second refund through on the same order.

Maintainer adaptation: the desk now refuses any re-send outside fixture mode unless the live-resend
switch is on and a recheck has found no known refund id, so the sandbox-mode desk below turns that switch on for
the test and calls `recheck` before each `retry`. The assertions are Xin's, unchanged."""
import pytest

from approval_receipt import PaymentRejected, PaymentTimeout
from app.__main__ import build_sandbox_service
from app.extractor import RuleBasedExtractor
from app.fixture_transport import FixturePayPal
from app.orders import Order, OrderBook
from app.paypal_adapter import PayPalAdapter, PayPalClient
from app.transport import HttpResponse, Transport

FIXTURE_ENV = {"PAYPAL_CLIENT_ID": "fixture-id", "PAYPAL_CLIENT_SECRET": "fixture-secret", "PAYPAL_ENV": "sandbox"}
DAMAGED_10 = "Order ORD-9000 arrived damaged. Please refund $10.00."
DAMAGED_20 = "Order ORD-9000 arrived damaged and the box was crushed. Please refund $20.00."


class TokenOutage(Transport):
    """Wraps the fixture. While `down` is set, the login endpoint answers 503."""

    def __init__(self, inner: Transport):
        self.inner, self.down = inner, False

    def send(self, req):
        if self.down and req.url.endswith("/v1/oauth2/token"):
            return HttpResponse(503, {}, b'{"name": "SERVICE_UNAVAILABLE"}')
        return self.inner.send(req)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A sandbox-mode Service on the fixture with one $50 order, and a switch for the login endpoint."""
    monkeypatch.setattr("app.service.LIVE_RESEND_VERIFIED", True)   # adaptation: a sandbox re-send needs the switch
    fx = FixturePayPal({"CAP-1": 5_000})
    tr = TokenOutage(fx)
    orders = OrderBook([Order("ORD-9000", "CAP-1", 5_000, "USD", "test order", "test buyer")])
    svc = build_sandbox_service(str(tmp_path / "data"), orders, RuleBasedExtractor(), env=FIXTURE_ENV,
                                transport=tr, checkpoint_dir=str(tmp_path / "checkpoint"),
                                key="token-refusal-test-key-" + "0" * 40)
    return svc, svc.buyer_capability("ORD-9000"), fx, tr


def outcome(svc, intent_id):
    return svc.ledger.latest_for_intent(intent_id)["outcome"]


def lost_answer_then_login_outage(svc, tok, fx, tr):
    """$10 refund created at PayPal, answer lost; then a retry while the login endpoint is down."""
    fx.timeout_after_accept = True
    r = svc.message(DAMAGED_10, "ORD-9000", tok, label="r1")
    assert outcome(svc, r["intent_id"]) == "UNKNOWN" and fx.refunds_created == 1
    tr.down, svc.client._token = True, None          # the next call must log in again, and login fails
    svc.recheck(r["intent_id"])                      # adaptation: a re-send needs a recorded recheck first
    svc.retry(r["intent_id"])
    return r["intent_id"]


# ---- the adapter's classification ----------------------------------------------------------------------
def adapter(dispatch_count):
    tr = TokenOutage(FixturePayPal({"CAP-1": 5_000}))
    tr.down = True
    return PayPalAdapter(PayPalClient(tr), dispatch_count=lambda key: dispatch_count)


def test_refused_login_on_a_first_send_is_a_definite_refusal():
    with pytest.raises(PaymentRejected):              # nothing was ever sent: FAILED is correct here
        adapter(dispatch_count=1).pay("CAP-1", 1_000, "USD", "key-1")


def test_refused_login_on_a_resend_is_unknown_not_a_refusal():
    with pytest.raises(PaymentTimeout):               # the first send may have worked: UNKNOWN
        adapter(dispatch_count=2).pay("CAP-1", 1_000, "USD", "key-1")


# ---- end to end through the Service --------------------------------------------------------------------
def test_login_outage_during_retry_keeps_the_refund_unknown(desk):
    svc, tok, fx, tr = desk
    iid = lost_answer_then_login_outage(svc, tok, fx, tr)
    assert outcome(svc, iid) == "UNKNOWN"
    assert fx.refunds_created == 1


def test_while_the_refund_is_unresolved_a_second_request_is_not_paid(desk):
    svc, tok, fx, tr = desk
    lost_answer_then_login_outage(svc, tok, fx, tr)
    tr.down = False
    r2 = svc.message(DAMAGED_20, "ORD-9000", tok, label="r2")
    assert r2["intent_id"] is None and r2["code"] == "ALREADY_IN_PROGRESS"   # the open $10 blocks the order
    assert fx.refunds_created == 1 and fx.refunded_minor("CAP-1") == 1_000


def test_retry_after_the_outage_finds_the_first_refund_and_creates_no_second(desk):
    svc, tok, fx, tr = desk
    iid = lost_answer_then_login_outage(svc, tok, fx, tr)
    tr.down = False
    svc.recheck(iid)                                  # adaptation: a re-send needs a recorded recheck first
    svc.retry(iid)                                    # same PayPal-Request-Id, same body
    assert outcome(svc, iid) == "SUCCEEDED"
    assert fx.refunds_created == 1


def test_a_refused_login_on_the_first_send_still_fails_cleanly(desk):
    svc, tok, fx, tr = desk
    tr.down = True
    r = svc.message(DAMAGED_10, "ORD-9000", tok, label="r1")
    assert outcome(svc, r["intent_id"]) == "FAILED"   # unchanged: no refund request ever left
    assert fx.refund_posts == 0 and fx.refunds_created == 0
