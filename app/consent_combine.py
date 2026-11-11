"""The combiner: the rules' answer (app/consent.py) and a model's reading (app/consent_reader.py) become ONE
consent decision for gate rule G17. Four invariants, each tested for every pairing in tests/test_consent_model.py:

  I1  a message the rules call HAZARD is never released, whatever the reading says;
  I2  a reading that is not a clean single request (verdict REQUEST, no flag) holds the message even when the rules PASS it,
      and so does a reading whose quote is not the buyer's own words or has no refund word in it: the model only tightens here;
  I3  a message the rules could not READ (UNREAD: a clause that starts with a word they do not know, with no hazard word in
      it) is released only by a clean reading with a verbatim refund quote. This is the ONE place the model loosens anything.
      What it can cost is bounded by the owner's allowance per order, the daily budget and one automatic refund per order;
  I4  with no reading at all the result is exactly the rules' own answer.

The reading is data from a model that may be fooled, so nothing about the amount or the order is read from it."""
from __future__ import annotations
from dataclasses import dataclass

from . import consent
from .consent_reader import ConsentReading, ReplyRejected
from .extractor import _REFUND_WORD


@dataclass(frozen=True)
class ConsentDecision:
    allowed: bool
    path: str                  # rules | rules+model | model | model_resolved
    code: str                  # OK | RULES_HAZARD | UNREAD_NO_MODEL | MODEL_UNAVAILABLE | MODEL_DECLINED | MODEL_QUOTE_INVALID | MODEL_READING_INVALID
    tier: str                  # what the rules said: PASS | UNREAD | HAZARD
    rules_reason: str          # consent.explain()'s reason, unchanged
    clause: str | None = None  # the words the rules could not read, or the clause that held it (the buyer's words, untrusted)
    verdict: str | None = None
    flags: tuple = ()
    model_reason: str | None = None

    def as_dict(self) -> dict:
        d = {"code": self.code, "path": self.path, "tier": self.tier, "rules_reason": self.rules_reason}
        if self.clause:
            d["clause"] = self.clause
        if self.verdict:
            d.update(verdict=self.verdict, flags=list(self.flags))
            if self.model_reason:
                d["model_reason"] = self.model_reason
        return d


def _quote_ok(reading: ConsentReading, text: str) -> bool:
    q = reading.request_quote
    if not q or " ".join(q.split()).lower() not in " ".join(text.split()).lower():
        return False
    return bool(_REFUND_WORD.search(q))


def decide_consent(text: str, reading, reading_required: bool = False) -> ConsentDecision:
    """A reading is a ConsentReading, its dict form (as carried in a proposal), or None. reading_required is True when a consent
    reader WAS configured and its reading could not be had (timeout, refusal, rejected reply): then nothing is released, because
    a reader that fails must not be more permissive than one that abstains. With no reader configured it is False
    and the rules decide alone."""
    a = consent.assess(text)
    tier, reason = a["tier"], a["reason"]
    clause = a.get("signal")
    if tier == consent.TIER_UNREAD:
        clause = (a.get("unread") or [clause])[0]

    def d(allowed, path, code, r=None):
        return ConsentDecision(allowed, path, code, tier, reason, clause,
                               r.verdict if r else None, r.flags if r else (), r.reason if r else None)

    if reading is None:
        if reading_required and tier != consent.TIER_HAZARD:
            return d(False, "model", "MODEL_UNAVAILABLE")
        if tier == consent.TIER_PASS:
            return d(True, "rules", "OK")
        return d(False, "rules", "UNREAD_NO_MODEL" if tier == consent.TIER_UNREAD else "RULES_HAZARD")
    try:
        r = reading if isinstance(reading, ConsentReading) else ConsentReading.from_dict(reading)
    except ReplyRejected:
        return d(False, "rules", "RULES_HAZARD" if tier == consent.TIER_HAZARD else "MODEL_READING_INVALID")
    if tier == consent.TIER_HAZARD:
        return d(False, "rules", "RULES_HAZARD", r)                                  # I1
    if not r.clean:
        return d(False, "model", "MODEL_DECLINED", r)                                # I2
    if not _quote_ok(r, text):
        return d(False, "model", "MODEL_QUOTE_INVALID", r)                           # I2, I3
    if tier == consent.TIER_PASS:
        return d(True, "rules+model", "OK", r)
    return d(True, "model_resolved", "OK", r)                                         # I3
