"""Demo control panel actions (fixture mode only). Scenes post sample buyer messages through
the SAME public path a real buyer uses, and arm the SYNTHETIC fixture transport's fault
switches. Nothing here touches a network. Sample message text lives in copy/strings.draft.json."""
from __future__ import annotations
import json
import os
import shutil

from . import views
from .fixture_transport import FixturePayPal
from .strings import S

SCENES = ("A", "B", "C_UNKNOWN", "C_INJECTION", "D_TIMEOUT", "E_PENDING")
SCENE_ORDER = {"A": "ORD-1001", "B": "ORD-1002", "C_UNKNOWN": "ORD-1001", "C_INJECTION": "ORD-1003",
               "D_TIMEOUT": "ORD-1004", "E_PENDING": "ORD-1005"}   # the order link each scene "clicks"


class Demo:
    def __init__(self, svc):
        self.svc = svc
        self.fixture: FixturePayPal = svc.client.transport
        self._event_n = 0

    def handle(self, path: str, data: dict) -> dict:
        if path == "/api/demo/scenes":
            return {"scenes": [{"id": s, "label": S("demo.scene." + s)} for s in SCENES]}
        if path == "/api/demo/scene":
            return self.scene(str(data.get("scene", "")))
        if path == "/api/demo/poll":
            return {"touched": len(self.svc.poll())}
        if path == "/api/demo/webhook":
            return self.webhook(replay=bool(data.get("replay")))
        if path == "/api/demo/counts":
            return {"posts": self.fixture.refund_posts, "created": self.fixture.refunds_created}
        if path == "/api/demo/tamper":
            return self.tamper()
        raise KeyError("UNKNOWN_DEMO_ACTION")

    def links(self) -> list[dict]:
        return [{"order_ref": o.order_ref, "item": o.item, "url": self.svc.buyer_link(o.order_ref)}
                for o in self.svc.orders.by_ref.values()]

    def scene(self, name: str) -> dict:
        if name not in SCENES:
            raise KeyError("UNKNOWN_SCENE")
        if name == "D_TIMEOUT":
            self.fixture.timeout_after_accept = True     # PayPal accepts, our answer is lost
        if name == "E_PENDING":
            self.fixture.next_refund_status = "PENDING"
        try:
            order = SCENE_ORDER[name]
            req = self.svc.message(S("demo.msg." + name), order, self.svc.buyer_capability(order))
        finally:
            self.fixture.next_refund_status = "COMPLETED"
        return {"scene": name, "request": views.proposal_view(self.svc, req)}

    def webhook(self, replay: bool = False) -> dict:
        if replay and getattr(self, "_last", None):
            headers, body = self._last
        else:
            pending = [rid for rid, r in self.fixture.refunds.items() if r["status"] == "PENDING"]
            target = pending[0] if pending else next(iter(self.fixture.refunds), None)
            if target is None:
                return {"result": "NO_REFUND_YET"}
            self.fixture.settle(target, "COMPLETED")
            self._event_n += 1
            headers, body = self.fixture.make_webhook(target, "PAYMENT.CAPTURE.REFUNDED", "COMPLETED",
                                                      f"WH-FIX-{self._event_n:04d}")
            self._last = (headers, body)
        status, result = self.svc.webhook(headers, body)
        return {"http_status": status, **result}

    def tamper(self) -> dict:
        """Write a COPY of the receipts with one amount edited, so Verify can name the broken
        record without damaging the real ledger."""
        src = self.svc.ledger.path
        dst = os.path.join(self.svc.data_dir, "receipts.tampered.jsonl")
        shutil.copyfile(src, dst)
        with open(dst, encoding="utf-8") as f:
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            rec = json.loads(line)
            if rec.get("intent"):
                rec["intent"]["amount_minor"] += 100
                lines[i] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
                break
        with open(dst, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return views.verify_view(self.svc, dst)
