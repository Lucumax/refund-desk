"""SYNTHETIC FIXTURE transport. It imitates the shape of PayPal sandbox responses for the
endpoint names in the spec (token, capture, refund, refund read, webhook verify). It is
NOT PayPal and proves nothing about PayPal's real behavior. Every response body comes from
a JSON file in fixtures/paypal/ marked SYNTHETIC; fields we are unsure of are listed there
under _unverified_fields. No sockets are opened."""
from __future__ import annotations
import hashlib
import json
import os
import re

from . import REPO_ROOT
from .money import to_minor, to_value
from .transport import HttpRequest, HttpResponse, Transport, TransportTimeout

FIX_DIR = os.path.join(REPO_ROOT, "fixtures", "paypal")
VALID_SIG = "FIXTURE-VALID-SIGNATURE"
WEBHOOK_ID = "FIXTURE-WEBHOOK-ID"


def load_fixture(name: str, **fill) -> dict:
    with open(os.path.join(FIX_DIR, name), encoding="utf-8") as f:
        text = f.read()
    for k, v in fill.items():
        text = text.replace("{" + k + "}", str(v))
    data = json.loads(text)
    data.pop("_label", None)
    data.pop("_unverified_fields", None)
    return data


def _resp(status: int, data: dict) -> HttpResponse:
    return HttpResponse(status, {"Content-Type": "application/json"}, json.dumps(data).encode())


class FixturePayPal(Transport):
    """In-memory stand-in with fault switches for tests and the demo control panel."""

    def __init__(self, captures: dict[str, int] | None = None):
        self.captures = dict(captures or {})        # capture_id -> total minor units
        self.refunds: dict[str, dict] = {}          # refund_id -> record
        self.by_request_id: dict[str, tuple[str, HttpResponse]] = {}
        self.log: list[tuple[str, str]] = []        # (method, path) of every request
        self.refund_posts = 0                       # POST refund calls received
        self.refund_requests: list[dict] = []       # headers and body of every refund POST (for tests)
        self.refunds_created = 0                    # refunds actually created
        self.token_calls = 0
        self.orders: dict[str, dict] = {}           # Orders v2 seeding: order_id -> {minor, status, capture_id}
        # one-shot fault switches
        self.timeout_before_accept = False
        self.timeout_after_accept = False
        self.crash_after_accept = False
        self.server_error_after_accept = False
        self.server_error_before_accept = False
        self.malformed_after_accept = False     # 201 with a body that is not JSON
        self.odd_status_after_accept = False    # 201 with a status we do not recognise
        self.conflict_after_accept = False      # 409
        self.redirect_after_accept = False      # 302
        self.expire_token_once = False
        self.verify_unavailable = False
        self.next_refund_status = "COMPLETED"       # or "PENDING"
        self.read_unavailable = False               # GET refund times out

    # -- helpers for tests and the demo --------------------------------------
    def refunded_minor(self, capture_id: str) -> int:
        return sum(r["minor"] for r in self.refunds.values()
                   if r["capture_id"] == capture_id and r["status"] in ("COMPLETED", "PENDING"))

    def settle(self, refund_id: str, status: str = "COMPLETED") -> None:
        self.refunds[refund_id]["status"] = status

    def make_webhook(self, refund_id: str, event_type: str, status: str, event_id: str,
                     create_time: str = "2026-11-01T09:05:00Z", signature: str = VALID_SIG):
        r = self.refunds[refund_id]
        body = json.dumps(load_fixture("webhook_refund.json", event_id=event_id, event_type=event_type,
                                       create_time=create_time, refund_id=refund_id, status=status,
                                       value=to_value(r["minor"]), custom_id=r["custom_id"])).encode()
        headers = {"paypal-transmission-id": "fix-" + event_id, "paypal-transmission-time": create_time,
                   "paypal-transmission-sig": signature, "paypal-cert-url": "https://fixture.invalid/cert",
                   "paypal-auth-algo": "FIXTURE"}
        return headers, body

    # -- transport ----------------------------------------------------------
    def send(self, req: HttpRequest) -> HttpResponse:
        path = re.sub(r"^https?://[^/]+", "", req.url)
        self.log.append((req.method, path))
        h = {k.lower(): v for k, v in req.headers.items()}
        if path == "/v1/oauth2/token":
            self.token_calls += 1
            if not h.get("authorization", "").startswith("Basic "):
                return _resp(401, {"error": "invalid_client"})
            return _resp(200, load_fixture("token.json"))
        if self.expire_token_once:
            self.expire_token_once = False
            return _resp(401, {"name": "AUTHENTICATION_FAILURE", "message": "FIXTURE: token expired"})
        if h.get("authorization") != "Bearer FIXTURE-ACCESS-TOKEN":
            return _resp(401, {"name": "AUTHENTICATION_FAILURE", "message": "FIXTURE"})
        if path == "/v2/checkout/orders" and req.method == "POST":
            return self._create_order(json.loads(req.body or b"{}"))
        m = re.fullmatch(r"/v2/checkout/orders/([^/]+)/capture", path)
        if m and req.method == "POST":
            return self._capture_order(m.group(1))
        if path == "/v1/notifications/verify-webhook-signature":
            if self.verify_unavailable:
                raise TransportTimeout("fixture verify unavailable")
            ok = json.loads(req.body)["transmission_sig"] == VALID_SIG
            return _resp(200, load_fixture("verify_webhook.json",
                                           verification_status="SUCCESS" if ok else "FAILURE"))
        m = re.fullmatch(r"/v2/payments/captures/([^/]+)", path)
        if m and req.method == "GET":
            cap = m.group(1)
            if cap not in self.captures:
                return _resp(404, load_fixture("error_not_found.json"))
            return _resp(200, load_fixture("capture.json", capture_id=cap, value=to_value(self.captures[cap])))
        m = re.fullmatch(r"/v2/payments/captures/([^/]+)/refund", path)
        if m and req.method == "POST":
            return self._refund(m.group(1), req, h)
        m = re.fullmatch(r"/v2/payments/refunds/([^/]+)", path)
        if m and req.method == "GET":
            if self.read_unavailable:
                raise TransportTimeout("fixture read unavailable")
            if m.group(1) not in self.refunds:
                return _resp(404, load_fixture("error_not_found.json"))
            return _resp(200, self._refund_json(m.group(1)))
        return _resp(404, load_fixture("error_not_found.json"))

    def _refund_json(self, refund_id: str) -> dict:
        r = self.refunds[refund_id]
        name = "refund_pending.json" if r["status"] == "PENDING" else "refund_completed.json"
        d = load_fixture(name, refund_id=refund_id, value=to_value(r["minor"]), custom_id=r["custom_id"])
        d["status"] = r["status"]
        return d

    # -- Orders v2 seeding (the shapes are the documented ones, UNVERIFIED against the real sandbox) ----------
    def _create_order(self, body: dict) -> HttpResponse:
        try:
            unit = body["purchase_units"][0]["amount"]
            minor = to_minor(unit["value"])
            if body.get("intent") != "CAPTURE" or unit["currency_code"] != "USD" or minor <= 0:
                raise ValueError("BAD_ORDER")
        except (KeyError, IndexError, TypeError, ValueError):
            return _resp(400, {"name": "INVALID_REQUEST", "message": "FIXTURE: bad order body", "details": [{"issue": "INVALID_REQUEST"}]})
        oid = f"FIXORDER-{len(self.orders) + 1:03d}"
        self.orders[oid] = {"minor": minor, "status": "PAYER_ACTION_REQUIRED", "capture_id": None}
        return _resp(201, {"id": oid, "status": "PAYER_ACTION_REQUIRED",
                           "links": [{"href": "https://fixture.invalid/approve/" + oid, "rel": "payer-action", "method": "GET"}]})

    def approve_order(self, order_id: str) -> None:
        """Test helper: the sandbox buyer approving in a browser."""
        self.orders[order_id]["status"] = "APPROVED"

    def _capture_order(self, order_id: str) -> HttpResponse:
        o = self.orders.get(order_id)
        if o is None:
            return _resp(404, load_fixture("error_not_found.json"))
        if o["status"] != "APPROVED":
            return _resp(422, {"name": "UNPROCESSABLE_ENTITY", "message": "FIXTURE: order not approved",
                               "details": [{"issue": "ORDER_NOT_APPROVED"}]})
        if o["capture_id"] is None:
            o["capture_id"] = f"FIXCAP-LIVE-{order_id[-3:]}"
            self.captures[o["capture_id"]] = o["minor"]
        o["status"] = "COMPLETED"
        return _resp(201, {"id": order_id, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
            {"id": o["capture_id"], "status": "COMPLETED", "amount": {"currency_code": "USD", "value": to_value(o["minor"])}}]}}]})

    def _refund_answer(self, refund_id: str, h: dict) -> dict:
        """What PayPal documents: the default answer is minimal (id, status, links); the full refund
        resource only comes back when the request says `Prefer: return=representation`."""
        full = self._refund_json(refund_id)
        if "return=representation" in h.get("prefer", "").replace(" ", ""):
            return full
        return {"id": full["id"], "status": full["status"], "links": full["links"]}

    def _refund(self, capture_id: str, req: HttpRequest, h: dict) -> HttpResponse:
        self.refund_posts += 1
        if self.timeout_before_accept:
            self.timeout_before_accept = False
            raise TransportTimeout("fixture: no response, nothing accepted")
        if self.server_error_before_accept:
            self.server_error_before_accept = False
            return _resp(503, {"name": "SERVICE_UNAVAILABLE", "message": "FIXTURE"})
        rid = h.get("paypal-request-id")
        body = json.loads(req.body or b"{}")
        self.refund_requests.append({"capture_id": capture_id, "headers": dict(h), "body": body})
        body_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if rid and rid in self.by_request_id:
            prior_hash, prior = self.by_request_id[rid]
            if prior_hash == body_hash:
                return prior              # same Request-Id, same body: same answer, no new refund
            return _resp(422, load_fixture("error_over_refund.json"))  # UNVERIFIED conflict behavior
        if capture_id not in self.captures:
            return _resp(404, load_fixture("error_not_found.json"))
        minor = to_minor(body["amount"]["value"])
        if minor <= 0 or self.refunded_minor(capture_id) + minor > self.captures[capture_id]:
            return _resp(422, load_fixture("error_over_refund.json"))
        refund_id = "FIXREF-" + hashlib.sha256(f"{capture_id}:{len(self.refunds)}".encode()).hexdigest()[:10].upper()
        self.refunds[refund_id] = {"capture_id": capture_id, "minor": minor,
                                   "custom_id": body.get("custom_id", ""),
                                   "status": self.next_refund_status}
        self.refunds_created += 1
        resp = _resp(201, self._refund_answer(refund_id, h))
        if rid:
            self.by_request_id[rid] = (body_hash, resp)
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise TransportTimeout("fixture: accepted, response lost")
        if self.crash_after_accept:
            self.crash_after_accept = False
            from approval_receipt import SimulatedCrash
            raise SimulatedCrash()
        if self.malformed_after_accept:
            self.malformed_after_accept = False
            return HttpResponse(201, {"Content-Type": "application/json"}, b"{not json")
        if self.odd_status_after_accept:
            self.odd_status_after_accept = False
            d = self._refund_json(refund_id)
            d["status"] = "SOMETHING_NEW"
            return _resp(201, d)
        if self.conflict_after_accept:
            self.conflict_after_accept = False
            return _resp(409, {"name": "RESOURCE_CONFLICT", "message": "FIXTURE"})
        if self.redirect_after_accept:
            self.redirect_after_accept = False
            return HttpResponse(302, {"Location": "https://evil.example/x"}, b"")
        if self.server_error_after_accept:
            self.server_error_after_accept = False
            return _resp(502, {"name": "BAD_GATEWAY", "message": "FIXTURE"})
        return resp
