"""python -m app            runs the SIMULATION on synthetic fixtures, no keys, no network.
python -m app --mode sandbox   uses your own PayPal sandbox app (see README); needs env vars.
python -m app repro            re-runs the reliability evaluation on fixtures and prints the counts (see README)."""
from __future__ import annotations
import argparse
from decimal import Decimal
import os
import shutil
import sys

from .extractor import RuleBasedExtractor, ModelExtractor
from .fixture_transport import FixturePayPal, WEBHOOK_ID
from .orders import OrderBook, DEFAULT_ORDERS
from .paypal_adapter import env_credentials, SANDBOX_BASE
from .server import App, serve
from .service import Service, StartupIntegrityError, default_checkpoint_dir
from .transport import UrllibTransport


def build_sandbox_service(data_dir: str, orders: OrderBook, extractor, env=None, transport=None,
                          checkpoint_dir: str | None = None, key: str | None = None) -> Service:
    """The one place a sandbox Service is built: used by `--mode sandbox` and by the live script.
    Needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (read when a token is first needed) and a
    signing key (APPROVAL_RECEIPT_HMAC_KEY or `key`). No demo routes exist in this mode."""
    env = os.environ if env is None else env
    passcode = env.get("APP_PASSCODE") or "live-run-unused"
    creds = lambda: (env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"])   # noqa: E731
    if not env.get("PAYPAL_CLIENT_ID") or not env.get("PAYPAL_CLIENT_SECRET"):
        raise RuntimeError("set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app only)")
    if any(o.currency != "USD" for o in orders.by_ref.values()):      # a real refund is made in the order's own currency, which this build does not do
        raise ValueError("Only USD orders are supported in sandbox mode")
    return Service(data_dir, transport or UrllibTransport(), orders, extractor, passcode=passcode,
                   key=key or env.get("APPROVAL_RECEIPT_HMAC_KEY"), webhook_id=env.get("PAYPAL_WEBHOOK_ID", ""),
                   credentials=creds, base_url=SANDBOX_BASE, mode="sandbox", checkpoint_dir=checkpoint_dir)


def session_guard(args):
    """One spend guard per session when a model reads anything (--extractor model, --consent-reader model or ARENA_READER=model: the
    arena's reader shares the desk's cap). Every model call is
    reserved against --max-spend before it is sent. At the cap the model goes quiet: the extractor answers by rules, and the
    consent reader gives no reading, so nothing goes out by itself (see app/spend_guard.py)."""
    arena_model = (os.environ.get("ARENA_READER") or "rules").strip().lower() == "model"
    if args.extractor != "model" and getattr(args, "consent_reader", "none") != "model" and not arena_model:
        return None
    from .spend_guard import SpendGuard

    def notice(reason):
        print("SPEND CAP: " + reason + ". No further model call will be made in this session; the rules answer alone and "
              "nothing is released by itself on a model's word.", file=sys.stderr)
    model = os.environ.get("APP_MODEL_ID")
    if not model:
        raise RuntimeError("set APP_MODEL_ID: the spend guard prices every model call at that model")
    return SpendGuard(model, getattr(args, "max_spend", Decimal("1.00")), getattr(args, "spend_ledger", None), on_halt=notice)


def build(args) -> tuple[App, str]:
    orders = OrderBook.load(args.orders or DEFAULT_ORDERS)
    cp_dir = args.checkpoint_dir or default_checkpoint_dir(args.data_dir)
    if args.fresh:      # the ONLY place saved receipts and their checkpoint are deleted, and only on request
        for d in (args.data_dir, cp_dir):
            if os.path.isdir(d):
                shutil.rmtree(d)
    os.makedirs(args.data_dir, exist_ok=True)
    extractor = RuleBasedExtractor()
    reader_name = None
    guard = session_guard(args)          # None unless a model is in use; one USD cap for this whole session
    if args.extractor == "model":
        from .model_client import make_completer
        completer = make_completer(guard=guard)
        extractor = ModelExtractor(completer)
        reader_name = f"model ({completer.provider}, {completer.model})"
    if args.mode == "fixture":
        transport = FixturePayPal({o.capture_id: o.total_minor for o in orders.by_ref.values()})
        passcode = os.environ.get("APP_PASSCODE", "demo-passcode")
        svc = Service(args.data_dir, transport, orders, extractor, passcode=passcode, webhook_id=WEBHOOK_ID,
                      mode="fixture", checkpoint_dir=cp_dir)
    else:
        passcode = os.environ.get("APP_PASSCODE")
        if not passcode:
            sys.exit("sandbox mode needs APP_PASSCODE (any passcode you choose) in the environment")
        env_credentials()   # fail early with a clear message if the sandbox keys are missing
        svc = build_sandbox_service(args.data_dir, orders, extractor, checkpoint_dir=cp_dir)
    if reader_name:
        svc.reader_name = reader_name
    if getattr(args, "consent_reader", "none") == "model":
        # A model reads the buyer's consent too (app/consent_reader.py). The rules stay the backstop: a reading can only tighten a
        # decision, and can settle only a clause the rules could not read. If the model cannot be had, nothing goes out by itself.
        from .consent_reader import ModelConsentReader
        from .model_client import make_completer
        completer = make_completer(guard=guard)
        svc.agent.consent_reader = ModelConsentReader(completer, f"model consent reader ({completer.provider}, {completer.model})")
    arena_reader = None
    if (os.environ.get("ARENA_READER") or "rules").strip().lower() == "model":
        from .arena_reader import build_reader
        arena_reader = build_reader(guard=guard)      # the arena's model calls draw on the same cap as the desk's
    return App(svc, arena_reader), passcode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app")
    p.add_argument("--mode", choices=("fixture", "sandbox"), default="fixture")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--checkpoint-dir", help="where the signed checkpoint lives (default: next to the data directory; use another disk for real protection)")
    p.add_argument("--orders", help="orders JSON (same shape as fixtures/paypal/orders.json)")
    p.add_argument("--extractor", choices=("rules", "model"), default=os.environ.get("APP_EXTRACTOR", "rules"),
                   help="rules is the labelled offline fallback; model needs APP_MODEL_PROVIDER, key and APP_MODEL_ID env vars")
    p.add_argument("--consent-reader", choices=("none", "model"), default=os.environ.get("APP_CONSENT_READER", "none"),
                   help="none: the rules read consent alone (default); model: a model also reads it (same provider variables as --extractor); "
                        "if its answer cannot be had, nothing goes out by itself")
    p.add_argument("--fresh", action="store_true", help="delete the data directory and its checkpoint first")
    p.add_argument("--max-spend", type=Decimal, default=Decimal("1.00"), metavar="USD",
                   help="hard cap on model spend for this session (default 1.00); applies when --extractor model or --consent-reader model is used. "
                        "A call that could take the session over the cap is never sent")
    p.add_argument("--spend-ledger", help="optional JSON file that carries the spend across sessions (holds no key)")
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "repro":         # python -m app repro: re-run the reliability evaluation (fixture run by default)
        from .evaluation import main as eval_main
        return eval_main(argv)
    p = build_parser()
    args = p.parse_args(argv)
    try:
        app, passcode = build(args)
        app.svc.recover()
    except StartupIntegrityError as e:
        print(f"STOPPED: {e}\nNothing was changed. The saved files are kept as evidence. "
              "Restore the receipts or start over with --fresh (which deletes them).")
        return 3
    except RuntimeError as e:
        print(f"STOPPED: {e}")
        return 2
    if args.mode == "fixture":
        print("SIMULATION: PayPal answers come from synthetic fixtures. No network, no real money, nothing sent to PayPal.")
    else:
        print("mode: sandbox (your own PayPal sandbox app)")
    print(f"open http://127.0.0.1:{args.port}/   merchant passcode: {passcode}")
    print(f"the Refund Desk (the whole story on one screen): http://127.0.0.1:{args.port}/desk.html")
    for o in app.svc.orders.by_ref.values():
        print(f"buyer link for {o.order_ref}: http://127.0.0.1:{args.port}{app.svc.buyer_link(o.order_ref)}")
    serve(app, "127.0.0.1", args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
