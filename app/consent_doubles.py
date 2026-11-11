"""Test doubles for the consent reader. NOT a model, and never presented as one. They stand in for the completer that
app/model_client.make_completer() returns: the same call, `complete(user, system=None) -> str`, the same attributes, so the real
ModelConsentReader (app/consent_reader.py) runs unchanged on top of them.

  ScriptedConsentClient   canned replies keyed by the buyer's text, with a switch to raise (a timeout, a refusal);
  RecordedClient          the same, loaded from eval/consent_recordings.json (replies written by hand, see that file's note);
  FooledClient            says "a plain request, no flags" to EVERYTHING, quoting the sentence that holds a refund word: the worst
                          case, a reader that has been talked into agreeing;
  IdealClient             answers from the corpus label (legit: clean; attack: not a request with a flag; unclear: unsure): an
                          upper bound that no real model reaches, useful only to show what the pipeline does when the reader is right.

None of these measures a real model. What the real model does is unmeasured until `python -m app repro --consent live` is run."""
from __future__ import annotations
import hashlib
import json
import re

from .extractor import _REFUND_WORD

_BOUND = re.compile(r"<<<BUYER_TEXT [0-9a-f]+>>>\n(.*)\n<<<END_BUYER_TEXT [0-9a-f]+>>>", re.S)


def reply(verdict: str, flags, quote, reason) -> str:
    """The JSON a model would send, in the exact shape ModelConsentReader asks for."""
    return json.dumps({"verdict": verdict, "flags": list(flags), "request_quote": quote, "reason": reason})


def clean_reply(quote: str, reason: str = "A plain request for money back.") -> str:
    return reply("REQUEST", [], quote, reason)


def key(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _buyer_text(user: str) -> str:
    m = _BOUND.search(user)
    return m.group(1) if m else user


class RecordingMissing(KeyError):
    """No recorded reply for this message: the reader treats it like a model that failed, and the rules answer alone."""


class ScriptedConsentClient:
    supports_system = True
    provider, model = "scripted", "scripted-consent-v1"

    def __init__(self, replies: dict[str, str] | None = None, default: str | None = None, raises: Exception | None = None):
        self.by_key = {key(t): (t, r) for t, r in (replies or {}).items()}
        self.default, self.raises = default, raises
        self.calls: list[tuple] = []

    def texts(self) -> set[str]:
        return {t for t, _ in self.by_key.values()}

    def __call__(self, user: str, system: str | None = None) -> str:
        self.calls.append((system, user))
        if self.raises is not None:
            raise self.raises
        text = _buyer_text(user)
        hit = self.by_key.get(key(text))
        if hit is not None:
            return hit[1]
        if self.default is not None:
            return self.default
        raise RecordingMissing(key(text))


class RecordedClient(ScriptedConsentClient):
    provider, model = "recorded", "recorded-consent-v1"


def load_recordings(path: str) -> RecordedClient:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # An entry saved from a live run keeps the model's exact text in raw_reply (plain JSON, JSON in a fence, or text that is not JSON at
    # all, parse_status says which): replaying that text reaches the same verdict the live run did. Hand-written entries have only `reply`.
    return RecordedClient({e["text"]: e["raw_reply"] if isinstance(e.get("raw_reply"), str) else json.dumps(e["reply"])
                           for e in data["entries"]})


def _request_sentence(text: str) -> str | None:
    """The sentence of the text that holds a refund word (what a fooled reader would quote), or None."""
    for part in re.split(r"(?<=[.!?])\s+", text):
        if _REFUND_WORD.search(part):
            return part.strip()[:300]
    return None


class FooledClient(ScriptedConsentClient):
    provider, model = "scripted", "fooled-consent-double"

    def __call__(self, user: str, system: str | None = None) -> str:
        self.calls.append((system, user))
        text = _buyer_text(user)
        q = _request_sentence(text) or text[:200]
        return clean_reply(q, "Fooled double: agrees with everything.")


class IdealClient(ScriptedConsentClient):
    provider, model = "scripted", "ideal-consent-double"

    def __init__(self, labels: dict[str, str]):
        super().__init__()
        self.labels = {key(t): v for t, v in labels.items()}

    def __call__(self, user: str, system: str | None = None) -> str:
        self.calls.append((system, user))
        text = _buyer_text(user)
        label = self.labels.get(key(text), "unclear")
        if label == "legit":
            return clean_reply(_request_sentence(text) or text[:200], "Ideal double: the label says this is an ordinary request.")
        if label == "attack":
            return reply("NOT_A_REQUEST", ["OTHER"], None, "Ideal double: the label says this is an attack.")
        return reply("UNSURE", [], None, "Ideal double: the label says unclear.")
