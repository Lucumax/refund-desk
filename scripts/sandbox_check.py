"""Check your PayPal sandbox setup before the other sandbox scripts. No money moves.

READ THIS FILE FIRST. By default it makes NO network call: it checks the Python version and that the
environment variable NAMES are set (values are never printed). With --token --yes it makes ONE call, the
OAuth token request to api-m.sandbox.paypal.com, to prove the sandbox accepts your Client ID and Secret.
The token is not printed or saved.

    python scripts/sandbox_check.py
    python scripts/sandbox_check.py --token --yes
    python scripts/sandbox_check.py --model

Exit codes: 0 ready, 1 a check failed, 2 refused to start.
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402,F401
from app.model_client import ENV_MODEL, ENV_PROVIDER, PROVIDERS  # noqa: E402
from app.paypal_adapter import ENV_CLIENT_ID, ENV_CLIENT_SECRET, SANDBOX_BASE, PayPalClient, PayPalHttpError  # noqa: E402
from app.transport import HostNotAllowed, TransportTimeout, UrllibTransport  # noqa: E402

KEY_ENV = "APPROVAL_RECEIPT_HMAC_KEY"


def main(argv=None, env=None, transport=None) -> int:
    env = os.environ if env is None else env
    p = argparse.ArgumentParser()
    p.add_argument("--token", action="store_true", help="also request one OAuth token from the sandbox (no money moves)")
    p.add_argument("--yes", action="store_true", help="confirm the one real sandbox call that --token makes")
    p.add_argument("--model", action="store_true", help="also check the variables a model reader needs")
    a = p.parse_args(argv)
    failures: list[str] = []

    def check(ok: bool, label: str) -> None:
        print(("PASS " if ok else "FAIL ") + label)
        if not ok:
            failures.append(label)

    check(sys.version_info >= (3, 11), f"Python 3.11 or newer (this is {sys.version.split()[0]})")
    for k in (ENV_CLIENT_ID, ENV_CLIENT_SECRET):
        v = env.get(k) or ""
        check(bool(v), f"{k} is set")
        if v:
            check(v == v.strip(), f"{k} has no leading or trailing spaces")
    check(env.get("PAYPAL_ENV") == "sandbox", "PAYPAL_ENV is exactly 'sandbox'")
    print(f"info: {KEY_ENV} is " + ("set" if env.get(KEY_ENV) else
          "not set (the live script makes a throwaway key; `python -m app --mode sandbox` needs it)"))
    if a.model:
        provider = env.get(ENV_PROVIDER, "anthropic")
        check(provider in PROVIDERS, f"{ENV_PROVIDER} is one of {sorted(PROVIDERS)} (now: {provider})")
        if provider in PROVIDERS:
            check(bool(env.get(PROVIDERS[provider][1])), f"{PROVIDERS[provider][1]} is set")
        check(bool(env.get(ENV_MODEL)), f"{ENV_MODEL} is set")

    if a.token:
        if failures:
            print("fix the failures above first. Nothing was called.")
            return 1
        if not a.yes:
            print("add --yes to confirm one real sandbox call (the token request). Nothing was called.")
            return 2
        client = PayPalClient(transport or UrllibTransport(), base_url=SANDBOX_BASE,
                              credentials=lambda: (env[ENV_CLIENT_ID], env[ENV_CLIENT_SECRET]))
        try:
            client._fetch_token()     # the token request alone; no order, capture or refund is touched
            check(True, "the sandbox accepted the Client ID and Secret (token received, not shown)")
        except PayPalHttpError as e:
            check(False, f"token request refused, HTTP {e.status}: wrong ID or Secret, or keys from a Live app")
        except (TransportTimeout, HostNotAllowed) as e:
            check(False, f"could not reach {SANDBOX_BASE}: {type(e).__name__} {e}")

    print("RESULT:", "READY" if not failures else f"{len(failures)} CHECK(S) FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
