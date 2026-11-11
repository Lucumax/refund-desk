"""CLI: `python -m approval_receipt demo` and `... verify FILE`. Output is JSON lines."""
from __future__ import annotations
import argparse, json, os
from datetime import datetime, timedelta, timezone
from .models import Mandate, Intent, ApproverSession
from .adapters import MockAdapter
from .receipts import Ledger
from .engine import Engine

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)

MANDATE = Mandate("M-12", "agent-buyer-1", 50_000, 100_000, ("acme-supplies",),
                  ("office_supplies",), datetime(2027, 1, 1, tzinfo=timezone.utc), "USD", 1)


def _intent(n: int, payee: str, amount_minor: int) -> Intent:
    return Intent(f"I-{n}", "agent-buyer-1", payee, amount_minor, "USD", "office_supplies",
                  "demo reason code", f"demo-key-{n}", T0 + timedelta(minutes=n))


def run_demo(path: str, key: str | None = None) -> tuple[list[dict], dict]:
    """Returns (output lines, signed checkpoint)."""
    for p in (path, path + ".checkpoint.json"):
        if os.path.exists(p):
            os.remove(p)
    t = {"now": T0}

    def clock():
        t["now"] += timedelta(seconds=1)
        return t["now"]

    adapter = MockAdapter()
    # Demo only: with no key in the environment this uses the PUBLIC demo key, loudly, and
    # every record is marked key_mode=DEMO.
    ledger = Ledger(path, key, allow_demo_key=True)
    engine = Engine([MANDATE], ledger, adapter, clock=clock)
    out = []

    def show(scene, rec, decision=None, **extra):
        d = decision or rec["decision"]
        out.append({"scene": scene, "decision": d["decision"], "rule_id": d["rule_id"],
                    "reason_code": d["reason_code"], "outcome": rec["outcome"],
                    "receipt_id": rec["id"], "payment_ref": rec["payment_ref"],
                    "flags": rec["flags"], "approver": rec["approver"],
                    "payment_calls": adapter.calls, **extra})

    show("A", engine.submit(_intent(1, "acme-supplies", 42_000)).receipt)
    show("B1", engine.submit(_intent(2, "acme-supplies", 230_000)).receipt)
    pending = engine.pending_approvals()[0]
    session = ApproverSession("approver-1", "demo-session")
    show("B2", engine.approve("I-2", session, pending["approval_nonce"],
                              pending["intent_digest"], "demo approval"))
    show("C", engine.submit(_intent(3, "unknown-payee", 10_000)).receipt)
    cp = ledger.checkpoint()
    out.append({"verify": ledger.verify(cp).as_dict()})
    return out, cp


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="approval_receipt")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run scenes A, B, C")
    d.add_argument("--out", default="demo_receipts.jsonl", help="receipt file (overwritten)")
    v = sub.add_parser("verify", help="verify a receipt file; key from the environment")
    v.add_argument("file")
    v.add_argument("--checkpoint", help="separately saved checkpoint JSON file")
    v.add_argument("--allow-demo-key", action="store_true", help="verify a DEMO-key ledger")
    m = sub.add_parser("manifest", help="check or write VENDOR_MANIFEST.json")
    m.add_argument("--root", help="directory holding approval_receipt/ and the manifest")
    m.add_argument("--check", action="store_true", help="verify (the default)")
    m.add_argument("--write", action="store_true", help="regenerate (core maintainers only)")
    a = p.parse_args(argv)
    if a.cmd == "manifest":
        from . import manifest
        if a.write:
            manifest.write_manifest(a.root)
        ok, problems = manifest.verify_manifest(a.root)
        print(json.dumps({"manifest_ok": ok, "problems": problems}, sort_keys=True))
        return 0 if ok else 1
    if a.cmd == "demo":
        lines, cp = run_demo(a.out)
        with open(a.out + ".checkpoint.json", "w", encoding="utf-8") as f:
            json.dump(cp, f, sort_keys=True)
        ok = lines[-1]["verify"]["ok"]
    else:
        cp = None
        if a.checkpoint:
            with open(a.checkpoint, encoding="utf-8") as f:
                cp = json.load(f)
        res = Ledger(a.file, allow_demo_key=a.allow_demo_key).verify(cp)
        lines, ok = [{"verify": res.as_dict()}], res.ok
    for line in lines:
        print(json.dumps(line, sort_keys=True))
    return 0 if ok else 1
