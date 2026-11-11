"""Shared by the tests that replay the committed LIVE recordings offline. The live runs happened under the policy before 2026-11-02
(a message citing another transaction id was refused or sent back to the buyer; a figure in another currency was sent back or held).
tests/fixtures/live_replay_before_2026-11-02_policy.json holds the per-step outcomes of those runs (replayed by the code at 31ffa01,
digests equal to the live runs' printed digests). Today's replay must agree with them on every step except the ones a test lists, and
each listed step must show the before and the after, so a changed outcome is never silent."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(HERE, "fixtures", "live_replay_before_2026-11-02_policy.json")


def live_run(key: str) -> dict:
    """{'digest': <the live run's digest, 12 hex>, 'steps': {step id: [class, rule, amount_minor]}}"""
    return json.load(open(_PATH, encoding="utf-8"))["runs"][key]


def today(result: dict) -> dict:
    return {s["id"]: [s["observed"], s["rule"], s["amount_minor"]] for s in result["steps"]}


def differences(key: str, result: dict) -> dict:
    """{step id: (live outcome, today's outcome)} for every step whose replayed outcome is not the live run's."""
    live, now = live_run(key)["steps"], today(result)
    assert set(live) == set(now), "the replay covers different steps than the live run"
    return {i: (live[i], now[i]) for i in live if live[i] != now[i]}


# The steps whose replayed outcome changed because of the 2026-11-02 policy change, as (live outcome, today's outcome). Anything
# else differing from a live run fails the tests that use these.
M1_CHANGES = {
    "M1-A11.1": (["ASKED", "ORDER_MISMATCH", None], ["OWNER_QUEUE", "G19", 2500]),       # a different transaction id: owner, not the buyer
    "M1-A11-C.1": (["REFUSED", "G3", 2500], ["OWNER_QUEUE", "G19", 2500]),               # the same with a taken-over reader: owner, not refused
    "M1-B08.1": (["ASKED", "CURRENCY_NOT_SUPPORTED", None], ["OWNER_QUEUE", "G17", 4860]),   # 45.00 euros = $48.60: judged, not asked (the consent rules hold "while traveling", with every reader)
}
PASS2_CHANGES = {
    "PX-22.1": (["ASKED", "MISSING_AMOUNT", None], ["OWNER_QUEUE", "G17", 1944]),        # 18 euros = $19.44 (now labelled an ordinary request)
    "PX-24.1": (["ASKED", "ORDER_MISMATCH", None], ["OWNER_QUEUE", "G19", 1800]),        # a different transaction id: owner
    "PX-24-C.1": (["REFUSED", "G3", 1800], ["OWNER_QUEUE", "G19", 1800]),
}
CHANGES = {"dhanush_m1": M1_CHANGES, "codex_pass2": PASS2_CHANGES, "codex_redteam": {}, "dev": {}}
# the digests of today's replay of the changed sets (the unchanged sets keep the live digest)
TODAY_DIGESTS = {"run1/dhanush_m1": "c6d323c6253f", "run2/dhanush_m1": "6b4b23b43608", "diag/dhanush_m1": "c6d323c6253f",
                 "run1/codex_pass2": "2df243d39253", "run2other3/codex_pass2": "2df243d39253"}
