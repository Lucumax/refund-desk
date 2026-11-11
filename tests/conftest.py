import json
import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app  # noqa: E402,F401  (adds vendor/ to sys.path)

from app.fixture_transport import FixturePayPal, WEBHOOK_ID  # noqa: E402
from app.orders import OrderBook  # noqa: E402
from app.server import App  # noqa: E402
from app.service import Service  # noqa: E402

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
KEY = "test-key-for-pytest-only"


def readme_text() -> str:
    """The README plus docs/DETAILS.md, where the long sections of the README live."""
    out = []
    for name in ("README.md", os.path.join("docs", "DETAILS.md")):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            out.append(f.read())
    return "\n".join(out)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


LIVE_SANDBOX_FLAG = "TOLLGATE_LIVE_SANDBOX"


def live_sandbox_enabled() -> bool:
    return os.environ.get(LIVE_SANDBOX_FLAG) == "1"


def pytest_configure(config):
    config.addinivalue_line("markers", "live_sandbox: real calls to your own PayPal sandbox; runs only with "
                                       f"{LIVE_SANDBOX_FLAG}=1, skipped otherwise")


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Tests must never open a socket. The one exception is a test marked live_sandbox, and only when
    TOLLGATE_LIVE_SANDBOX=1 is set; without it that test is skipped before it makes any call."""
    if request.node.get_closest_marker("live_sandbox") and live_sandbox_enabled():
        return

    def boom(*a, **k):
        raise AssertionError("network access attempted in a test")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


@dataclass
class Env:
    svc: Service
    fx: FixturePayPal
    app: App
    clock: Clock
    dir: str
    headers: dict

    def post(self, path, obj=None, headers=None):
        return self.app.handle("POST", path, headers or self.headers, json.dumps(obj or {}).encode())

    def get(self, path, headers=None):
        return self.app.handle("GET", path, headers or self.headers)

    def msg(self, text, order=None, expect=201):
        """A buyer message through the public endpoint (no session needed), with the order link's
        capability. The order defaults to the one named in the text (else ORD-1001)."""
        from app.extractor import normalize_order_ref
        order = order or normalize_order_ref(text) or "ORD-1001"
        r = self.post_public("/api/buyer/messages", {"text": text, "order": order, "cap": self.svc.buyer_capability(order)})
        assert r.status == expect, r.body
        return r.json()

    def post_public(self, path, obj):
        return self.app.handle("POST", path, {}, json.dumps(obj).encode())

    def inbox(self):
        return self.get("/api/inbox").json()

    def item(self, request_id):
        return self.get(f"/api/requests/{request_id}").json()

    def approve(self, request_id, note="ok"):
        v = self.item(request_id)
        return self.post(f"/api/requests/{request_id}/approve",
                         {"nonce": v["approval"]["nonce"], "digest": v["approval"]["digest"], "note": note,
                          "intent_id": v["intent_id"], "amount_minor": v["amount_minor"]})

    def act(self, request_id, action, body=None):
        """POST approve or deny the way the card does: with the exact intent id and amount the card shows (the server refuses
        an owner action that does not name them). Anything the caller puts in `body` wins."""
        v = self.item(request_id)
        bound = {"intent_id": v.get("intent_id"), "amount_minor": v.get("amount_minor")}
        return self.post(f"/api/requests/{request_id}/{action}", {**bound, **(body or {})})

    def deny(self, request_id, note="no"):
        return self.act(request_id, "deny", {"note": note})

    def save_policy(self, text, expect=200):
        """Preview, then save exactly what the preview returned: the only way the save API accepts a rule."""
        p = self.post("/api/policy/preview", {"text": text}).json()
        r = self.post("/api/policy/save", {"text": text, "base_version": p["base_version"], "result_hash": p["result_hash"] or "0" * 64})
        assert r.status == expect, r.body
        return r.json()

    def retry(self, request_id):
        """The owner's two steps for an unresolved refund: re-check first (nothing is sent), then ask for the same-key
        re-send. The server refuses a re-send that was not preceded by a re-check that found no known refund id."""
        self.post(f"/api/requests/{request_id}/recheck")
        return self.post(f"/api/requests/{request_id}/retry")

    def latest_req(self):
        return list(self.svc.store.requests.values())[-1]


def build_env(path, passcode="test-passcode-xyz", extractor=None, **kw):
    ob = OrderBook.load()
    fx = FixturePayPal({o.capture_id: o.total_minor for o in ob.by_ref.values()})
    clock = Clock()
    svc = Service(str(path), fx, ob, extractor=extractor, clock=clock, passcode=passcode, key=KEY,
                  webhook_id=WEBHOOK_ID, mode="fixture", **kw)
    a = App(svc)
    r = a.handle("POST", "/api/login", {}, json.dumps({"passcode": passcode}).encode())
    assert r.status == 200
    cookie = r.headers["Set-Cookie"].split(";")[0]
    return Env(svc, fx, a, clock, str(path), {"cookie": cookie, "x-csrf-token": r.json()["csrf"]})


@pytest.fixture
def env(tmp_path):
    return build_env(tmp_path / "data")


MSG_A = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."
MSG_B = "Order ORD-1002 never arrived. Please send me a full refund."
