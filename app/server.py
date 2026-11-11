"""HTTP layer. `App.handle()` is a pure function of (method, path, headers, body), so the whole
app is tested without opening a socket. `serve()` wraps it in the standard library server."""
from __future__ import annotations
import json
import mimetypes
import os
import re
import threading
import time
from dataclasses import dataclass, field
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from approval_receipt.engine import ApprovalError, IdempotencyConflict

from . import REPO_ROOT
from .agent import MessageRejected
from .arena import Arena
from .arena_reader import Reader, build_reader
from .auth import LoginError
from .demo import Demo
from .desk import Desk
from .service import Service, ResendRefused
from .strings import all_strings
from . import views

STATIC = os.path.join(REPO_ROOT, "static")
MAX_BODY = 16 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        return json.loads(self.body.decode())


def _json(status: int, obj, extra: dict | None = None) -> Response:
    h = {"Content-Type": "application/json", "Cache-Control": "no-store", **(extra or {})}
    return Response(status, h, json.dumps(obj).encode())


def _err(status: int, code: str) -> Response:
    return _json(status, {"error": code})


class App:
    def __init__(self, svc: Service, arena_reader: Reader | None = None):
        self.svc = svc
        self._arena_reader, self._arena = arena_reader, None
        self.demo = Demo(svc) if svc.mode == "fixture" else None
        self.desk = Desk(svc)
        self._lock = threading.RLock()   # one DECISION at a time: the core is single-process
        self._arena_lock = threading.Lock()
        self._tl = threading.local()     # the model's reading of the request this thread is handling (taken before the lock)

    def arena(self) -> Arena:
        """Built on first use so the main app starts exactly as before. The reader comes from
        ARENA_READER (rules by default); a bad value stops the arena with a clear message."""
        if self._arena is None:
            with self._arena_lock:
                if self._arena is None:
                    self._arena = Arena(self.svc, self._arena_reader or build_reader())
        return self._arena

    # ---- entry point -----------------------------------------------------
    def handle(self, method: str, target: str, headers: dict | None = None, body: bytes = b"") -> Response:
        method, hl, body = method.upper(), {k.lower(): v for k, v in (headers or {}).items()}, body or b""
        # A model call (the extractor, the consent reader, the arena's reader) can take many seconds. It runs HERE,
        # before the lock, so one slow provider call no longer stops every other request. The reading decides nothing: the
        # decision is made below, under the lock, from the same inputs as before.
        self._tl.pre = self._prefetch(method, target, hl, body)
        try:
            with self._lock:
                r = self._route(method, target, hl, body)
        finally:
            self._tl.pre = None
        r.headers.update({"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                          "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
        return r

    def _prefetch(self, method: str, target: str, h: dict, body: bytes):
        """(kind, reading) for the three routes that call a model, or None. Anything that is not a clean case (a bad body, a
        refused message, a throttled order) returns None, and the locked path then refuses it exactly as before."""
        if method != "POST" or len(body) > MAX_BODY:
            return None
        path = urlsplit(target).path
        if path not in ("/api/buyer/messages", "/api/desk/send", "/api/arena/attempt"):
            return None
        try:
            data = json.loads(body) if body.strip() else {}
            if not isinstance(data, dict):
                return None
            svc = self.svc
            if path == "/api/buyer/messages":
                return ("buyer", svc.prefetch(str(data.get("text", "")), str(data.get("order", "")), str(data.get("cap", ""))))
            if path == "/api/arena/attempt":
                return ("arena", self.arena().prefetch(str(data.get("text", "")), str(data.get("order", ""))))
            sid = self._sid(h)                                           # the desk: an owner session and CSRF first
            if svc.auth.session(sid) is None or not svc.auth.csrf_ok(sid, h.get("x-csrf-token")):
                return None
            plan = self.desk.plan(data.get("scenario"), data.get("order"), data.get("text"))   # a scenario names its own text/order/readers
            if svc.orders.get(plan.order) is None:
                return None
            return ("desk", (svc.prefetch(plan.text, plan.order, svc.buyer_capability(plan.order),
                                          reader=plan.reader, extractor=plan.extractor), plan))
        except Exception:
            return None

    def _pre(self, kind: str):
        got = getattr(self._tl, "pre", None)
        return got[1] if got and got[0] == kind else None

    def _route(self, method: str, target: str, h: dict, body: bytes) -> Response:
        parts = urlsplit(target)
        path, query = parts.path, parse_qs(parts.query)
        if len(body) > MAX_BODY:
            return _err(413, "BODY_TOO_LARGE")
        if path == "/favicon.ico" and method == "GET":
            return Response(204, {"Cache-Control": "max-age=86400"})       # no icon: quiet 204 instead of a 404 in the console
        if not path.startswith("/api/"):
            return self._static(path) if method == "GET" else _err(405, "METHOD_NOT_ALLOWED")
        try:
            return self._api(method, path, query, h, body)
        except MessageRejected as e:
            return _err({"PROOF_REQUIRED": 403, "RATE_LIMITED": 429}.get(e.code, 400), e.code)
        except ApprovalError as e:
            return _err(409, e.code)
        except IdempotencyConflict:
            return _err(409, "IDEMPOTENCY_CONFLICT")
        except ResendRefused as e:
            return _err(409, e.code if e.code.startswith("RESEND_") else "RESEND_" + e.code)
        except (ValueError, KeyError, TypeError):
            return _err(400, "BAD_REQUEST")

    # ---- static ----------------------------------------------------------
    def _static(self, path: str) -> Response:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC, name))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return _err(404, "NOT_FOUND")
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        return Response(200, {"Content-Type": ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""),
                              "Cache-Control": "no-cache"}, data)

    # ---- api ---------------------------------------------------------------
    def _sid(self, h: dict) -> str | None:
        c = cookies.SimpleCookie()
        try:
            c.load(h.get("cookie", ""))
        except cookies.CookieError:
            return None
        return c["sid"].value if "sid" in c else None

    def _api(self, method: str, path: str, q: dict, h: dict, body: bytes) -> Response:
        svc = self.svc
        data = json.loads(body) if body.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("BAD_BODY")

        # public: copy, buyer, webhook
        if path == "/api/strings" and method == "GET":
            return _json(200, all_strings())
        if path == "/api/mode" and method == "GET":
            return _json(200, {"mode": svc.mode})
        if path == "/api/buyer/messages" and method == "POST":
            req = svc.message(str(data.get("text", "")), str(data.get("order", "")), str(data.get("cap", "")),
                              prefetched=self._pre("buyer"))
            return _json(201, {"token": req["token"], "status": views.buyer_view(svc, req)})
        m = re.fullmatch(r"/api/buyer/requests/([A-Za-z0-9_\-]{8,64})", path)
        if m and method == "GET":
            req = next((r for r in svc.store.requests.values() if r["token"] == m.group(1)), None)
            return _json(200, views.buyer_view(svc, req)) if req else _err(404, "NOT_FOUND")
        if path == "/api/demo/links" and method == "GET" and self.demo is not None:
            return _json(200, {"links": self.demo.links()})     # demo only: stands in for the order email
        if path == "/api/webhooks/paypal" and method == "POST":
            status, result = svc.webhook(h, body)
            return _json(status, result)
        if path == "/api/login" and method == "POST":
            try:
                sid = svc.auth.login(str(data.get("passcode", "")))
            except LoginError as e:
                return _err(429 if e.code == "LOCKED" else 401, e.code)
            return _json(200, {"csrf": svc.auth.csrf(sid)},
                         {"Set-Cookie": f"sid={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"})

        # public: the Injection Arena and the read side of the policy (a simulation, its own receipt chain)
        r = self._arena_routes(method, path, q, data)
        if r is not None:
            return r

        # everything below needs a session
        sid = self._sid(h)
        session = svc.auth.session(sid)
        if session is None:
            return _err(401, "SESSION_REQUIRED")
        if path == "/api/session" and method == "GET":
            return _json(200, {"csrf": svc.auth.csrf(sid), "approver": session.approver_id, "mode": svc.mode})
        if path == "/api/logout" and method == "POST":
            svc.auth.logout(sid)
            return _json(200, {"ok": True}, {"Set-Cookie": "sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        if method == "POST" and not svc.auth.csrf_ok(sid, h.get("x-csrf-token")):
            return _err(403, "CSRF_INVALID")

        if path == "/api/policy/preview" and method == "POST":
            return _json(200, svc.policies.preview(str(data.get("text", ""))))
        if path == "/api/policy/save" and method == "POST":
            res = svc.save_policy(str(data.get("text", "")), session.approver_id,
                                  data.get("base_version"), data.get("result_hash"))
            stale = any(e["code"] in ("POLICY_CHANGED", "PREVIEW_MISMATCH") for e in res.get("errors", []))
            return _json(200 if res["ok"] else (409 if stale else 422), res)

        # the Refund Desk: one screen over the same service (owner session, CSRF on every POST)
        if path == "/api/desk/state" and method == "GET":
            return _json(200, self.desk.state())
        if path == "/api/desk/send" and method == "POST":
            got = self._pre("desk")
            return _json(201, self.desk.send(data.get("scenario"), data.get("order"), data.get("text"),
                                             prefetched=got[0] if got else None, plan=got[1] if got else None))
        m = re.fullmatch(r"/api/desk/readback/(REQ-\d{4,})", path)
        if m and method == "POST":
            return _json(200, self.desk.readback(m.group(1)))

        if path == "/api/inbox" and method == "GET":
            return _json(200, views.inbox_view(svc))
        m = re.fullmatch(r"/api/requests/(REQ-\d{4,})", path)
        if m and method == "GET":
            req = svc.store.requests.get(m.group(1))
            return _json(200, views.proposal_view(svc, req, detail=True)) if req else _err(404, "NOT_FOUND")
        m = re.fullmatch(r"/api/requests/(REQ-\d{4,})/(approve|deny|reconcile|recheck|retry)", path)
        if m and method == "POST":
            req = svc.store.requests.get(m.group(1))
            if not req:
                return _err(404, "NOT_FOUND")
            action = m.group(2)
            # The target comes ONLY from the signed records (never from the request file's own pointer) ...
            iid, err = views.signed_target(svc, req)
            if err:
                return _err(404 if err == "NOT_FOUND" else 409, err)
            if action in ("approve", "deny"):
                # ... and the owner's action must name the exact intent and amount the card showed.
                amount = svc.ledger.latest_for_intent(iid)["intent"]["amount_minor"]
                if data.get("intent_id") != iid or type(data.get("amount_minor")) is not int or data["amount_minor"] != amount:
                    return _err(409, "STATE_MISMATCH")
            note = str(data.get("note", ""))[:280]
            if action == "approve":
                svc.approve(iid, session, str(data.get("nonce", "")), str(data.get("digest", "")), note)
            elif action == "deny":
                svc.deny(iid, session, note)
            elif action == "reconcile":
                svc.reconcile(iid)
            elif action == "recheck":
                svc.recheck(iid)
            else:
                svc.retry(iid)
            out = views.proposal_view(svc, req, detail=True)
            if action in ("approve", "deny"):
                out["next_waiting"] = views.next_waiting(svc, req["id"])      # one tap to the next request that waits for the owner
            return _json(200, out)
        m = re.fullmatch(r"/api/receipts/(I-\d{4,})", path)
        if m and method == "GET":
            v = views.receipt_view(svc, m.group(1))
            return _json(200, v) if v else _err(404, "NOT_FOUND")
        if path == "/api/poll" and method == "POST":
            touched = len(svc.poll())
            return _json(200, {"touched": touched,
                               "held": [{"intent_id": i, "reason": r} for i, r in svc.recovery_held]})   # left AUTHORIZED, not sent
        if path == "/api/verify" and method == "GET":
            tampered = os.path.join(svc.data_dir, "receipts.tampered.jsonl")
            use = tampered if q.get("copy") == ["tampered"] and os.path.exists(tampered) else None
            return _json(200, views.verify_view(svc, use))
        if path.startswith("/api/demo/") and method in ("GET", "POST"):
            if self.demo is None:
                return _err(404, "NOT_FOUND")
            return _json(200, self.demo.handle(path, data))
        return _err(404, "NOT_FOUND")


    def _arena_routes(self, method: str, path: str, q: dict, data: dict) -> Response | None:
        if not (path.startswith("/api/arena/") or path in ("/api/policy", "/api/policy/diff")):
            return None
        if path == "/api/policy" and method == "GET":
            return _json(200, {**self.svc.policies.state(), "enforced": self.svc.enforced_limits()})
        if path == "/api/policy/diff" and method == "GET":
            v = self.svc.policies.diff_versions(int(q["from"][0]), int(q["to"][0]))
            return _json(200, v) if v else _err(404, "NOT_FOUND")
        ar = self.arena()
        if path == "/api/arena/state" and method == "GET":
            return _json(200, ar.state())
        if path == "/api/arena/attempt" and method == "POST":
            preset = data.get("preset")
            return _json(201, ar.attempt(str(data.get("text", "")), str(data.get("order", "")),
                                         str(preset) if preset else None, prefetched=self._pre("arena")))
        m = re.fullmatch(r"/api/arena/(attempts|court)/(A-\d{4,})", path)
        if m and method == "GET":
            if m.group(1) == "court":
                v = ar.court(m.group(2))
            else:
                rec = ar.find_attempt(m.group(2))
                v = ar.attempt_view(rec) if rec else None
            return _json(200, v) if v else _err(404, "NOT_FOUND")
        m = re.fullmatch(r"/api/arena/receipts/(R-\d{6,})", path)
        if m and method == "GET":
            v = ar.receipt(m.group(1))
            return _json(200, v) if v else _err(404, "NOT_FOUND")
        if path == "/api/arena/verify" and method == "GET":
            return _json(200, ar.verify())
        return _err(404, "NOT_FOUND")


# ---- stdlib server wrapper -------------------------------------------------
def serve(app: App, host: str = "127.0.0.1", port: int = 8765, maintain_every: float = 60.0) -> None:
    def maintain():     # orphan recovery, read-back of unknown payments, pending-refund polling
        while True:
            time.sleep(maintain_every)
            try:
                with app._lock:
                    app.svc.poll()
            except Exception as e:          # keep the loop alive; never hide that it failed
                print("maintenance error:", type(e).__name__, e)
    threading.Thread(target=maintain, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        def _go(self, method: str):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, MAX_BODY + 1))
            r = app.handle(method, self.path, dict(self.headers), body)
            self.send_response(r.status)
            for k, v in r.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(r.body)))
            self.end_headers()
            self.wfile.write(r.body)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, *a):   # keep the console clean; no request bodies logged
            pass

    ThreadingHTTPServer((host, port), H).serve_forever()
