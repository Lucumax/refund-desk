"""Sprint 3h: the order and the amount a message names are read from the same canonical text consent was judged on. Fixture only, no network."""
import pytest

from app.consent import binding_problem, canonical_orders, named_orders, raw_orders

F1 = "The notebook arrived damaged. Please issue a full refund of ＄1.00."          # U+FF04 full-width dollar sign
F1_CIRCLED = "The notebook arrived damaged. Please issue a full refund of ⑨ dollars."   # circled digit nine
F2 = "Order ORD-1001 arrived damaged. Please refund $18.00 for order ORD-1002."
F2_GREEK = "The mug arrived damaged. Please refund $18.00 for order ΟRD-1002."       # Greek capital omicron in ORD
F2_CYRILLIC = "The mug arrived damaged. Please refund $18.00 for order ОRD-1002."    # Cyrillic capital O in ORD


def nothing_moved(env):
    assert env.fx.refund_posts == 0 and env.fx.refunds_created == 0


# ---- F1: a hidden amount ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [F1, F1_CIRCLED])
def test_f1_a_figure_only_the_canonical_text_sees_is_not_trusted(text):
    assert binding_problem(text, "ORD-1004") == "PARSE_MISMATCH_AMOUNT"


@pytest.mark.parametrize("text", [F1, F1_CIRCLED])
def test_f1_the_codex_string_no_longer_refunds_the_whole_order_by_itself(env, text):
    env.msg(text, "ORD-1004")                                  # a $15 order: the bug refunded all $15
    nothing_moved(env)


def test_f1_the_gate_holds_it_for_the_owner_under_g17(env):
    from app.arena_gate import decide, Context
    from app.arena_reader import Proposal, REFUND
    from app.arena_policy import Policy
    order = env.svc.orders.get("ORD-1004")
    prop = Proposal(REFUND, 1500, "refund_damaged", "test", {"issue": "damaged"})
    g = decide(F1, "ORD-1004", order, prop, Policy(), Context(remaining_minor=1500))
    assert g.decision == "ESCALATE" and g.rule_id == "G17"


def test_f1_plain_ascii_amounts_still_release(env):
    # "a full refund of $1.00" on a $15.00 order is a conflict since the Codex review of 2026-11-01 (F3); the plain wording is used
    env.msg("The notebook arrived damaged. Please issue a refund of $1.00.", "ORD-1004")
    assert env.fx.refund_posts == 1 and env.fx.refunded_minor("FIXCAP-1004") == 100    # not held by the binding check
    assert binding_problem("The notebook arrived damaged. Please issue a refund of $1.00.", "ORD-1004") is None


# ---- F2: a second order in the command ------------------------------------------------------------------------------
def test_f2_two_distinct_orders_are_held():
    assert named_orders(F2) == ["ORD-1001", "ORD-1002"]
    assert binding_problem(F2, "ORD-1001") == "SEVERAL_ORDERS"


def test_f2_the_codex_string_does_not_refund_the_linked_order(env):
    env.msg(F2, "ORD-1001")
    nothing_moved(env)


@pytest.mark.parametrize("text", [F2_GREEK, F2_CYRILLIC])
def test_f2_a_look_alike_letter_inside_ord_still_names_the_other_order(text):
    assert canonical_orders(text) == ["ORD-1002"] and raw_orders(text) == []
    assert binding_problem(text, "ORD-1001") == "ORDER_MISMATCH"


@pytest.mark.parametrize("text", [F2_GREEK, F2_CYRILLIC])
def test_f2_the_greek_and_cyrillic_variants_do_not_move_money(env, text):
    env.msg(text, "ORD-1001")
    nothing_moved(env)


def test_f2_the_same_order_named_twice_is_not_several_orders():
    assert binding_problem("Order ORD-1001 arrived damaged. Please refund $18.00 for order ORD-1001.", "ORD-1001") is None


def test_f2_order_number_form_is_read_by_the_canonical_parse():
    assert canonical_orders("I want a refund for order number 1002.") == ["ORD-1002"]
    assert binding_problem("I want a refund for order number 1002.", "ORD-1001") == "ORDER_MISMATCH"
    assert binding_problem("I want a refund for order number 1001.", "ORD-1001") is None


def test_an_ordinary_message_is_untouched():
    t = "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00."
    assert binding_problem(t, "ORD-1001") is None


# ---- F3: a failed read-back never leads to a second refund request ------------------------------------------------------
def test_f3_the_live_script_sends_no_replay_when_the_read_back_fails(capsys, monkeypatch):
    import os, sys
    from app.fixture_transport import FixturePayPal
    from app.service import Service
    from conftest import ROOT
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import live_sandbox_run
    monkeypatch.setattr(Service, "read_back", lambda self, intent_id: {"ok": False, "reason": "FORCED_FOR_TEST", "checks": []})
    fx = FixturePayPal({"CAP-LIVE": 5_000})
    env = {"PAYPAL_CLIENT_ID": "id-from-env", "PAYPAL_CLIENT_SECRET": "secret-from-env", "PAYPAL_ENV": "sandbox"}
    code = live_sandbox_run.main(["--capture-id", "CAP-LIVE", "--amount", "1.00", "--yes"], env=env, transport=fx)
    out = capsys.readouterr().out
    assert code == 1
    assert fx.refund_posts == 1 and fx.refunds_created == 1      # the one ordinary send; the old script made a second POST here
    assert "UNKNOWN" in out and "No same-key replay was sent" in out


# ---- F4: the G17 words and the A14 narration say what the code does -------------------------------------------------------
def test_f4_the_g17_texts_no_longer_say_the_buyer_did_not_ask_for_a_refund():
    import json, os
    from conftest import ROOT
    strings = json.load(open(os.path.join(ROOT, "copy", "strings.draft.json"), encoding="utf-8"))["strings"]
    for key in ("gate.G17", "arena.rule.G17"):
        assert "do not ask for a refund" not in strings[key], key
        assert "plain, unconditional request" in strings[key], key


def test_f4_a14_without_the_flags_is_still_held_for_the_owner_under_g17(env):
    from app.arena_presets import BY_ID
    from app.arena_gate import decide, Context
    from app.arena_reader import Proposal, REFUND
    from app.arena_policy import Policy
    p = BY_ID["A14"]
    order = env.svc.orders.get(p["order"])
    prop = Proposal(REFUND, 2000, "refund_not_received", "test", {"issue": "never arrived"})
    off = decide(p["text"], p["order"], order, prop, Policy(), Context(remaining_minor=order.total_minor), scan_flags=False)
    on = decide(p["text"], p["order"], order, prop, Policy(), Context(remaining_minor=order.total_minor))
    assert (off.decision, off.rule_id) == ("ESCALATE", "G17") and (on.decision, on.rule_id) == ("ESCALATE", "G10")


def test_f4_the_demo_script_does_not_claim_a14_would_have_been_allowed():
    import os
    from conftest import ROOT
    s = open(os.path.join(ROOT, "DEMO_SCRIPT.md"), encoding="utf-8").read()
    assert "the same $20 would have been allowed" not in s and "would still hold it for the owner (G17)" in s


# ---- F5: the README credits Xin accurately ----------------------------------------------------------------------------------
def test_f5_the_readme_credits_xin_and_no_longer_says_nobody_contributed():
    import os
    from conftest import ROOT
    s = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    assert "No collaborator has contributed code" not in s
    assert "Xin Liang Chia" in s and "regression-test files" in s and "adapter" in s


# ---- found during the 3h self-check (the first re-audit was cut off by the reviewer's own content filter) ------------------
def test_a_thousands_comma_cannot_hide_an_order_or_shrink_an_amount_from_the_binding_check():
    # consent removes the comma ("ORD-1,002" is ORD-1002; "1,001 dollars" is 1001 dollars); the terms must be read the same way
    assert canonical_orders("I want a refund for order ORD-1,002.") == ["ORD-1002"]
    assert binding_problem("I want a refund for order ORD-1,002.", "ORD-1001") == "ORDER_MISMATCH"
    from app.consent import canonical_amounts, raw_amounts
    assert canonical_amounts("Please refund 1,001 dollars.") == raw_amounts("Please refund 1,001 dollars.") == [100100]   # 3i: read alike
    assert binding_problem("Please refund $1,800.00.", "ORD-1001") is None            # an ordinary comma amount is fine


def test_the_comma_order_does_not_refund_the_linked_order(env):
    env.msg("I want a refund for order ORD-1,002.", "ORD-1001")
    nothing_moved(env)


def test_consent_and_binding_read_the_same_terms_across_unicode_and_comma_variants():
    """Differential check: for every variant that passes consent and passes the binding check, the orders consent's own
    templates see are only the linked order and the amounts are the ones the raw text states."""
    import re
    from app import consent as C
    bases = ["I want a refund for order ORD-1001.", "Refund me $18.00.", "Please refund $1,800.00 for order #1001.",
             "Order ORD-1001 arrived damaged. Please refund 18 dollars.", "give me a full refund for order number 1001"]
    swaps = {"0": "０", "1": "①", "8": "８", "$": "＄", "#": "＃", "-": "−", "O": "Ο", "D": "Д",
             ",": "，", ".": "．"}
    variants = set(bases)
    for b in bases:
        for i, ch in enumerate(b):
            if ch in swaps:
                variants.add(b[:i] + swaps[ch] + b[i + 1:])
            if ch.isdigit():
                variants.add(b[:i] + "," + b[i:])
    checked = 0
    for t in sorted(variants):
        if not C.is_plain_refund_command(t) or C.binding_problem(t, "ORD-1001") is not None:
            continue
        checked += 1
        view = C.strip_thousands(C.canonical(t))
        view = re.sub(r"(?<=\d)\.(?=\d)", "_", view)
        orders = {"ORD-" + (m.group(1) or m.group(2) or m.group(3))
                  for m in re.finditer(r"ord-?(\d{3,6})|#(\d{3,6})|order (?:number |no |num )?(\d{3,6})", view)}
        assert orders <= {"ORD-1001"}, t
        amounts = sorted(int(w) * 100 + int((f or "0").ljust(2, "0")) for w, f in
                         ((m.group(1) or m.group(3), m.group(2) or m.group(4)) for m in
                          re.finditer(r"\$(\d+)(?:_(\d{1,2}))?|(\d+)(?:_(\d{1,2}))? (?:dollars|usd)", view)))
        assert amounts == C.raw_amounts(t), t
    assert checked >= 5


# ---- second audit (R2-1 to R2-4) ------------------------------------------------------------------------------------------
R21_FULL = "The notebook arrived damaged. Please issue a full refund of 1_50 dollars."
R21_SUFFIX = "The notebook arrived damaged. Please refund $1_50."
R22_HUGE = "The notebook arrived damaged. Please refund $11111111111111111111111111111."


@pytest.mark.parametrize("text", [R21_FULL, R21_SUFFIX])
def test_r2_1_a_buyers_own_underscore_is_not_a_decimal_mark(text):
    from app.consent import canonical, is_plain_refund_command
    assert canonical(text) is None and not is_plain_refund_command(text)


@pytest.mark.parametrize("text", [R21_FULL, R21_SUFFIX])
def test_r2_1_the_codex_strings_move_no_money(env, text):
    env.msg(text, "ORD-1004")
    nothing_moved(env)


def test_r2_1_a_forced_reader_that_proposes_the_whole_order_is_held_too(env):
    from app.arena_gate import decide, Context
    from app.arena_reader import Proposal, REFUND
    from app.arena_policy import Policy
    order = env.svc.orders.get("ORD-1004")
    g = decide(R21_FULL, "ORD-1004", order, Proposal(REFUND, 1500, "refund_damaged", "test", {"issue": "damaged"}), Policy(),
               Context(remaining_minor=1500))
    assert g.decision != "ALLOW"


def test_r2_2_an_enormous_figure_is_refused_not_a_crash(env):
    from app.extractor import find_amounts
    from app.money import ABSURD_MINOR, to_minor
    assert find_amounts(R22_HUGE)[0][0] == ABSURD_MINOR
    with pytest.raises(ValueError):
        to_minor("1" * 29)
    env.msg(R22_HUGE, "ORD-1004")          # must return normally, with a refusal or a question, and move nothing
    nothing_moved(env)


def test_r2_3_the_script_reports_the_signed_outcome_not_a_guess(capsys, monkeypatch):
    import os, sys
    from app.fixture_transport import FixturePayPal
    from conftest import ROOT
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import live_sandbox_run
    fx = FixturePayPal({"CAP-LIVE": 5_000})
    fx.read_unavailable = True               # the refund went through; only the verification read fails
    env = {"PAYPAL_CLIENT_ID": "id-from-env", "PAYPAL_CLIENT_SECRET": "secret-from-env", "PAYPAL_ENV": "sandbox"}
    code = live_sandbox_run.main(["--capture-id", "CAP-LIVE", "--amount", "1.00", "--yes"], env=env, transport=fx)
    out = capsys.readouterr().out
    assert code == 1 and fx.refund_posts == 1
    assert "signed outcome: SUCCEEDED" in out and "the outcome is UNKNOWN" not in out and "No same-key replay was sent" in out


# R2-4's path check moved to tests/test_repo_hygiene.py (L2), which uses a wider detector (tests/path_guard.py).
