"""scripts/sandbox_check.py and scripts/sandbox_orders.py, on the synthetic fixture: no network, and neither
script ever sends a refund."""
import json
import os
import re
import sys

from app.fixture_transport import FixturePayPal
from app.paypal_adapter import PayPalClient
from app.transport import HttpResponse
from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import sandbox_check  # noqa: E402
import sandbox_orders  # noqa: E402

KEYS = {"PAYPAL_CLIENT_ID": "id-from-env", "PAYPAL_CLIENT_SECRET": "secret-from-env", "PAYPAL_ENV": "sandbox"}


class PayPalWithOrders(FixturePayPal):
    """The fixture plus GET /v2/checkout/orders/{id}, and a capture status that can be set per capture."""

    def __init__(self, captures=None, status=None):
        super().__init__(captures or {})
        self.status = status or {}

    def send(self, req):
        m = re.search(r"/v2/checkout/orders/([^/]+)$", req.url)
        if req.method == "GET" and m:
            o = self.orders.get(m.group(1))
            if o is None:
                return HttpResponse(404, {}, b'{"name": "RESOURCE_NOT_FOUND"}')
            caps = [{"id": o["capture_id"]}] if o["capture_id"] else []
            body = {"id": m.group(1), "status": o["status"], "purchase_units": [{"payments": {"captures": caps}}]}
            return HttpResponse(200, {}, json.dumps(body).encode())
        r = super().send(req)
        m = re.search(r"/v2/payments/captures/([^/]+)$", req.url)
        if req.method == "GET" and m and m.group(1) in self.status and r.status == 200:
            d = r.json()
            d["status"] = self.status[m.group(1)]
            return HttpResponse(200, {}, json.dumps(d).encode())
        return r


def seeded_order(fx, captured=True):
    """An order made the way scripts/seed_sandbox_capture.py makes it."""
    c = PayPalClient(fx)
    body = {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": "USD", "value": "50.00"}}]}
    oid = c.call("POST", "/v2/checkout/orders", body).json()["id"]
    if captured:
        fx.approve_order(oid)
        c.call("POST", f"/v2/checkout/orders/{oid}/capture", {})
    return oid


# ---- sandbox_check -------------------------------------------------------------------------------------------
def test_check_without_keys_fails_and_calls_nothing(capsys):
    fx = FixturePayPal({})
    assert sandbox_check.main(["--token", "--yes"], env={}, transport=fx) == 1
    assert not fx.log


def test_check_token_needs_yes_then_makes_exactly_one_call(capsys):
    fx = FixturePayPal({})
    assert sandbox_check.main(["--token"], env=KEYS, transport=fx) == 2 and not fx.log
    assert sandbox_check.main(["--token", "--yes"], env=KEYS, transport=fx) == 0
    assert fx.token_calls == 1 and fx.refund_posts == 0
    out = capsys.readouterr().out
    assert "RESULT: READY" in out and "secret-from-env" not in out       # the secret is never printed


def test_check_flags_a_pasted_space(capsys):
    assert sandbox_check.main([], env={**KEYS, "PAYPAL_CLIENT_SECRET": "secret "}) == 1


# ---- sandbox_orders -------------------------------------------------------------------------------------------
def run_orders(args, fx, tmp_path, env=KEYS):
    out = tmp_path / "my_orders.json"
    return sandbox_orders.main([*args, "--out", str(out)], env=env, transport=fx), out


def test_orders_file_from_capture_ids_works_with_the_app(tmp_path, capsys):
    from app.orders import OrderBook
    fx = PayPalWithOrders({"C0": 5_000, "C1": 4_200})
    code, out = run_orders(["ORD-2001=C0", "ORD-1001=C1"], fx, tmp_path)
    assert code == 0
    book = OrderBook.load(str(out))
    assert book.get("ORD-2001").capture_id == "C0" and book.get("ORD-2001").total_minor == 5_000
    assert book.get("ORD-1001").total_minor == 4_200
    assert fx.refund_posts == 0


def test_an_order_id_is_resolved_to_its_capture(tmp_path, capsys):
    fx = PayPalWithOrders()
    oid = seeded_order(fx)
    code, out = run_orders([f"ORD-2001={oid}"], fx, tmp_path)
    assert code == 0 and "is an order id" in capsys.readouterr().out
    assert json.load(open(out))["orders"][0]["capture_id"] == fx.orders[oid]["capture_id"]


def test_unusable_captures_are_refused_and_nothing_is_written(tmp_path, capsys):
    fx = PayPalWithOrders({"SMALL": 5_000, "USED": 5_000}, status={"USED": "PARTIALLY_REFUNDED"})
    never_captured = seeded_order(fx, captured=False)
    for args in (["ORD-1003=SMALL"], ["ORD-2001=USED"], [f"ORD-2001={never_captured}"], ["ORD-2001=NOPE"]):
        code, out = run_orders(args, fx, tmp_path)
        assert code == 1 and not out.exists(), args
    assert fx.refund_posts == 0


def test_bad_arguments_are_refused_before_any_call(tmp_path, capsys):
    fx = PayPalWithOrders({"C0": 5_000})
    for args, env in ((["ORD-2001=NEW_ID"], KEYS), (["ORD-2001=C0", "ORD-1001=C0"], KEYS),
                      (["ORD-9999=C0"], KEYS), (["ORD-2001"], KEYS), (["ORD-2001=C0"], {})):
        code, out = run_orders(args, fx, tmp_path, env=env)
        assert code == 2 and not out.exists(), args
    assert not fx.log
