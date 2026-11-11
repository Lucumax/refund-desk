"""Readers for the Injection Arena. A reader READS a buyer message and PROPOSES. It never decides
money: the deterministic gate (arena_gate.py) does, and it recomputes what it needs from the raw
text instead of trusting the proposal.

Two readers:
- RuleReader: the offline fallback. It is deliberately naive and compliant, the way a weak model can
  be: it takes the largest amount it sees and follows "send me money" wording, so the arena shows
  the gate catching proposals a gullible reader makes. It is not a model.
- ModelReader: wraps the existing ModelExtractor around an injected text-in, text-out call. It is
  chosen with the environment variable ARENA_READER=model (plus the provider variables in
  model_client.py). It has not been run against a real model in the arena; the extractor it wraps has (eval/live/).

Interface: reader.read(text, order) -> Proposal. Must not raise on odd input."""
from __future__ import annotations
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .arena_text import figures
from .extractor import (Extractor, Facts, ModelExtractor, RuleBasedExtractor, EXPLICIT, FULL, AMBIGUOUS)

ENV_READER = "ARENA_READER"
REFUND, ASK, NONE = "REFUND", "ASK", "NONE"
_PAYISH = re.compile(r"refund|money back|reimburs|send (me|us)|pay (me|us)|transfer|wire|give me|credit|release|return my",
                     re.IGNORECASE)


@dataclass(frozen=True)
class Proposal:
    action: str                       # REFUND | ASK | NONE
    amount_minor: int | None
    reason_code: str | None
    source: str                       # "rules" or "model"
    quotes: dict = field(default_factory=dict)      # field -> exact words from the message
    notes: tuple = ()
    explanation: str | None = None
    consent: dict | None = None       # a model's reading of the buyer's consent (app/consent_reader.py): untrusted, signed with the decision

    def as_dict(self) -> dict:
        return {"action": self.action, "amount_minor": self.amount_minor, "reason_code": self.reason_code,
                "source": self.source, "quotes": dict(self.quotes), "notes": list(self.notes),
                "explanation": self.explanation, "consent": dict(self.consent) if self.consent else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        return cls(d["action"], d.get("amount_minor"), d.get("reason_code"), d.get("source", "rules"),
                   dict(d.get("quotes") or {}), tuple(d.get("notes") or ()), d.get("explanation"),
                   dict(d["consent"]) if d.get("consent") else None)


class Reader(ABC):
    kind = "rules"
    name = "reader"

    @abstractmethod
    def read(self, text: str, order) -> Proposal:
        """Return a proposal for one message about one order (`order` has total_minor)."""


class RuleReader(Reader):
    kind, name = "rules", "rule-based fallback reader (not a model)"

    def __init__(self):
        self._facts = RuleBasedExtractor()

    def read(self, text: str, order) -> Proposal:
        try:
            return self._read(text or "", order)
        except Exception:                                    # odd input must not break the arena
            return Proposal(NONE, None, None, "rules", notes=("READER_ERROR",))

    def _read(self, text: str, order) -> Proposal:
        f = self._facts.extract(text)
        quotes = {k: v for k, v in f.evidence.items() if k in ("issue", "amount", "refund_intent")}
        asks_money = bool(_PAYISH.search(text))
        figs = figures(text)
        amount = None
        if figs:
            amount = max(figs)                              # naive: the largest figure wins
        elif f.amount_state == FULL and order is not None:
            amount = order.total_minor
        if not asks_money and not f.refund_intent:
            return Proposal(NONE, None, f.issue_code, "rules", quotes, tuple(f.notes))
        if amount is None or amount <= 0:
            return Proposal(ASK, None, f.issue_code, "rules", quotes, tuple(f.notes) + ("NO_AMOUNT",))
        return Proposal(REFUND, amount, f.issue_code, "rules", quotes, tuple(f.notes))


class ModelReader(Reader):
    kind = "model"

    def __init__(self, extractor: Extractor, name: str = "model reader"):
        self.extractor, self.name = extractor, name

    def read(self, text: str, order) -> Proposal:
        try:
            f: Facts = self.extractor.extract(text)
        except Exception:
            return Proposal(NONE, None, None, "model", notes=("READER_ERROR",))
        quotes = {k: v for k, v in f.evidence.items() if k in ("issue", "amount", "refund_intent")}
        notes = tuple(f.notes)
        if not f.refund_intent:
            return Proposal(NONE, None, f.issue_code, f.source, quotes, notes, f.explanation)
        if f.amount_state == EXPLICIT and f.requested_amount_minor:
            return Proposal(REFUND, f.requested_amount_minor, f.issue_code, f.source, quotes, notes, f.explanation)
        if f.amount_state == FULL and order is not None:
            return Proposal(REFUND, order.total_minor, f.issue_code, f.source, quotes, notes, f.explanation)
        return Proposal(ASK, None, f.issue_code, f.source, quotes, notes + (("AMOUNT_AMBIGUOUS",) if f.amount_state == AMBIGUOUS else ()),
                        f.explanation)


def build_reader(env=None, guard=None) -> Reader:
    """ARENA_READER=rules (default) or model. The model path needs the provider variables named in
    model_client.py and fails early with a clear message if they are missing. No network call is made
    here; the model is called only when a visitor submits a message. `guard` is the session's SpendGuard (app/spend_guard.py): the
    arena's model calls are reserved against the same USD cap as the desk's."""
    env = os.environ if env is None else env
    choice = (env.get(ENV_READER) or "rules").strip().lower()
    if choice == "rules":
        return RuleReader()
    if choice == "model":
        from .model_client import make_completer
        return ModelReader(ModelExtractor(make_completer(guard=guard)), "model reader (" + (env.get("APP_MODEL_ID") or "unnamed") + ")")
    raise RuntimeError(f"{ENV_READER} must be 'rules' or 'model'")
