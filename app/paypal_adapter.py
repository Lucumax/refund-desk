"""PayPal REST client and the core PaymentAdapter built on it.

Refunds go through REST (POST /v2/payments/captures/{capture_id}/refund). The documented
PayPal agent toolkit does list refund tools (create_refund, get_refund), but this build calls
REST directly so that one small adapter is the only code that can move money, and it holds the
credentials. Credentials are read from the
environment variables named below, never stored in code or files.

What a refund request carries: header PayPal-Request-Id (our idempotency key, saved before the first
send and reused on every re-send), header Prefer: return=representation (so the answer is the full
refund resource), and in the body the amount and custom_id (our intent id, for reconciliation). The
invoice_id field is NOT sent. Proof that a refund happened is read from the refund resource
(GET /v2/payments/refunds/{id}); no capture-level cumulative field is relied on.

UNVERIFIED items are marked in comments and listed in NOT_DONE.md."""
from __future__ import annotations
import base64
import json
import os
from typing import Callable

from datetime import datetime, timedelta, timezone

from approval_receipt import PaymentAdapter, PaymentRejected, PaymentTimeout, LookupResult

from .money import to_value
from .transport import HttpRequest, Transport, TransportTimeout

ENV_CLIENT_ID = "PAYPAL_CLIENT_ID"
ENV_CLIENT_SECRET = "PAYPAL_CLIENT_SECRET"
SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
KNOWN_STATUSES = ("COMPLETED", "PENDING", "FAILED", "CANCELLED")   # the refund schema's documented list
# HTTP statuses that CAN be a definite refusal, and only on the FIRST send of a request id (see
# PayPalAdapter.pay). After a re-send, or once a refund id is known, none of them proves anything.
DEFINITE_REFUSAL = (400, 401, 403, 404, 422)
MAX_CUSTOM_ID = 127
# The application's own conservative setting, not a PayPal figure: how long we re-send a repeated
# request id by ourselves. PayPal's behavior on repeated ids is verified in the sandbox run.
# After this, an unresolved refund is never re-sent automatically.
REPLAY_WINDOW = timedelta(hours=6)


def refund_mismatches(d, ref: str, amount_minor: int | None, currency: str | None) -> list[str]:
    """Which required facts of a refund resource do NOT match what we hold for it. Missing provider data is a
    mismatch, never a pass. `amount_minor` / `currency` None means we hold no terms to compare (only the id is checked)."""
    if not isinstance(d, dict):
        return ["resource"]
    amount = d.get("amount") if isinstance(d.get("amount"), dict) else {}
    bad = []
    if d.get("id") != ref:
        bad.append("refund_id")
    if d.get("status") not in KNOWN_STATUSES:
        bad.append("status_known")
    if amount_minor is not None and amount.get("value") != to_value(amount_minor):
        bad.append("amount")
    if currency is not None and amount.get("currency_code") != currency:
        bad.append("currency")
    return bad


class PayPalHttpError(Exception):
    def __init__(self, status: int, name: str = "", issue: str = ""):
        super().__init__(f"{status} {name} {issue}".strip())
        self.status, self.name, self.issue = status, name, issue


def env_credentials() -> tuple[str, str]:
    cid, sec = os.environ.get(ENV_CLIENT_ID), os.environ.get(ENV_CLIENT_SECRET)
    if not cid or not sec:
        raise RuntimeError(f"set {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET} (sandbox app only)")
    return cid, sec


class PayPalClient:
    def __init__(self, transport: Transport,
                 credentials: Callable[[], tuple[str, str]] = lambda: ("fixture-id", "fixture-secret"),
                 base_url: str = SANDBOX_BASE, timeout: float = 10.0):
        self.transport, self.credentials, self.base_url, self.timeout = transport, credentials, base_url, timeout
        self._token: str | None = None

    def _fetch_token(self) -> str:
        cid, sec = self.credentials()
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        resp = self.transport.send(HttpRequest(
            "POST", self.base_url + "/v1/oauth2/token",
            {"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
            b"grant_type=client_credentials", self.timeout))
        if resp.status != 200:
            raise PayPalHttpError(resp.status, "TOKEN_REFUSED")
        self._token = resp.json()["access_token"]
        return self._token

    def call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None,
             raw: bytes | None = None):
        """One authenticated call. On a 401 the token is refreshed once and the SAME request
        (same headers, so the same PayPal-Request-Id) is sent again. `raw` sends exact bytes."""
        for attempt in (0, 1):
            token = self._token or self._fetch_token()
            h = {"Authorization": "Bearer " + token, "Content-Type": "application/json", **(headers or {})}
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            resp = self.transport.send(HttpRequest(method, self.base_url + path, h, data, self.timeout))
            if resp.status == 401 and attempt == 0:
                self._token = None
                continue
            return resp
        raise AssertionError("unreachable")

    @staticmethod
    def err(resp) -> PayPalHttpError:
        try:
            d = resp.json()
            issue = (d.get("details") or [{}])[0].get("issue", "")
            return PayPalHttpError(resp.status, d.get("name", ""), issue)
        except Exception:
            return PayPalHttpError(resp.status)

    def get_capture(self, capture_id: str) -> dict:
        r = self.call("GET", f"/v2/payments/captures/{capture_id}")
        if r.status != 200:
            raise self.err(r)
        return r.json()

    def get_refund(self, refund_id: str) -> dict:
        r = self.call("GET", f"/v2/payments/refunds/{refund_id}")
        if r.status != 200:
            raise self.err(r)
        return r.json()

    def refund(self, capture_id: str, amount_minor: int, currency: str, request_id: str,
               custom_id: str):
        """Returns the raw response. Header PayPal-Request-Id carries our idempotency key; `Prefer:
        return=representation` asks for the full refund resource back (the default answer is minimal);
        custom_id carries our intent id. The application sends a repeat-safe request id; PayPal's
        behavior on repeated ids is verified in the sandbox run (UNVERIFIED until then)."""
        body = {"amount": {"value": to_value(amount_minor), "currency_code": currency},
                "custom_id": custom_id[:MAX_CUSTOM_ID]}
        return self.call("POST", f"/v2/payments/captures/{capture_id}/refund", body,
                         {"PayPal-Request-Id": request_id, "Prefer": "return=representation"})

    def verify_webhook(self, headers: dict, body: bytes, webhook_id: str) -> bool:
        """UNVERIFIED endpoint and body keys: POST /v1/notifications/verify-webhook-signature.
        PayPal verifies the event exactly as it was sent, so the received bytes are spliced into the
        request as they are and never parsed and re-serialized. (Webhooks are off in this build.)"""
        h = {k.lower(): v for k, v in headers.items()}
        json.loads(body)      # not a JSON event: ValueError, handled by the caller; the parsed copy is dropped
        head = json.dumps({"auth_algo": h.get("paypal-auth-algo", ""), "cert_url": h.get("paypal-cert-url", ""),
                           "transmission_id": h.get("paypal-transmission-id", ""),
                           "transmission_sig": h.get("paypal-transmission-sig", ""),
                           "transmission_time": h.get("paypal-transmission-time", ""),
                           "webhook_id": webhook_id})
        raw = (head[:-1] + ', "webhook_event": ' + body.decode("utf-8") + "}").encode("utf-8")
        r = self.call("POST", "/v1/notifications/verify-webhook-signature", raw=raw)
        if r.status != 200:
            raise self.err(r)
        return r.json().get("verification_status") == "SUCCESS"


class PayPalAdapter(PaymentAdapter):
    """Core PaymentAdapter. `payee` is the capture ID being refunded. The core's idempotency
    key is sent as PayPal-Request-Id and is never regenerated, so a retry reuses it.

    Outcome classification (money safety), by dispatch history AND response:
    - FIRST send of a request id: an explicit client-error answer (400, 401, 403, 404, 422) with a
      parseable PayPal error body is a definite refusal (PaymentRejected): nothing was created.
    - RE-SEND of a request id that may already have reached PayPal (two or more sends in the signed
      history, or a refund id already known): NO answer is definite, a 4xx or a refused login
      included. A 422 "refund exceeds the remaining amount" can simply mean the first send worked. The result stays UNKNOWN
      and a lookup by refund id, or a person, resolves it. A failed retry never resolves an earlier
      unknown send.
    - Everything else (timeouts, 5xx, 409, 429, redirects, a 2xx with a broken or unrecognised body)
      raises PaymentTimeout, which the core records as UNKNOWN and which keeps the money reserved."""

    def __init__(self, client: PayPalClient, index: dict | None = None,
                 on_change: Callable[[], None] = lambda: None,
                 dispatch_time: Callable[[str], datetime | None] = lambda key: None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 replay_window: timedelta = REPLAY_WINDOW,
                 custom_id_for_key: Callable[[str], str] = lambda key: key,
                 dispatch_count: Callable[[str], int] = lambda key: 0,
                 terms_for_key: Callable[[str], tuple | None] = lambda key: None):
        self.client = client
        self.index = index if index is not None else {}   # idempotency key -> refund id (a hint only)
        self.statuses: dict[str, str] = {}                # refund id -> last status seen
        self._on_change = on_change
        self.dispatch_time, self.clock, self.replay_window = dispatch_time, clock, replay_window
        self.custom_id_for_key, self.dispatch_count = custom_id_for_key, dispatch_count
        self.terms_for_key = terms_for_key                # key -> (amount_minor, currency) from the SIGNED receipt, or None
        self.last_error: dict[str, str] = {}              # key -> error code, for the receipt view
        self._sent: dict[str, int] = {}                   # sends by this process (the signed history covers restarts)

    def remember(self, key: str, refund_id: str, status: str | None = None) -> None:
        self.index[key] = refund_id
        if status:
            self.statuses[refund_id] = status
        self._on_change()

    def is_resend(self, key: str) -> bool:
        """True when this request id may already have reached PayPal: a second send, or a refund id
        is already known for it. Called while a send is in flight, so the signed count includes it."""
        return max(self.dispatch_count(key), self._sent.get(key, 0)) > 1 or key in self.index

    def pay(self, payee: str, amount_minor: int, currency: str, idempotency_key: str) -> str:
        self._sent[idempotency_key] = self._sent.get(idempotency_key, 0) + 1
        resend = self.is_resend(idempotency_key)
        try:
            return self._pay(payee, amount_minor, currency, idempotency_key, resend)
        except PaymentRejected as e:
            if resend:
                # Whatever the reason (a refused token, a 4xx), nothing a re-send meets can prove the EARLIER send
                # did not work. The record stays UNKNOWN and keeps its reservation.
                self.last_error.setdefault(idempotency_key, "RESEND_REFUSED")
                raise PaymentTimeout("a re-send was refused; the earlier send is still unresolved: " + str(e)) from e
            raise

    def _pay(self, payee: str, amount_minor: int, currency: str, idempotency_key: str, resend: bool) -> str:
        try:
            resp = self.client.refund(payee, amount_minor, currency, idempotency_key,
                                      self.custom_id_for_key(idempotency_key))
        except TransportTimeout as e:
            raise PaymentTimeout("no response") from e       # outcome unknown: reconcile before retry
        except PayPalHttpError as e:
            # A refused login means THIS attempt sent no refund request. That is a definite failure only on the first
            # send; on a re-send the earlier send may have reached PayPal, so it is UNKNOWN. Classified here at its origin
            # (Xin Liang Chia, 009552e) and, as a second layer, again in pay(), which turns any re-send rejection into UNKNOWN.
            if e.name == "TOKEN_REFUSED" and not resend:
                raise PaymentRejected("token refused") from e
            if e.name == "TOKEN_REFUSED":
                self.last_error.setdefault(idempotency_key, "RESEND_REFUSED")
            raise PaymentTimeout(str(e)) from e
        if 200 <= resp.status < 300:
            try:
                d = resp.json()
            except ValueError:
                d = None
            if isinstance(d, dict) and d.get("id") and d.get("status") in KNOWN_STATUSES:
                if not isinstance(d.get("amount"), dict) or refund_mismatches(d, str(d.get("id")), amount_minor, currency):
                    # A refund answer that carries no amount, or one that disagrees with what we sent, is not a matched
                    # refund. Nothing is taken from it as a result: the outcome stays UNKNOWN. Without an amount the id is
                    # kept as a hint, so the owner's re-check can read the resource and validate it (amount, currency and
                    # our custom_id) before anything is treated as settled.
                    if not isinstance(d.get("amount"), dict):
                        self.index[idempotency_key] = str(d["id"])
                        self._on_change()
                    self.last_error[idempotency_key] = ("RESPONSE_INCOMPLETE" if not isinstance(d.get("amount"), dict)
                                                         else "RESPONSE_DOES_NOT_MATCH")
                    raise PaymentTimeout("the answer is incomplete or does not match the request")      # UNKNOWN: a person looks
                # A FAILED or CANCELLED status here still means PayPal created the refund record:
                # keep its id; the signed provider-status receipt then releases the reservation.
                self.remember(idempotency_key, str(d["id"]), d["status"])
                return str(d["id"])
            self.last_error[idempotency_key] = "RESPONSE_UNREADABLE"
            raise PaymentTimeout("accepted-looking answer could not be read")   # UNKNOWN, never FAILED
        err = PayPalClient.err(resp)
        self.last_error[idempotency_key] = err.issue or err.name or str(err.status)
        if resend:
            raise PaymentTimeout(str(err))                    # a re-send's refusal proves nothing about the first send
        if resp.status in DEFINITE_REFUSAL and err.name:
            raise PaymentRejected(str(err))                   # first send, explicit refusal with a PayPal error body
        raise PaymentTimeout(str(err))                        # 3xx, 409, 429, 5xx, odd bodies: unknown

    # What a look-up can honestly say about a key. The core's ABSENT means "the provider DEFINITIVELY has no
    # record", and this build can never say that: it cannot search PayPal by request id. So lookup() never returns
    # ABSENT. "No refund id is known to us" is a fact about OUR records, named NO_KNOWN_ID, and no PayPal read happens.
    KNOWN_ID, NO_KNOWN_ID, WINDOW_PASSED = "KNOWN_ID", "NO_KNOWN_ID", "WINDOW_PASSED"

    def lookup_basis(self, idempotency_key: str) -> str:
        """KNOWN_ID: we hold a refund id, so PayPal's own record can be read. NO_KNOWN_ID: no id and the first
        dispatch is inside the replay window (a same-key re-send is not ruled out by our own setting; PayPal's
        retention of request ids is UNVERIFIED). WINDOW_PASSED: no id and the window is over, or the first dispatch
        time is unknown: nothing more is attempted automatically. Reads nothing from PayPal."""
        if self.index.get(idempotency_key):
            return self.KNOWN_ID
        t = self.dispatch_time(idempotency_key)
        if t is not None and self.clock() - t <= self.replay_window:
            return self.NO_KNOWN_ID
        return self.WINDOW_PASSED

    def lookup(self, idempotency_key: str) -> LookupResult:
        """Two answers only. FOUND: we hold the refund id and PayPal's refund resource (GET) returns it with a known
        status and our custom_id. INDETERMINATE: everything else (no id known, the read failed, the answer is
        unreadable or does not match, the window passed); the core then never re-dispatches. It never returns
        ABSENT: see lookup_basis for why."""
        ref = self.index.get(idempotency_key)
        if not ref:
            return LookupResult.indeterminate()               # no PayPal read happens on this branch
        try:
            d = self.client.get_refund(ref)
        except Exception:
            return LookupResult.indeterminate()
        status = d.get("status") if isinstance(d, dict) else None
        terms = self.terms_for_key(idempotency_key)
        if refund_mismatches(d, ref, *(terms or (None, None))):     # the indexed id is a hint: it must be THIS refund
            return LookupResult.indeterminate()
        if d.get("custom_id") != self.custom_id_for_key(idempotency_key)[:MAX_CUSTOM_ID]:
            # Amount and currency cannot tell two equal refunds apart. The only thing that ties a refund resource to THIS
            # operation is the custom_id we sent (our intent id): a missing, empty or different one is not a match.
            # Whether PayPal echoes custom_id is UNVERIFIED; if it does not, the hold stays and a person looks.
            return LookupResult.indeterminate()
        self.statuses[ref] = status
        return LookupResult.found(ref)
