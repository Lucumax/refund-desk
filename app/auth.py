"""Approver sessions for the demo. A link alone grants nothing: the approver signs in with a
passcode (printed on the server console at start, or set by APP_PASSCODE), receives an
HttpOnly cookie, and every state-changing call must carry the CSRF token for that session.
This is demo-grade authentication, not a production identity system (see NOT_DONE.md)."""
from __future__ import annotations
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Callable

from approval_receipt import ApproverSession

APPROVER_ID = "merchant-owner"
SESSION_TTL = timedelta(hours=8)
MAX_FAILS, LOCK_FOR = 5, timedelta(seconds=60)


class LoginError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SessionManager:
    def __init__(self, passcode: str, clock: Callable[[], datetime]):
        if not passcode:
            raise ValueError("passcode required")
        self._passcode, self.clock = passcode, clock
        self._secret = secrets.token_bytes(32)
        self._sessions: dict[str, tuple[ApproverSession, datetime]] = {}
        self._fails, self._locked_until = 0, None

    def login(self, passcode: str) -> str:
        now = self.clock()
        if self._locked_until and now < self._locked_until:
            raise LoginError("LOCKED")
        if not hmac.compare_digest((passcode or "").encode(), self._passcode.encode()):
            self._fails += 1
            if self._fails >= MAX_FAILS:
                self._fails, self._locked_until = 0, now + LOCK_FOR
            raise LoginError("BAD_PASSCODE")
        self._fails, self._locked_until = 0, None
        sid = secrets.token_urlsafe(24)
        self._sessions[sid] = (ApproverSession(APPROVER_ID, hashlib.sha256(sid.encode()).hexdigest()[:16]),
                               now + SESSION_TTL)
        return sid

    def session(self, sid: str | None) -> ApproverSession | None:
        entry = self._sessions.get(sid or "")
        if not entry:
            return None
        if self.clock() >= entry[1]:
            del self._sessions[sid]
            return None
        return entry[0]

    def csrf(self, sid: str) -> str:
        return hmac.new(self._secret, sid.encode(), hashlib.sha256).hexdigest()

    def csrf_ok(self, sid: str | None, token: str | None) -> bool:
        return bool(sid and token) and hmac.compare_digest(self.csrf(sid), token)

    def logout(self, sid: str | None) -> None:
        self._sessions.pop(sid or "", None)
