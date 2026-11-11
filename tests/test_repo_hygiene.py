"""Sprint 3i: fixes from the Opus audit of 3h (2026-10-27): M1 (no pre-filled amount from a parse that was just rejected),
L1 (a thousands comma needs a first group of 1 to 999), L2 (a wider absolute-path guard), L3 (the sealed corpus is an internal
file for the export). Fixture only, no network."""
import os
import subprocess
import sys

import pytest

from conftest import ROOT

M1_CASES = [
    ("The notebook arrived damaged. Please issue a full refund of \uff041.00.", "ORD-1004"),               # full-width dollar sign
    ("The notebook arrived damaged. Please issue a full refund of 1.00 d\u043ellars.", "ORD-1005"),         # Cyrillic o in dollars
    ("The mug arrived damaged. Please refund $1\u2078.00.", "ORD-1005"),                                    # superscript eight
    ("The mug arrived damaged. Please refund $1\uff0c800.", "ORD-1002"),                                    # full-width comma
    ("The notebook arrived damaged. Please issue a full refund of \u2468 dollars.", "ORD-1004"),            # circled nine
]


# ---- M1: the owner never gets a card with the amount of a parse that was just rejected -----------------------------------
@pytest.mark.parametrize("text,order", M1_CASES)
def test_m1_a_held_amount_is_asked_about_not_prefilled(env, text, order):
    env.msg(text, order)
    req = env.latest_req()
    assert req["kind"] == "NEEDS_INFO" and req["code"] == "AMOUNT_UNVERIFIED"
    assert not req.get("intent_id") and not req.get("intent") and not req.get("proposal")
    assert env.fx.refund_posts == 0 and env.fx.refunds_created == 0


@pytest.mark.parametrize("text,order", M1_CASES)
def test_m1_the_owner_sees_both_readings_and_no_approve_action(env, text, order):
    env.msg(text, order)
    req = env.latest_req()
    b = req["binding"]
    assert b["reason"] == "PARSE_MISMATCH_AMOUNT" and "raw_amounts_minor" in b and "canonical_amounts_minor" in b
    assert b["raw_amounts_minor"] != b["canonical_amounts_minor"]
    item = env.item(req["id"])
    assert item["stage"] == "NEEDS_INFO" and not item["actions"] and item["approval"] is None
    assert "it reads as" in item["headline"] and "nothing was proposed" in item["headline"].lower()


def test_m1_the_buyer_is_asked_to_type_the_amount_plainly(env):
    out = env.msg(*M1_CASES[0])
    view = env.get(f"/api/buyer/requests/{out['token']}", headers={}).json()
    assert view["stage"] == "NEEDS_AMOUNT_UNVERIFIED" and "digits" in view["message"].lower()


def test_m1_the_same_words_typed_plainly_still_go_through(env):
    # "a full refund of $1.00" on a $15.00 order is a conflict since the Codex review of 2026-11-01 (F3); the plain wording is used
    env.msg("The notebook arrived damaged. Please issue a refund of $1.00.", "ORD-1004")
    assert env.fx.refund_posts == 1 and env.fx.refunded_minor("FIXCAP-1004") == 100


def test_m1_the_reader_path_does_not_prefill_either(env):
    """A reader that proposes the whole order for the hidden-amount text must not reach the owner card either."""
    from app.arena_reader import Proposal, REFUND

    class Whole:
        kind, name = "test", "whole-order reader"

        def read(self, text, order):
            return Proposal(REFUND, order.total_minor, "refund_damaged", "test", {"issue": "damaged"})

    text, order = M1_CASES[0]
    env.svc.message(text, order, env.svc.buyer_capability(order), reader=Whole())
    req = env.latest_req()
    assert req["kind"] == "NEEDS_INFO" and req["code"] == "AMOUNT_UNVERIFIED" and not req.get("intent_id")
    assert env.fx.refund_posts == 0


# ---- L1: a thousands comma needs a first group of 1 to 999 --------------------------------------------------------------
@pytest.mark.parametrize("text", ["$0,018", "$00,018", "$0,018.00", "$01,800"])
def test_l1_a_leading_zero_group_is_not_thousands(env, text):
    from app.extractor import find_amounts
    assert all(a[0] != 1800 for a in find_amounts("Please refund " + text + "."))
    env.msg("The mug arrived damaged. Please refund " + text + ".", "ORD-1001")
    assert env.fx.refund_posts == 0 and env.fx.refunded_minor("FIXCAP-1001") == 0


def test_l1_ordinary_thousands_commas_still_read_as_thousands():
    from app.extractor import find_amounts
    from app.consent import canonical_amounts, raw_amounts
    for t, minor in (("$1,800", 180000), ("$12,345.67", 1234567), ("$999,999", 99999900), ("1,800 dollars", 180000)):
        assert find_amounts("refund " + t + ".")[0][0] == minor, t
        assert canonical_amounts("refund " + t + ".") == raw_amounts("refund " + t + ".") == [minor], t


def test_l1_both_places_use_one_rule():
    from app import consent
    assert consent.strip_thousands("order 1,002 and $0,018 and $1,800 and 12,345,678") == "order 1002 and $0,018 and $1800 and 12345678"


# ---- L2: the absolute-path guard sees the common spellings ----------------------------------------------------------------
LEAKY = ["C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "x", "C:" + "\\" * 2 + "Users" + "\\" * 2 + "someone", "c:/Users/someone/x",
         "C://Users//x", "D:" + "\\" + "Projects" + "\\" + "app", "E:/Work Files/a b/c", "/c/Users/someone/x", "/d/Projects/x",
         "/home/someone/x", "/Users/someone/x", "AppData" + "\\" + "Local" + "\\" + "Temp", "C:" + "\\" + "Walter" + "\\" + "100 day plan"]
CLEAN = ["https://api-m.sandbox.paypal.com/a", "the path is relative: app/consent.py", "key:value", "http://127.0.0.1:8000/x", "a:b",
         "3:00 PM ET", "tests/conftest.py"]


def test_l2_the_detector_finds_each_spelling_and_ignores_ordinary_text():
    from path_guard import has_local_path
    for s in LEAKY:
        assert has_local_path(s), s
    for s in CLEAN:
        assert not has_local_path(s), s


def test_l2_no_tracked_file_carries_a_local_path_including_names_with_spaces():
    from path_guard import has_local_path
    run = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
    names = [n for n in run.stdout.decode("utf-8").split(chr(0)) if n] if run.returncode == 0 else []
    if not names:
        pytest.skip("not a git checkout (the clean export has no .git)")
    bad = []
    for f in names:
        if f.startswith("vendor/") or f.lower().endswith((".png", ".jpg", ".ico", ".woff", ".woff2", ".gif")):
            continue
        if f in ("tests/path_guard.py", "tests/test_repo_hygiene.py"):
            continue
        p = os.path.join(ROOT, f)
        if not os.path.isfile(p):
            continue
        try:
            text = open(p, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        if has_local_path(text) or has_local_path(f):
            bad.append(f)
    assert bad == []


# ---- L3: the sealed corpus is an internal file for the export ------------------------------------------------------------
def test_l3_the_sealed_corpus_is_listed_as_internal():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import clean_export_check
    assert "eval/heldout_corpus.json" in clean_export_check.INTERNAL
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "clean_export_check.py"), "--list-internal"],
                         cwd=ROOT, capture_output=True, text=True).stdout.split()
    if os.path.exists(os.path.join(ROOT, "eval", "heldout_corpus.json")):
        assert "eval/heldout_corpus.json" in out
