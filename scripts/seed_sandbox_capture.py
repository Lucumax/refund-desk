"""Make ONE captured test payment in YOUR OWN PayPal sandbox, so the refund path has something to refund.
Never run by tests (they use the synthetic fixture) or by the build tooling.

READ THIS FILE FIRST. It makes real HTTPS calls to api-m.sandbox.paypal.com (and only that host) when you run
it. Sandbox test money only.

What it does (the documented Orders v2 sequence; response shapes are UNVERIFIED until you run it):
  1. Gets a token with your sandbox app's Client ID and Secret.
  2. Creates an order for one USD amount (default 50.00) with intent CAPTURE.
  3. Prints the approval link. YOU open it in a browser and approve with the SANDBOX BUYER account (the
     personal sandbox account, password on the Sandbox Accounts page in the developer dashboard). Never use
     your real PayPal login there.
  4. After you press Enter, captures the order and prints the CAPTURE ID. That id is what
     scripts/live_sandbox_run.py needs (the order id is NOT the capture id).

Needs: PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_ENV=sandbox in your shell (names only here), and --yes.

    python scripts/seed_sandbox_capture.py --amount 50.00 --yes

Exit codes: 0 a capture id was printed, 1 a call failed, 2 refused to start.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402,F401
from app.money import to_minor, to_value  # noqa: E402
from app.paypal_adapter import PayPalClient, PayPalHttpError, ENV_CLIENT_ID, ENV_CLIENT_SECRET, SANDBOX_BASE  # noqa: E402
from app.transport import UrllibTransport, TransportTimeout  # noqa: E402

MAX_SEED_MINOR = 20_000     # a sandbox seed never needs more than $200


def main(argv=None, env=None, transport=None, wait=None) -> int:
    env = os.environ if env is None else env
    p = argparse.ArgumentParser()
    p.add_argument("--amount", default="50.00", help="payment amount in dollars (default 50.00; the live run needs about 31.00 or more)")
    p.add_argument("--yes", action="store_true", help="confirm that you want real sandbox calls")
    a = p.parse_args(argv)
    missing = [k for k in (ENV_CLIENT_ID, ENV_CLIENT_SECRET) if not env.get(k)]
    if missing:
        print("missing environment variables: " + ", ".join(missing) + ". Nothing was called.")
        return 2
    if env.get("PAYPAL_ENV") != "sandbox":
        print("PAYPAL_ENV must be exactly 'sandbox'. Nothing was called.")
        return 2
    if not a.yes:
        print("add --yes to confirm real sandbox calls. Nothing was called.")
        return 2
    try:
        minor = to_minor(a.amount)
    except ValueError:
        print("bad --amount")
        return 2
    if not 100 <= minor <= MAX_SEED_MINOR:
        print("--amount must be between 1.00 and 200.00")
        return 2
    client = PayPalClient(transport or UrllibTransport(), credentials=lambda: (env[ENV_CLIENT_ID], env[ENV_CLIENT_SECRET]),
                          base_url=SANDBOX_BASE)
    try:
        body = {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": "USD", "value": to_value(minor)}}]}
        r = client.call("POST", "/v2/checkout/orders", body, {"PayPal-Request-Id": str(uuid.uuid4())})
        if r.status not in (200, 201):
            raise client.err(r)
        order = r.json()
        link = next((l["href"] for l in order.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
        if not order.get("id") or not link:
            print("the order answer had no id or approval link; nothing more was done. Answer keys:", sorted(order))
            return 1
        print(f"order created: {order['id']} (status {order.get('status')}), {to_value(minor)} USD")
        print("1. Open this link in a browser and approve with the SANDBOX BUYER account:")
        print("   " + link)
        if wait is None:
            input("2. When you have approved, press Enter here to capture the payment... ")
        else:
            wait(order["id"])
        r = client.call("POST", f"/v2/checkout/orders/{order['id']}/capture", {}, {"PayPal-Request-Id": str(uuid.uuid4())})
        if r.status not in (200, 201):
            raise client.err(r)
        cap = r.json()
        captures = ((cap.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or []
        if not captures or not captures[0].get("id"):
            print("the capture answer had no capture id. Answer keys:", sorted(cap))
            return 1
        print(f"CAPTURE ID: {captures[0]['id']}  (status {captures[0].get('status')}, amount {json.dumps(captures[0].get('amount'))})")
        print("Next: python scripts/live_sandbox_run.py --capture-id " + captures[0]["id"] + " --amount 1.00 --yes")
        return 0
    except (PayPalHttpError, TransportTimeout) as e:
        print("FAIL:", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
