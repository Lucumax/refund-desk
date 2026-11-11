"""Model-backed text completion for ModelExtractor. NOT used by tests or the default demo, and
NOT exercised in this build (no network was allowed): treat as untested until you run it with
your own key. The model only reads the buyer message and returns JSON facts plus a one sentence
summary; it never decides money. Keys come from environment variable NAMES below, never stored.

Instructions and buyer text travel SEPARATELY: the fixed instructions go in the provider's system
field, the buyer's words alone go in the user message (fenced by a random boundary token, see
app/extractor.py). They are never joined into one string.

APP_MODEL_PROVIDER = anthropic (default) or openai
anthropic: ANTHROPIC_API_KEY, APP_MODEL_ID     openai: OPENAI_API_KEY, APP_MODEL_ID
Anthropic request (2026-11-01): no `temperature` (current models return HTTP 400 for it), max_tokens 4000, and a reply whose
stop_reason is `max_tokens` or `refusal` is rejected. Checked offline only, against a fake opener; no live call has been made."""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.request

from .model_deadline import with_deadline
from .transport import no_redirect_opener

ENV_PROVIDER, ENV_MODEL = "APP_MODEL_PROVIDER", "APP_MODEL_ID"
ENV_DEADLINE = "APP_MODEL_DEADLINE_SECONDS"          # wall-clock limit for ONE model call (the socket timeout alone is not one)
DEFAULT_DEADLINE, MAX_CALLS_IN_FLIGHT = 45.0, 8
MAX_REPLY_BYTES = 1_000_000          # a reply is a few KB of JSON; more than this is read no further and rejected
PROVIDERS = {
    "anthropic": ("https://api.anthropic.com/v1/messages", "ANTHROPIC_API_KEY"),
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"),
}
# Room for the reply. Current Claude models think before they answer and the thinking counts toward max_tokens, so the old
# 400 could end a reply before its JSON. No sampling parameters are sent: current Claude models reject `temperature` (HTTP 400).
MAX_TOKENS = 4000
# A reply that stopped for one of these reasons is not a complete answer: it is rejected and the rule-based extractor answers.
REJECTED_STOPS = {"anthropic": ("max_tokens", "refusal"), "openai": ("length", "content_filter")}


class ModelReplyRejected(Exception):
    """The provider answered, but the reply is cut off or declined. ModelExtractor treats it like any bad reply."""


def model_deadline_seconds() -> float:
    raw = os.environ.get(ENV_DEADLINE)
    if raw is None or not raw.strip():
        return DEFAULT_DEADLINE
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"{ENV_DEADLINE} must be a number of seconds") from None
    if not (0 < value <= 300):
        raise RuntimeError(f"{ENV_DEADLINE} must be more than 0 and at most 300 seconds")
    return value


def make_completer(timeout: float = 30.0, guard=None):
    """guard: an app.spend_guard.SpendGuard. With a guard every call is reserved against its USD cap BEFORE the request is sent
    (a call that does not fit raises SpendCapReached and sends nothing) and settled from the usage in the reply. Anthropic only:
    the guard's price table is Anthropic's, and the guard must have been built for the configured model id."""
    provider = os.environ.get(ENV_PROVIDER, "anthropic")
    if provider not in PROVIDERS:
        raise RuntimeError(f"{ENV_PROVIDER} must be one of {sorted(PROVIDERS)}")
    url, key_env = PROVIDERS[provider]
    key, model = os.environ.get(key_env), os.environ.get(ENV_MODEL)
    if not key or not model:
        raise RuntimeError(f"set {key_env} and {ENV_MODEL} to use the model extractor")
    if guard is not None:
        if provider != "anthropic":
            raise RuntimeError("the spend guard prices Anthropic models only: set APP_MODEL_PROVIDER=anthropic or run without a guard")
        if guard.model != model:
            raise RuntimeError(f"MODEL MISMATCH: the spend guard was built for {guard.model!r} but {ENV_MODEL} is {model!r}")

    def complete(user: str, system: str | None = None) -> str:
        if provider == "anthropic":
            body = {"model": model, "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": user}]}
            if system:
                body["system"] = system
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        else:
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
            body = {"model": model, "messages": msgs}
            headers = {"Authorization": "Bearer " + key, "content-type": "application/json"}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
        res = guard.reserve(system, user, MAX_TOKENS) if guard is not None else None      # raises SpendCapReached: nothing is sent
        try:
            with no_redirect_opener().open(req, timeout=timeout) as r:   # fixed https URL, redirects off
                raw = r.read(MAX_REPLY_BYTES + 1)
                if len(raw) > MAX_REPLY_BYTES:
                    raise ModelReplyRejected("REPLY_TOO_LARGE")
                data = json.loads(raw)
        except urllib.error.HTTPError as e:      # a 4xx was refused before it ran: not billed; a 5xx may have been
            if res is not None:
                guard.fail(res, billed=e.code >= 500, status=e.code)
            raise
        except BaseException:                    # timeout, connection error, oversize or unreadable reply: may have been billed
            if res is not None:
                guard.fail(res, billed=True)
            raise
        if res is not None:
            guard.settle(res, data.get("usage") if isinstance(data, dict) else None)
        if provider == "anthropic":
            stop = data.get("stop_reason")
            if stop in REJECTED_STOPS[provider]:
                raise ModelReplyRejected("STOP_REASON:" + stop)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        choice = data["choices"][0]
        if choice.get("finish_reason") in REJECTED_STOPS[provider]:
            raise ModelReplyRejected("STOP_REASON:" + choice["finish_reason"])
        return choice["message"]["content"]

    complete.supports_system = True          # ModelExtractor sends instructions and buyer text separately
    complete.provider, complete.model = provider, model   # named in the evidence; never the key
    # one wall-clock deadline per call and a cap on calls in flight (app/model_deadline.py); a timeout or a full cap ends as the
    # same failure every reader already handles: the rules answer alone
    return with_deadline(complete, seconds=model_deadline_seconds(), max_in_flight=MAX_CALLS_IN_FLIGHT)
