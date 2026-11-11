"""Injectable HTTP transport. Everything that talks to PayPal goes through this interface,
so tests and the demo run on recorded fixtures with no network at all."""
from __future__ import annotations
import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

SANDBOX_HOST = "api-m.sandbox.paypal.com"


class TransportTimeout(Exception):
    """No usable response (timeout or dropped connection). The request may or may not
    have been processed by the other side."""


class HostNotAllowed(Exception):
    """The transport refuses any host that is not on its allow list."""


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict
    body: bytes | None = None
    timeout: float = 10.0


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        return json.loads(self.body.decode("utf-8")) if self.body else {}


class Transport(ABC):
    @abstractmethod
    def send(self, req: HttpRequest) -> HttpResponse:
        """Return a response for any HTTP status. Raise TransportTimeout when there is none."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the Authorization header must not be forwarded anywhere the
    host allow list did not check. A 3xx comes back to the caller as an ordinary response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def no_redirect_opener(*extra_handlers) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(*extra_handlers, _NoRedirect)


class UrllibTransport(Transport):
    """Real network transport. Used ONLY by scripts/live_sandbox_run.py or `--mode sandbox`.
    Never imported by tests or the default demo. Refuses every host except the PayPal
    sandbox host, so a mistaken URL cannot reach a live endpoint."""

    def __init__(self, allowed_hosts: tuple[str, ...] = (SANDBOX_HOST,), opener=None):
        self.allowed_hosts = allowed_hosts
        self._opener = opener or no_redirect_opener()   # redirects disabled; opener is injectable for tests

    def send(self, req: HttpRequest) -> HttpResponse:
        parts = urllib.parse.urlsplit(req.url)
        if parts.scheme != "https" or parts.hostname not in self.allowed_hosts:
            raise HostNotAllowed(parts.hostname)
        r = urllib.request.Request(req.url, data=req.body, method=req.method, headers=req.headers)
        try:
            with self._opener.open(r, timeout=req.timeout) as resp:  # host checked above, redirects off
                return HttpResponse(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as e:
            return HttpResponse(e.code, dict(e.headers or {}), e.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TransportTimeout(str(e)) from e
