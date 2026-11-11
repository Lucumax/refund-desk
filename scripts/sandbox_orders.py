"""Write the orders file for running the web app on YOUR OWN PayPal sandbox. No money moves.

READ THIS FILE FIRST. It only READS your captures from api-m.sandbox.paypal.com (GET requests) and writes
my_orders.json (git-ignored) for `python -m app --mode sandbox --orders my_orders.json`.

Give it an order name and the id the seed script printed (the CAPTURE ID, or the order id: it then finds
that order's capture). Each order needs its own fresh capture:

    python scripts/sandbox_orders.py ORD-2001=CAPTURE_ID
    python scripts/sandbox_orders.py ORD-2001=ID ORD-1001=ID ORD-1002=ID ORD-1003=ID ORD-1008=ID

ORD-2001 is for typing your own messages on the Refund Desk. ORD-1001, ORD-1002, ORD-1003 and ORD-1008 make
the desk's sample buttons for those orders appear; each has a smallest capture that gives the same result
as the simulation (see docs/SANDBOX_TESTING.md).

Needs PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET and PAYPAL_ENV=sandbox.
Exit codes: 0 file written, 1 a capture is not usable, 2 refused to start.
"""
from __future__ import annotations
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app  # noqa: E402,F401
from app.money import fmt, to_minor  # noqa: E402
from app.paypal_adapter import ENV_CLIENT_ID, ENV_CLIENT_SECRET, SANDBOX_BASE, PayPalClient, PayPalHttpError  # noqa: E402
from app.transport import TransportTimeout, UrllibTransport  # noqa: E402

# order -> (what it is for, item, smallest capture that behaves like the simulation, largest)
ORDERS = {
    "ORD-2001": ("Or write your own ($30 approved, $10 automatic, one hostile message)", "Sandbox test order", 5_000, 20_000),
    "ORD-1001": ("Ordinary request, inside the rule ($18 refunded by itself)", "Sample ceramic mug set", 1_800, 20_000),
    "ORD-1002": ("Larger request, needs the owner (full refund after you approve)", "Sample desk lamp", 2_501, 20_000),
    "ORD-1003": ("Message tries to give the helper orders (refused, no money)", "Sample canvas tote", 8_500, 20_000),
    "ORD-1008": ("Taken-over reader quotes unrelated words (held, you deny)", "Sample phone stand", 2_000, 20_000),
}
PLACEHOLDERS = {"NEW_ID", "CAPTURE_ID", "YOUR_CAPTURE_ID", "ID", "NEW_CAPTURE_ID"}


def resolve(client: PayPalClient, given: str) -> tuple[dict, str | None]:
    """The capture for `given`, which may be a capture id or the seed script's order id. Reads only."""
    try:
        return client.get_capture(given), None
    except PayPalHttpError as e:
        if e.status != 404:
            raise
    r = client.call("GET", f"/v2/checkout/orders/{given}")
    if r.status != 200:
        raise PayPalHttpError(404, "RESOURCE_NOT_FOUND", "neither a capture id nor an order id at PayPal")
    order = r.json()
    captures = ((order.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or []
    if not captures:
        raise PayPalHttpError(409, "ORDER_NOT_CAPTURED", f"{given} is an order id (status {order.get('status')}) that "
                              "was never captured; run the seed again and approve before pressing Enter")
    return client.get_capture(captures[0]["id"]), f"{given} is an order id; using its capture {captures[0]['id']}"


def main(argv=None, env=None, transport=None) -> int:
    env = os.environ if env is None else env
    p = argparse.ArgumentParser()
    p.add_argument("pairs", nargs="+", metavar="ORDER=ID", help="for example ORD-2001=7XY12345AB678901C")
    p.add_argument("--out", default=os.path.join(ROOT, "my_orders.json"), help="where to write the orders file")
    a = p.parse_args(argv)
    if any("=" not in x for x in a.pairs):
        print("each argument is ORDER=ID, for example ORD-2001=7XY12345AB678901C. Nothing was called.")
        return 2
    pairs = dict(x.split("=", 1) for x in a.pairs)
    unknown = [k for k in pairs if k not in ORDERS]
    if unknown or len(pairs) != len(a.pairs):
        print("unknown or repeated order: " + ", ".join(unknown or a.pairs) + ". Orders: " + ", ".join(ORDERS)
              + ". Nothing was called.")
        return 2
    placeholders = [f"{k}={v}" for k, v in pairs.items() if v.upper() in PLACEHOLDERS]
    if placeholders:
        print("replace the placeholder with the real id from the seed output (the 'CAPTURE ID:' line): "
              + ", ".join(placeholders) + ". Nothing was called.")
        return 2
    if len(set(pairs.values())) != len(pairs):
        print("each order needs its own capture; the same id is used twice. Nothing was called.")
        return 2
    if not env.get(ENV_CLIENT_ID) or not env.get(ENV_CLIENT_SECRET) or env.get("PAYPAL_ENV") != "sandbox":
        print("load your sandbox keys first: set -a; source .env.local; set +a. Nothing was called.")
        return 2
    client = PayPalClient(transport or UrllibTransport(), base_url=SANDBOX_BASE,
                          credentials=lambda: (env[ENV_CLIENT_ID], env[ENV_CLIENT_SECRET]))
    orders, problems = [], []
    for ref, given in pairs.items():
        purpose, item, low, high = ORDERS[ref]
        try:
            cap, note = resolve(client, given)
        except (PayPalHttpError, TransportTimeout) as e:
            problems.append(f"{ref}: {given} could not be read ({e})")
            continue
        if note:
            print(f"note: {note}")
        total, status = to_minor(cap["amount"]["value"]), cap.get("status")
        print(f"{ref}  {cap['id']}  {fmt(total)}  {status}  -> {purpose}")
        if status != "COMPLETED":
            problems.append(f"{ref}: capture is {status}; use a fresh one (the app cannot see refunds it did not make)")
        if cap["amount"]["currency_code"] != "USD":
            problems.append(f"{ref}: capture is not in USD")
        if not low <= total <= high:
            problems.append(f"{ref}: capture is {fmt(total)}; this order needs {fmt(low)} to {fmt(high)}")
        orders.append({"order_ref": ref, "capture_id": cap["id"], "total": cap["amount"]["value"], "currency": "USD",
                       "item": item, "buyer_label": "Sandbox buyer"})
    if problems:
        print("\nNOT written:\n  " + "\n  ".join(problems))
        return 1
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"orders": orders}, f, indent=1)
        f.write("\n")
    print(f"\nwritten: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
