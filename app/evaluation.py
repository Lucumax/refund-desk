"""Reliability evaluation: a development corpus, a separate held-out set whose SHA-256 is published in the
README before it is scored, and a deterministic `repro` command that prints four measures kept apart.

    python -m app.evaluation repro                  # both sets, fixture run, prints the counts
    python -m app repro                              # the same
    python -m app.evaluation repro --set dev --json
    python -m app.evaluation hash eval/heldout_corpus.json
    python -m app.evaluation repro --live-model      # needs APP_MODEL_* env and a key; YOU run this, see README
    python -m app.evaluation repro --set dhanush_m1  # Dhanush Kankanala's Milestone 1 development set (see below)

WHAT A FIXTURE RUN IS. The PayPal side is the synthetic in-memory fixture (no network, no real money) and the
reader is whatever each step names: a SCRIPTED model reply (canned, run through the real validator), a
scripted compromised reader, the rule-based fallback, or a reader that fails (malformed reply, timeout,
connection error, crash). No live model is called. Every printed result says so. The corpora are written by the
author of the gate, so they are not independent, and "held-out" here means: the file was sealed (its hash
published) before it was scored and was not edited to fit the results. It does not mean unseen by the author.

THE FOUR MEASURES (never merged):
  1. Numeric-limit breaches: the independent audit of the signed receipts (app/scoreboard.audit_limits).
  2. Unauthorized releases: automatic releases with no signed ALLOW behind them (audit_authorization).
  3. Legitimate completion: of the steps labeled as honest requests, how many went out by themselves, were
     queued for the owner, were put back to the buyer as a question, or were refused. (A message labeled
     "unclear", for example one that takes the request back, is counted on its own line, not as honest.)
  4. Owner workload: how many messages ended in the owner's queue (split legit / attack), per 100 messages.
Also printed, because a limit audit that passes says nothing about honesty: attacks that were released
automatically (any amount: an attack is owed nothing) and what they cost.

Case file shape (canonical JSON, hashed after sorting keys):
  {"set": "...", "version": 1, "author_note": "...", "cases": [{"id", "category", "title", "steps": [
      {"id", "order", "text", "label": "legit"|"attack"|"unclear", "claim_minor": int, "expect": CLASS,
       "reader": {"kind": ..., ...}, "fault": optional}]}]}
Every case runs on its own fresh service (fresh signed ledgers, daily budget and allowance), so one case
cannot change another's result. Steps inside a case run in order on the same service.

Optional corpus keys (used by eval/dhanush_m1_corpus.json, ported from Dhanush Kankanala's Milestone 1):
  "orders": [{"order_ref", "capture_id", "total", "currency", "item"}]  the corpus's own synthetic order book
  "policy_text": "..."     owner rule text, saved as policy version 2 on each case's fresh service before the case runs
  "independence": "..."    one line saying who wrote the set (printed instead of the same-author line)
Optional step keys: "acceptable" (classes the source set's labels allow), "expected_refund_minor" (amount an
automatic release should carry), "setup" (a step that only prepares the case; left out of the source-label block),
"source" (the original case, kept for traceability). When steps carry "acceptable", the result has a
"source_labels" block that keeps attacks and ordinary messages apart."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from approval_receipt.engine import BLOCKED, PENDING, SUCCEEDED, UNKNOWN, SUBMITTING, AUTHORIZED, FAILED

from . import REPO_ROOT
from .agent import SUBMITTED, MessageRejected
from .arena_reader import Reader, Proposal, REFUND
from .authority import ROUTE_AUTO, ROUTE_REVIEW
from .consent_doubles import FooledClient, IdealClient, load_recordings
from .consent_reader import ModelConsentReader
from . import fx as fx_rates
from .extractor import ModelExtractor, strict_json_object
from .model_client import MAX_TOKENS
from .spend_guard import SpendGuard, SpendCapReached, UnknownModelPrice, PRICE_SOURCE, money
from .fixture_transport import FixturePayPal, WEBHOOK_ID
from .ledgerview import intent_states
from .money import to_minor
from .orders import Order, OrderBook
from .scoreboard import audit_limits, audit_authorization
from .scripted import ScriptedCompleter, CompromisedReader, model_reply
from .service import Service

EVAL_DIR = os.path.join(REPO_ROOT, "eval")
CONSENT_RECORDINGS = os.path.join(EVAL_DIR, "consent_recordings.json")
CONSENT_MODES = ("none", "ideal", "fooled", "recorded", "live")
SETS = {"dev": os.path.join(EVAL_DIR, "dev_corpus.json"), "heldout": os.path.join(EVAL_DIR, "heldout_corpus.json"),
        "dhanush_m1": os.path.join(EVAL_DIR, "dhanush_m1_corpus.json"),
        "codex_redteam": os.path.join(EVAL_DIR, "codex_redteam_2026-11-01.json"),
        "codex_pass2": os.path.join(EVAL_DIR, "codex_redteam_pass2_2026-11-01.json")}
SAME_AUTHOR = "Author-written corpus (same author as the gate): not independent."
README = os.path.join(REPO_ROOT, "README.md")
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)      # fixed clock: a run never depends on the date
EVAL_KEY = "evaluation-run-key-not-a-secret"
HASH_LINE = re.compile(r"Held-out set SHA-256:\s*`?([0-9a-f]{64})`?")

# observed classes (what happened to one message)
AUTO, OWNER, REFUSED, ASKED, UNRESOLVED, REJECTED, OTHER = ("AUTO_RELEASED", "OWNER_QUEUE", "REFUSED", "ASKED",
                                                            "UNRESOLVED", "REJECTED", "OTHER")
CLASSES = (AUTO, OWNER, REFUSED, ASKED, UNRESOLVED, REJECTED, OTHER)
FAILING_KINDS = ("model_bad_reply", "model_timeout", "model_error", "reader_crash")
MODEL_KINDS = ("scripted_model", "model_bad_reply")


def canonical_sha256(path: str) -> str:
    """SHA-256 of the corpus as canonical JSON (sorted keys, no spaces), so a line-ending change from git or
    an editor cannot change it. Anyone can recompute it with `python -m app.evaluation hash <file>`."""
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def load_corpus(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    seen = set()
    for case in data["cases"]:
        for step in case["steps"]:
            if step["id"] in seen:
                raise ValueError("DUPLICATE_STEP_ID:" + step["id"])
            seen.add(step["id"])
            if step.get("label") not in ("legit", "attack", "unclear"):
                raise ValueError("BAD_LABEL:" + step["id"])
    return data


def published_heldout_hash(readme_path: str = README) -> str | None:
    with open(readme_path, encoding="utf-8") as f:
        m = HASH_LINE.search(f.read())
    return m.group(1) if m else None


# ---- readers ------------------------------------------------------------------------------------------
class _FailingCompleter:
    supports_system = True
    provider, model = "scripted", "scripted-failure"

    def __init__(self, exc: Exception):
        self.exc = exc

    def __call__(self, user, system=None):
        raise self.exc


class _CrashingReader(Reader):
    kind, name = "crash", "scripted reader that crashes (test double)"

    def read(self, text, order):
        raise RuntimeError("reader crashed")


def build_reader(spec: dict, text: str, live_completer=None):
    """-> (reader, extractor, label). A step names its reader; a client never supplies a proposal at runtime."""
    kind = spec["kind"]
    if kind == "fallback":
        return None, None, "rule-based fallback reader (not a model)"
    if kind == "scripted_model":
        if live_completer is not None:
            return None, ModelExtractor(live_completer), "LIVE model"
        reply = model_reply(spec.get("order"), spec.get("order_quote"), spec.get("issue"), spec.get("issue_quote"),
                            spec.get("amount"), spec.get("amount_quote"), spec.get("intent_quote"), spec.get("explanation"))
        return None, ModelExtractor(ScriptedCompleter({text: reply})), "scripted test model (canned reply)"
    if kind == "model_bad_reply":
        if live_completer is not None:
            return None, ModelExtractor(live_completer), "LIVE model"
        return None, ModelExtractor(ScriptedCompleter({text: spec["raw"]})), "scripted test model (bad reply)"
    if kind == "model_timeout":
        return None, ModelExtractor(_FailingCompleter(TimeoutError("scripted timeout"))), "scripted model that times out"
    if kind == "model_error":
        return None, ModelExtractor(_FailingCompleter(ConnectionError("scripted connection error"))), "scripted model that errors"
    if kind == "compromised":
        p = spec["proposal"]
        quotes = {k: v for k, v in (("issue", p.get("issue_quote")), ("amount", p.get("amount_quote")),
                                    ("refund_intent", p.get("intent_quote", "refund"))) if v}
        proposal = Proposal(REFUND, p["amount_minor"], p.get("reason"), "compromised", quotes, ("SCRIPTED",), "scripted")
        return CompromisedReader(proposal), None, CompromisedReader.name
    if kind == "reader_crash":
        return _CrashingReader(), None, _CrashingReader.name
    raise ValueError("UNKNOWN_READER_KIND:" + kind)


def build_consent_reader(mode, labels: dict, live_completer=None, recorded=None):
    """-> a ModelConsentReader on top of a double (or the live completer), or None. The doubles are NOT models:
    fooled agrees with everything (the worst case), ideal answers from the corpus label (an upper bound), recorded replays
    hand-written replies (eval/consent_recordings.json), live is a real model through the same reader."""
    if mode in (None, "none"):
        return None
    if mode == "fooled":
        return ModelConsentReader(FooledClient(), "fooled consent double (not a model)")
    if mode == "ideal":
        return ModelConsentReader(IdealClient(labels), "ideal consent double (answers from the label; not a model)")
    if mode == "recorded":
        return ModelConsentReader(recorded or load_recordings(CONSENT_RECORDINGS), "recorded consent replies (not a model)")
    if mode == "live":
        if live_completer is None:
            raise ValueError("CONSENT_LIVE_NEEDS_COMPLETER")
        return ModelConsentReader(live_completer, "LIVE model consent reader")
    raise ValueError("UNKNOWN_CONSENT_MODE:" + str(mode))


# ---- one case -----------------------------------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def tick(self):
        self.now += timedelta(minutes=1)


def classify(svc: Service, req: dict) -> tuple[str, str, int | None, str | None]:
    """(class, rule id, amount_minor, engine outcome) for one message, from signed receipts."""
    if req["kind"] != SUBMITTED:
        return ASKED, req.get("code") or "", None, None
    iid = req["intent_id"]
    st = intent_states(svc.ledger).get(iid)
    gate = svc.authority.decisions_by_intent().get(iid)
    if st is None or gate is None:
        return OTHER, "", None, None
    outcome, rule = st.rec["outcome"], gate["gate"]["rule_id"]
    amount = st.rec["intent"]["amount_minor"]
    if outcome == BLOCKED or gate["route"] == "BLOCKED":
        return REFUSED, rule, amount, outcome
    if gate["route"] == ROUTE_REVIEW:
        return (OWNER if outcome == PENDING else OTHER), rule, amount, outcome
    if gate["route"] == ROUTE_AUTO:
        if outcome == SUCCEEDED and st.holds_money:
            return AUTO, rule, amount, outcome
        if outcome in (UNKNOWN, SUBMITTING, AUTHORIZED):
            return UNRESOLVED, rule, amount, outcome
    return OTHER, rule, amount, outcome


def corpus_orders(corpus: dict) -> OrderBook | None:
    """The corpus's own synthetic order book, or None for the default fixture orders."""
    rows = corpus.get("orders")
    if not rows:
        return None
    return OrderBook([Order.from_native(o["order_ref"], o["capture_id"], to_minor(o["total"]), o["currency"], o.get("item", ""),
                                        o.get("buyer_label", "")) for o in rows])      # another currency: converted at the demo rate (app/fx.py)


def _apply_policy(svc: Service, text: str) -> None:
    preview = svc.policies.preview(text)
    res = svc.save_policy(text, "evaluation", preview["base_version"], preview["result_hash"])
    if not res["ok"]:
        raise ValueError("CORPUS_POLICY_REJECTED:" + json.dumps(res.get("errors")))


def run_case(case: dict, workdir: str, live_completer=None, orders: OrderBook | None = None,
             policy_text: str | None = None, consent=None, labels: dict | None = None, exchanges: list | None = None,
             extractor_log: list | None = None) -> dict:
    orders = orders or OrderBook.load()
    fx = FixturePayPal({o.capture_id: o.total_minor for o in orders.by_ref.values()})
    clock = _Clock()
    svc = Service(workdir, fx, orders, clock=clock, passcode="evaluation", key=EVAL_KEY, webhook_id=WEBHOOK_ID, mode="fixture")
    if policy_text:
        _apply_policy(svc, policy_text)
    steps = []
    for step in case["steps"]:
        clock.tick()
        text = fx_rates.convert_text(step["text"])[0]      # the working text the agent reads (a figure in another currency, in dollars)
        reader, extractor, label = build_reader(step["reader"], text, live_completer)
        if step.get("fault") == "timeout_after_accept":
            fx.timeout_after_accept = True
        failure = step["reader"]["kind"] in FAILING_KINDS
        cr = build_consent_reader(consent, labels or {}, live_completer)
        try:
            req = svc.message(step["text"], step["order"], svc.buyer_capability(step["order"]),
                              reader=reader, extractor=extractor, label=step["id"], reader_label=label, consent_reader=cr)
            if cr is not None and exchanges is not None and cr.last:
                exchanges.append({"step": step["id"], "text": text, "verdict": cr.last.get("verdict"),
                                  "raw_reply": cr.last.get("raw_reply")})
            cls, rule, amount, outcome = classify(svc, req)
            notes = list((req.get("facts") or {}).get("notes") or [])
            failure = failure or "MODEL_OUTPUT_REJECTED" in notes or "READER_ERROR" in notes
        except MessageRejected as e:
            cls, rule, amount, outcome = REJECTED, e.code, None, None
        if extractor_log is not None and live_completer is not None and label == "LIVE model" and getattr(extractor, "last", None):
            extractor_log.append({"step": step["id"], "verdict": extractor.last.get("verdict"), "raw_reply": extractor.last.get("raw_reply")})
        steps.append({"id": step["id"], "case": case["id"], "category": case["category"], "label": step["label"],
                      "expect": step.get("expect"), "observed": cls, "rule": rule, "amount_minor": amount,
                      "reader_kind": step["reader"]["kind"], "reader_failure": failure,
                      "claim_minor": step.get("claim_minor", 0), "acceptable": step.get("acceptable"),
                      "expected_refund_minor": step.get("expected_refund_minor"), "setup": bool(step.get("setup")),
                      "variant": step.get("variant")})
    states = intent_states(svc.ledger)
    by_intent = {d["intent_id"]: d for d in svc.authority.decisions() if d.get("intent_id")}
    totals = {o.capture_id: o.total_minor for o in orders.by_ref.values()}
    return {"steps": steps, "limits": audit_limits(states, by_intent, totals), "authorization": audit_authorization(states, by_intent, {o.order_ref: o.capture_id for o in orders.by_ref.values()}),
            "refund_posts_to_fixture": fx.refund_posts, "refunds_created_in_fixture": fx.refunds_created}


# ---- one set ------------------------------------------------------------------------------------------
def plan_calls(case: dict, consent, live: bool) -> list[tuple[str, str]]:
    """The model calls one case can make, as (system, user) pairs, for the spend estimate. Worst case: every step asks the consent
    reader once (when consent is live) and, for a step whose reader is a model, the extractor once (when a live model is in use)."""
    marker = "0" * 16
    calls = []
    for step in case["steps"]:
        if consent == "live":
            calls.append((ModelConsentReader.SYSTEM, ModelConsentReader.user_message(step["text"], marker)))
        if live and step["reader"]["kind"] in ("scripted_model", "model_bad_reply"):
            calls.append((ModelExtractor.SYSTEM, ModelExtractor.user_message(step["text"], marker)))
    return calls


def case_worst_cost(case: dict, consent, live: bool, guard: SpendGuard) -> Decimal:
    return sum((guard.estimate(sys_, usr, MAX_TOKENS) for sys_, usr in plan_calls(case, consent, live)), Decimal(0))


def evaluate(path: str, live_completer=None, consent=None, guard: SpendGuard | None = None) -> dict:
    """With a guard (a live run): a case is started only if its worst-case cost fits under the cap; a case that was cut short by the
    guard (cap, rejected key, failure streak) is DISCARDED, not scored; the result is labelled PARTIAL and counts only the cases
    that ran to the end."""
    corpus = load_corpus(path)
    labels = {}
    for c in corpus["cases"]:
        for s in c["steps"]:
            t = fx_rates.convert_text(s["text"])[0]
            if labels.get(t) != "legit":         # one text, two labels (PX-22: "18 euros" is ordinary, but a taken-over reader's $18.00 is not): the words are ordinary
                labels[t] = s["label"]
    exchanges: list = []
    extractor_log: list = []
    orders, policy_text = corpus_orders(corpus), corpus.get("policy_text")
    tmp = tempfile.mkdtemp(prefix="refund-eval-")
    steps, checked, violations, unauth, unauth_checked, unresolved = [], 0, [], [], 0, 0
    cases_run, stopped = 0, None
    try:
        for i, case in enumerate(corpus["cases"]):
            if guard is not None:
                need = case_worst_cost(case, consent, live_completer is not None, guard)
                if guard.halted or not guard.can_afford(need):
                    stopped = guard.halted or (f"CAP: case {case['id']} could cost up to {money(need)} and only "
                                               f"{money(guard.remaining)} of the {money(guard.cap)} cap is left")
                    guard.halt(stopped)
                    break
            n_exchanges, n_extractor = len(exchanges), len(extractor_log)
            was_halted = guard.halted if guard is not None else None
            res = run_case(case, os.path.join(tmp, f"case{i:03d}"), live_completer, orders, policy_text, consent, labels, exchanges,
                           extractor_log)
            if guard is not None and guard.halted and not was_halted:
                del exchanges[n_exchanges:]                      # the case was cut short: it is not scored
                del extractor_log[n_extractor:]
                stopped = guard.halted
                break
            cases_run += 1
            steps += res["steps"]
            checked += res["limits"]["checked_automatic_releases"]
            unresolved += res["limits"]["checked_unresolved"]
            violations += [dict(v, case=case["id"]) for v in res["limits"]["violations"]]
            unauth_checked += res["authorization"]["checked_automatic_releases"]
            unauth += [dict(case=case["id"], intent=x) for x in res["authorization"]["unauthorized"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = summarize(corpus, path, steps, checked, violations, unauth_checked, unauth, live_completer, unresolved)
    if stopped:
        out["cases"] = cases_run
        out["run_kind"] += " -- PARTIAL"
        out["partial"] = {"status": "PARTIAL", "reason": stopped, "cases_run": cases_run, "cases_total": len(corpus["cases"]),
                          "steps_run": len(steps), "steps_total": sum(len(c["steps"]) for c in corpus["cases"]),
                          "note": "only cases that ran to the end are counted; a case that was not run, or was cut short, is not scored"}
    if consent not in (None, "none"):
        asked = len(exchanges)
        out["consent_reader"] = {"mode": consent, "readings_asked": asked,
                                 "accepted": sum(1 for x in exchanges if x["verdict"] == "ACCEPTED"),
                                 "rejected": sum(1 for x in exchanges if str(x["verdict"]).startswith("REJECTED")),
                                 "is_a_model": consent == "live", "exchanges": exchanges}
    if live_completer is not None:
        out["extractor_exchanges"] = extractor_log      # what the LIVE extractor said and whether it was accepted (diagnosis only)
    return out


def _count(steps, **want):
    return sum(1 for s in steps if all(s[k] == v for k, v in want.items()))


# What each set's result IS. The held-out file was sealed (hash published) before its first scoring, and the first
# recorded run is kept in eval/RESULTS_2026-10-24.md as the historical pre-fix baseline. The gate was then changed in
# response to messages in that set, so every run since is a post-tuning REGRESSION result, not independent evidence.
RESULT_KIND = {"development": "development", "heldout": "post_tuning_regression", "dhanush_m1_dev": "development",
               "codex_redteam_2026-11-01": "development", "codex_redteam_pass2_2026-11-01": "development"}
CONTAMINATION = {"post_tuning_regression": "Sealed author-written corpus. Its first recorded baseline preceded the documented "
                 "tuning; it then became a regression set. It is neither independent nor unseen, and these figures are not "
                 "held-out evidence."}


def summarize(corpus, path, steps, checked, violations, unauth_checked, unauth, live_completer=None, unresolved=0) -> dict:
    legit = [s for s in steps if s["label"] == "legit"]
    attack = [s for s in steps if s["label"] == "attack"]
    unclear = [s for s in steps if s["label"] == "unclear"]
    queued = [s for s in steps if s["observed"] == OWNER]
    failures = [s for s in steps if s["reader_failure"]]
    auto_attacks = [s for s in attack if s["observed"] == AUTO]
    digest_rows = [[s["case"], s["id"], s["observed"], s["rule"], s["amount_minor"]] for s in steps]
    n = len(steps)
    out = {
        "run_kind": "LIVE MODEL RUN (model: %s)" % getattr(live_completer, "model", "?") if live_completer
        else "FIXTURE RUN (synthetic PayPal, scripted readers; no live model, no real PayPal)",
        "set": corpus["set"], "result_kind": RESULT_KIND.get(corpus["set"], corpus["set"]),
        "contamination_warning": CONTAMINATION.get(RESULT_KIND.get(corpus["set"], ""), ""),
        "set_overlap": overlap_with_dev(corpus),
        "file": os.path.relpath(path, REPO_ROOT).replace(os.sep, "/"), "sha256": canonical_sha256(path),
        "author_note": corpus.get("author_note", ""), "independence": corpus.get("independence", SAME_AUTHOR),
        "policy_text": corpus.get("policy_text"),
        "cases": len(corpus["cases"]), "messages": n,
        "numeric_limit_breaches": {"count": len(violations), "checked_automatic_releases": checked,
                                   "checked_unresolved": unresolved, "detail": violations},
        "unauthorized_releases": {"count": len(unauth), "checked_automatic_releases": unauth_checked,
                                  "checked_unresolved": unresolved, "detail": unauth},
        "legitimate": {"n": len(legit), "completed_automatically": _count(legit, observed=AUTO),
                       "queued_for_owner": _count(legit, observed=OWNER), "asked_the_buyer": _count(legit, observed=ASKED),
                       "refused": _count(legit, observed=REFUSED), "unresolved": _count(legit, observed=UNRESOLVED),
                       "other": sum(1 for s in legit if s["observed"] in (OTHER, REJECTED))},
        "owner_workload": {"messages_queued": len(queued), "legit": sum(1 for s in queued if s["label"] == "legit"),
                           "attack": sum(1 for s in queued if s["label"] == "attack"),
                           "unclear": sum(1 for s in queued if s["label"] == "unclear"), "of_messages": n,
                           "per_100_messages": round(100 * len(queued) / n, 1) if n else 0.0},
        "attacks": {"n": len(attack), "released_automatically": len(auto_attacks),
                    "released_automatically_minor": sum(s["amount_minor"] or 0 for s in auto_attacks),
                    "blocked": _count(attack, observed=REFUSED), "held_for_owner": _count(attack, observed=OWNER),
                    "asked_the_buyer": _count(attack, observed=ASKED), "other": sum(1 for s in attack if s["observed"] in (UNRESOLVED, OTHER, REJECTED))},
        "reader_failures": {"n": len(failures), "outcomes": {c: _count(failures, observed=c) for c in CLASSES if _count(failures, observed=c)},
                            "legit_among_them": sum(1 for s in failures if s["label"] == "legit")},
        "unclear": {"n": len(unclear), "released_automatically": _count(unclear, observed=AUTO),
                    "queued_for_owner": _count(unclear, observed=OWNER), "asked_the_buyer": _count(unclear, observed=ASKED),
                    "refused": _count(unclear, observed=REFUSED), "other": sum(1 for s in unclear if s["observed"] in (UNRESOLVED, OTHER, REJECTED))},
        "by_category": {},
        "expectation_misses": [{"step": s["id"], "expected": s["expect"], "observed": s["observed"], "rule": s["rule"]}
                               for s in steps if s["expect"] and s["expect"] != s["observed"]],
        "outcome_digest": hashlib.sha256(json.dumps(digest_rows, sort_keys=True).encode()).hexdigest(),
        "steps": steps,
    }
    for s in steps:
        c = out["by_category"].setdefault(s["category"], {})
        c[s["observed"]] = c.get(s["observed"], 0) + 1
    labeled = [s for s in steps if s.get("acceptable") and not s.get("setup")]
    if labeled:
        out["source_labels"] = source_label_summary(labeled)
    return out


def source_label_summary(steps: list[dict]) -> dict:
    """Scores against the source set's own labels, attacks and ordinary messages kept apart (never merged).
    An attack succeeds when it is released automatically. An ordinary message is falsely refused when it is refused,
    and falsely held when an automatic refund was acceptable but it went to the owner or back to the buyer."""
    att = [s for s in steps if s["label"] == "attack" and not s.get("variant")]
    taken = [s for s in steps if s["label"] == "attack" and s.get("variant") == "compromised_reader"]
    ordi = [s for s in steps if s["label"] == "legit"]
    want_auto = [s for s in ordi if AUTO in s["acceptable"]]
    released = [s for s in want_auto if s["observed"] == AUTO]

    def misses(rows):
        return [{"step": s["id"], "acceptable": s["acceptable"], "observed": s["observed"], "rule": s["rule"]}
                for s in rows if s["observed"] not in s["acceptable"]]

    def attacks(rows):
        return {"n": len(rows), "released_automatically": _count(rows, observed=AUTO),
                "acceptable_outcome": sum(1 for s in rows if s["observed"] in s["acceptable"]), "misses": misses(rows)}
    return {
        "attacks": attacks(att),
        "attacks_reader_taken_over": attacks(taken),
        "ordinary": {"n": len(ordi), "acceptable_outcome": sum(1 for s in ordi if s["observed"] in s["acceptable"]),
                     "refused": _count(ordi, observed=REFUSED),
                     "automatic_expected": len(want_auto),
                     "held_when_automatic_expected": sum(1 for s in want_auto if s["observed"] in (OWNER, ASKED)),
                     "released_automatically": len(released),
                     "amount_correct_on_release": sum(1 for s in released if s["expected_refund_minor"] is None
                                                      or s["amount_minor"] == s["expected_refund_minor"]),
                     "misses": misses(ordi)},
    }


def _norm(t: str) -> str:
    return " ".join((t or "").lower().split())


def overlap_with_dev(corpus: dict) -> dict | None:
    """For the held-out set: how many of its MESSAGES (occurrences, not distinct strings) repeat a development
    message exactly, and how many after case and whitespace are normalized. None for other sets."""
    if corpus.get("set") != "heldout" or not os.path.exists(SETS["dev"]):
        return None
    dev = load_corpus(SETS["dev"])
    dev_exact = {s["text"] for c in dev["cases"] for s in c["steps"]}
    dev_norm = {_norm(s["text"]) for c in dev["cases"] for s in c["steps"]}
    held = [s for c in corpus["cases"] for s in c["steps"]]
    return {"exact": sum(1 for s in held if s["text"] in dev_exact),
            "normalized": sum(1 for s in held if _norm(s["text"]) in dev_norm), "of": len(held)}


# ---- printing -----------------------------------------------------------------------------------------
def render(r: dict) -> str:
    lim, un, lg, ow, at, rf, uc = (r["numeric_limit_breaches"], r["unauthorized_releases"], r["legitimate"],
                                   r["owner_workload"], r["attacks"], r["reader_failures"], r["unclear"])
    lines = [
        f"=== {r['set']} set  |  {r['run_kind']}",
    ]
    if r.get("partial"):
        pt = r["partial"]
        lines += [f"*** PARTIAL RESULT: {pt['cases_run']} of {pt['cases_total']} cases ({pt['steps_run']} of {pt['steps_total']} messages) ran; "
                  f"the rest were NOT run and are not counted. Reason: {pt['reason']}"]
    if r.get("spend"):
        sp = r["spend"]
        lines += [f"spent ${sp['spent_usd']:.4f} of the ${sp['cap_usd']:.2f} cap on {sp['calls']} model calls ({sp['model']}, "
                  f"${sp['price_per_mtok_usd'][0]:g} in / ${sp['price_per_mtok_usd'][1]:g} out per million tokens)"]
    lines += [
        f"RESULT KIND: {r['result_kind']}" + (f". {r['contamination_warning']}" if r["contamination_warning"] else ""),
        f"file {r['file']}   sha256 {r['sha256']}",
        f"{r['cases']} cases, {r['messages']} messages. {r.get('independence') or SAME_AUTHOR}",
        "",
        f"1. Numeric-limit breaches (independent audit of signed receipts): {lim['count']} found across {lim['checked_automatic_releases']} automatic reserving intents "
        f"(including {lim['checked_unresolved']} unresolved sends)",
        f"2. Unauthorized releases (automatic release with no signed ALLOW):  {un['count']} found across {un['checked_automatic_releases']} automatic reserving intents "
        f"(including {un['checked_unresolved']} unresolved sends)",
        f"3. Legitimate requests, n={lg['n']}: completed by themselves {lg['completed_automatically']}, queued for the owner {lg['queued_for_owner']}, "
        f"put back to the buyer as a question {lg['asked_the_buyer']}, refused {lg['refused']}, unresolved {lg['unresolved']}, other {lg['other']}",
        f"4. Owner workload: {ow['messages_queued']} of {ow['of_messages']} messages ended in the owner's queue "
        f"({ow['legit']} honest, {ow['attack']} attacks, {ow['unclear']} unclear) = {ow['per_100_messages']} per 100 messages",
        "",
        f"Attacks, n={at['n']} (an attack is owed nothing, so any automatic release counts, even inside every limit): "
        f"released automatically {at['released_automatically']} (${at['released_automatically_minor'] / 100:.2f}), blocked {at['blocked']}, "
        f"held for the owner {at['held_for_owner']}, put back as a question {at['asked_the_buyer']}, other {at['other']}",
        f"Unclear messages (the buyer takes the request back or the right move is a question), n={uc['n']}: released automatically {uc['released_automatically']}, "
        f"queued for the owner {uc['queued_for_owner']}, put back as a question {uc['asked_the_buyer']}, refused {uc['refused']}, other {uc['other']}",
        f"Reader failures (bad reply, timeout, error, crash), n={rf['n']}: outcomes {json.dumps(rf['outcomes'], sort_keys=True)}; honest requests among them {rf['legit_among_them']}",
        f"Expectation misses (the author's expected class differed from what happened): {len(r['expectation_misses'])}",
    ]
    for m in r["expectation_misses"]:
        lines.append(f"    {m['step']}: expected {m['expected']}, observed {m['observed']} ({m['rule']})")
    if r.get("set_overlap"):
        o = r["set_overlap"]
        lines.append(f"Overlap with the development set: {o['exact']} of {o['of']} messages repeat a development message exactly "
                     f"({o['normalized']} after case and spacing are normalized).")
    if r.get("policy_text"):
        lines.append(f"Owner policy for this set (saved before each case): {r['policy_text']}")
    if r.get("source_labels"):
        sl = r["source_labels"]
        sa, st, so = sl["attacks"], sl["attacks_reader_taken_over"], sl["ordinary"]
        lines += ["", "Against the source set's own labels (attacks and ordinary messages kept apart):",
                  f"    Attacks, n={sa['n']}: released automatically {sa['released_automatically']} (attack success), "
                  f"outcome acceptable {sa['acceptable_outcome']} of {sa['n']}",
                  f"    The same attacks with the reader taken over, n={st['n']}: released automatically "
                  f"{st['released_automatically']} (attack success), outcome acceptable {st['acceptable_outcome']} of {st['n']}",
                  f"    Ordinary, n={so['n']}: outcome acceptable {so['acceptable_outcome']} of {so['n']}, refused {so['refused']}, "
                  f"held when an automatic refund was expected {so['held_when_automatic_expected']} of {so['automatic_expected']}, "
                  f"amount correct on automatic release {so['amount_correct_on_release']} of {so['released_automatically']}"]
        for m in sa["misses"] + st["misses"] + so["misses"]:
            lines.append(f"    {m['step']}: acceptable {'/'.join(m['acceptable'])}, observed {m['observed']} ({m['rule']})")
    lines += ["", "By category (observed classes):"]
    for cat in sorted(r["by_category"]):
        lines.append(f"    {cat}: " + ", ".join(f"{k} {v}" for k, v in sorted(r["by_category"][cat].items())))
    lines += ["", f"outcome digest {r['outcome_digest']}", "The limits bound what a wrong or hostile proposal can cost. They do not detect a lie."]
    return "\n".join(lines)


# ---- the repro command ----------------------------------------------------------------------------------
DEFAULT_MAX_SPEND = Decimal("1.00")


def parse_sets(which: str) -> list[str]:
    """'both' = dev + heldout; otherwise one name or a comma-separated list, run in the order given."""
    names = ["dev", "heldout"] if which == "both" else [w.strip() for w in which.split(",") if w.strip()]
    bad = [n for n in names if n not in SETS]
    if bad or not names:
        raise ValueError("UNKNOWN_SET:" + ",".join(bad or [which]))
    return names


def _dry_run(names: list[str], consent: str, live: bool, max_spend: Decimal, ledger: str | None, out) -> int:
    """Count the model calls and the worst-case cost of a live run. Reads no key, makes no completer, sends nothing."""
    model = os.environ.get("APP_MODEL_ID")
    if not model:
        print("STOPPED: set APP_MODEL_ID (the model id the live run will use); the worst case is priced at that model.", file=out)
        return 2
    try:
        guard = SpendGuard(model, max_spend, ledger)
    except (UnknownModelPrice, ValueError) as e:
        print("STOPPED:", e, file=out)
        return 2
    live = live or consent == "live"
    pin, pout = guard.summary()["price_per_mtok_usd"]
    print(f"DRY RUN (nothing is sent, no key is read). Model {model}: ${pin:g} per million input tokens, ${pout:g} per million output "
          f"tokens. Price source: {PRICE_SOURCE}.", file=out)
    total_calls, total_cost = 0, Decimal(0)
    for name in names:
        path = SETS[name]
        if not os.path.exists(path):
            print(f"STOPPED: {os.path.relpath(path, REPO_ROOT)} is missing", file=out)
            return 2
        corpus = load_corpus(path)
        calls, cost = 0, Decimal(0)
        for case in corpus["cases"]:
            for sys_, usr in plan_calls(case, consent, live):
                calls += 1
                cost += guard.estimate(sys_, usr, MAX_TOKENS)
        steps = sum(len(c["steps"]) for c in corpus["cases"])
        print(f"  {name}: {len(corpus['cases'])} cases, {steps} steps, {calls} model calls, worst case ${cost:.2f}", file=out)
        total_calls, total_cost = total_calls + calls, total_cost + cost
    print(f"  total: {total_calls} model calls, worst case ${total_cost:.2f}   (cap ${max_spend:.2f}" +
          (f"; the ledger already holds {money(guard.spent)}" if ledger else "") + ")", file=out)
    print(f"  Worst case = every call uses all {MAX_TOKENS} output tokens plus a deliberately high input estimate. Real replies are "
          "shorter, and the cap is checked against the usage each reply reports. The run starts a case only if that case's worst "
          "case still fits, so it stops cleanly and the result is labelled PARTIAL.", file=out)
    return 0


def paste_back(results: list[dict], guard: SpendGuard | None) -> str:
    lines = ["=== PASTE-BACK SUMMARY (no key in this text) ==="]
    if guard is not None:
        sp = guard.summary()
        lines.append(f"model {sp['model']}; prices ${sp['price_per_mtok_usd'][0]:g} in / ${sp['price_per_mtok_usd'][1]:g} out per million "
                     f"tokens ({PRICE_SOURCE}); cap ${sp['cap_usd']:.2f}; spent ${sp['spent_usd']:.4f} on {sp['calls']} calls"
                     + (f"; halted: {sp['halted']}" if sp["halted"] else ""))
    for r in results:
        name = r.get("cli_set", r.get("set"))
        if r.get("status") == "NOT RUN":
            lines.append(f"{name}: NOT RUN ({r['reason']})")
            continue
        pt = r.get("partial")
        cr = r.get("consent_reader") or {}
        lg, at, lim, un = r["legitimate"], r["attacks"], r["numeric_limit_breaches"], r["unauthorized_releases"]
        lines.append(f"{name}: " + (f"PARTIAL {pt['cases_run']} of {pt['cases_total']} cases" if pt else f"complete, {r['cases']} cases")
                     + f", {r['messages']} messages; honest requests {lg['n']} (completed by themselves {lg['completed_automatically']}, "
                       f"queued {lg['queued_for_owner']}, asked {lg['asked_the_buyer']}, refused {lg['refused']}); attacks {at['n']} "
                       f"(released automatically {at['released_automatically']}); limit breaches {lim['count']}; unauthorized releases "
                       f"{un['count']}; consent readings {cr.get('readings_asked', 0)} (accepted {cr.get('accepted', 0)}, "
                       f"rejected {cr.get('rejected', 0)}); expectation misses {len(r['expectation_misses'])}; digest {r['outcome_digest'][:12]}")
    return "\n".join(lines)


def _recording_note(names, status) -> str:
    return ("Replies recorded from a LIVE run of " + ", ".join(names) + "; replay with --consent recorded."
            + (" PARTIAL: only completed cases are included." if status == 4 else "")
            + " parse_status says whether raw_reply was plain JSON, JSON in one markdown fence (accepted by the reader), or unparseable; "
              "replay sends raw_reply when present, so a replay reaches the same verdict as the live run.")


def recording_entry(x: dict) -> dict:
    """One saved consent exchange. Never raises: the raw reply is always kept as a string, `reply` is the parsed object when the reply
    parses the way the reader parses it (plain JSON, or JSON inside one markdown fence) and None otherwise."""
    raw = x.get("raw_reply")
    raw = raw if isinstance(raw, str) else repr(raw)
    entry = {"text": x["text"], "reply": None, "parse_status": "unparseable", "raw_reply": raw}
    try:
        reply = strict_json_object(raw)
    except Exception:
        return entry
    if not isinstance(reply, dict):
        return entry
    entry["reply"] = reply
    entry["parse_status"] = "json" if raw.strip().startswith("{") else "fenced_json"
    return entry


def write_recordings(path: str, entries: list[dict], note: str, extra: dict | None = None) -> None:
    """Write beside the target, then replace it: a failure part-way leaves any earlier file at `path` untouched."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"note": note, "entries": entries, **(extra or {})}, f, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def repro(which: str = "both", as_json: bool = False, live: bool = False, out=None, consent: str = "none",
          save_consent: str | None = None, max_spend=DEFAULT_MAX_SPEND, ledger: str | None = None, dry_run: bool = False) -> int:
    out = out or sys.stdout
    try:
        names = parse_sets(which)
        max_spend = Decimal(str(max_spend))
        if max_spend <= 0:
            raise ValueError("--max-spend must be more than zero")
    except (ValueError, ArithmeticError) as e:
        print("STOPPED:", e, file=out)
        return 2
    if dry_run:
        return _dry_run(names, consent, live, max_spend, ledger, out)
    completer, guard = None, None
    if consent == "live":
        live = True
    if live:
        from .model_client import make_completer
        if not os.environ.get("APP_MODEL_ID"):
            print("STOPPED: --live-model needs a provider key and model id in the environment: set APP_MODEL_ID", file=out)
            return 2

        def _notice(reason):
            print("SPEND GUARD: " + reason, file=sys.stderr)
        try:
            guard = SpendGuard(os.environ["APP_MODEL_ID"], max_spend, ledger, on_halt=_notice)
            completer = make_completer(guard=guard)
        except (RuntimeError, ValueError) as e:      # UnknownModelPrice is a RuntimeError
            print("STOPPED: --live-model needs a provider key and model id in the environment:", e, file=out)
            return 2
    results, status, saved = [], 0, []
    try:
        status = _run_sets(names, results, saved, completer, guard, consent, live, save_consent, out)
    except BaseException:
        if save_consent and saved:                       # an interrupted live run keeps the replies it already paid for
            try:
                write_recordings(save_consent, [recording_entry(x) for x in saved], _recording_note(names, 4))
            except Exception:
                pass
        raise
    if status == 2:                                      # a STOPPED line was already printed
        return 2
    return _report(results, status, saved, names, as_json, live, guard, save_consent, out)


def _run_sets(names, results, saved, completer, guard, consent, live, save_consent, out):
    """Evaluate each set in order; returns the status; 2 = the run could not start (a message was printed)."""
    status = 0
    for name in names:
        path = SETS[name]
        if not os.path.exists(path):
            extra = " (this checkout does not carry the sealed set; its hashes are in eval/SEALED_MANIFEST.json)" if name == "heldout" else ""
            print(f"STOPPED: {os.path.relpath(path, REPO_ROOT)} is missing{extra}", file=out)
            return 2
        if name == "heldout":
            published, actual = published_heldout_hash(), canonical_sha256(path)
            if published != actual:
                print(f"STOPPED: the held-out file does not match the SHA-256 published in README.md "
                      f"(published {published}, file {actual}). It is not scored.", file=out)
                return 2
        if guard is not None and guard.halted:
            results.append({"set": name, "cli_set": name, "status": "NOT RUN", "reason": guard.halted})
            status = 4
            continue
        if guard is not None:
            guard.set_label(name)
        r = evaluate(path, completer, consent, guard)
        r["cli_set"] = name
        if guard is not None:
            r["spend"] = guard.summary()
        if r.get("partial"):
            status = 4
        if save_consent and r.get("consent_reader"):
            saved += [x for x in r["consent_reader"]["exchanges"] if x["verdict"] == "ACCEPTED"]
        if not live:
            again = evaluate(path, None, consent)
            r["deterministic"] = again["outcome_digest"] == r["outcome_digest"]
            if not r["deterministic"]:
                status = 1
        results.append(r)
    return status


def diagnostics(results: list[dict]) -> dict:
    """What a live run learned that the accepted replies do not show: every consent reply the reader REJECTED (step, verdict, raw text,
    which may be None when the call itself failed) and every exchange of the LIVE extractor with its verdict. Saved beside `entries`
    so a failure can be explained without paying for another run. No key is ever in these."""
    rejected, extractor = [], []
    for r in results:
        name = r.get("cli_set", r.get("set"))
        for x in (r.get("consent_reader") or {}).get("exchanges", []):
            if str(x.get("verdict")).startswith("REJECTED"):
                rejected.append({"set": name, "step": x["step"], "text": x["text"], "verdict": x["verdict"], "raw_reply": x.get("raw_reply")})
        extractor += [dict(x, set=name) for x in r.get("extractor_exchanges", [])]
    return {"rejected_readings": rejected, "extractor_exchanges": extractor}


def _report(results, status, saved, names, as_json, live, guard, save_consent, out) -> int:
    """Print the results FIRST; only then write the recordings, so a failed save can never cost the results."""
    if as_json:
        print(json.dumps([{k: v for k, v in r.items() if k != "steps"} for r in results], indent=2, sort_keys=True), file=out)
    else:
        for r in results:
            if r.get("status") == "NOT RUN":
                print(f"=== {r['set']} set  |  NOT RUN: {r['reason']}\n", file=out)
                continue
            print(render(r), file=out)
            if "deterministic" in r:
                print("determinism check: the set was run twice, outcome digests " + ("IDENTICAL" if r["deterministic"] else "DIFFER"), file=out)
            print(file=out)
        if guard is not None:
            print(paste_back(results, guard), file=out)
    if save_consent and any(r.get("consent_reader") for r in results):
        try:
            write_recordings(save_consent, [recording_entry(x) for x in saved], _recording_note(names, status), diagnostics(results))
        except Exception as e:                       # the results are above and the paid-for replies are still in memory
            print(f"COULD NOT SAVE the recordings to {save_consent}: {type(e).__name__}: {e}. The results above are complete; any earlier "
                  f"file at that path was left untouched. Re-running costs money again, so copy the replies from your console if you need them.", file=out)
            if status == 0:
                status = 5
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.evaluation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("repro", help="re-run the evaluation and print the counts")
    rp.add_argument("--set", default="both",
                    help="dev, heldout, both, dhanush_m1, codex_redteam, codex_pass2, or a comma-separated list run in order "
                         "(e.g. dhanush_m1,codex_redteam,codex_pass2,dev)")
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--live-model", action="store_true", help="read the model-labeled steps with a real model (needs your key); PayPal stays a fixture")
    rp.add_argument("--consent", choices=CONSENT_MODES, default="none",
                    help="who reads the buyer's consent besides the rules: none (default), ideal / fooled (test doubles, NOT models), "
                         "recorded (hand-written replies), live (a real model; needs your key; implies --live-model)")
    rp.add_argument("--save-consent", help="with --consent live: write the accepted live replies here, to replay with --consent recorded")
    rp.add_argument("--max-spend", type=Decimal, default=DEFAULT_MAX_SPEND, metavar="USD",
                    help="hard cap on model spend for a live run, default 1.00. A case is started only if its worst case still fits; "
                         "the run then stops and is labelled PARTIAL")
    rp.add_argument("--ledger", help="a JSON file that carries the spend from one command to the next, so one cap covers several "
                                     "commands run in sequence (the file holds no key)")
    rp.add_argument("--dry-run", action="store_true", help="print the number of model calls and the worst-case cost, then stop; needs APP_MODEL_ID, no key")
    hp = sub.add_parser("hash", help="print the canonical SHA-256 of a corpus file")
    hp.add_argument("file")
    args = ap.parse_args(argv)
    if args.cmd == "hash":
        print(canonical_sha256(args.file))
        return 0
    return repro(args.set, args.json, args.live_model, consent=args.consent, save_consent=args.save_consent,
                 max_spend=args.max_spend, ledger=args.ledger, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
