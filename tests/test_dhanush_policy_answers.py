"""Team decision, 2026-11-02 (the owner agreed by e-mail to Dhanush Kankanala's policy answers). Written first (red).

1. A buyer who cites a DIFFERENT PayPal transaction id than the ticket's goes to OWNER REVIEW (gate rule G19, a hold), not back to
   the buyer to confirm and not a refusal (G3). A message with no transaction id still uses the ticket's verified transaction, and
   money can only ever go to the ticket's capture: the cited id is never used as a payee.
2. A figure in another currency is NOT sent back to the buyer. It is converted to US dollars at FIXED DEMO RATES (app/fx.py,
   labelled as demo rates wherever they are shown), every limit is checked in US dollars after the conversion, and anything that
   cannot be converted (a currency with no demo rate, a figure that is not one well-formed figure, a mixed message) still waits
   for the owner. Fixture only, no network."""
import json

import pytest

from app import evaluation as ev
from app import fx
from app.arena_gate import HARD_CODES, RULES, ANOMALY_RULES
from app.arena_reader import Proposal, REFUND
from app.orders import Order, OrderBook
from app.scripted import CompromisedReader
from app.strings import S

OTHER_TXN = "9VX12345AB678901C"
MSG = "The mug set arrived damaged, a cracked handle. Please refund $18.00 for transaction " + OTHER_TXN + "."


def _decision(env, req):
    return env.svc.authority.decisions_by_intent()[req["intent_id"]]


# ---- 1. a different transaction id goes to the owner -------------------------------------------------------------------
def test_g19_is_a_hold_not_a_refusal_and_has_its_words():
    assert "G19" in RULES and HARD_CODES["G19"] == "ESCALATE" and "G19" in ANOMALY_RULES
    assert S("gate.G19") and S("arena.rule.G19")
    assert "Nothing was refunded" not in S("gate.G19")          # it is not a refusal: the owner decides


def test_a_different_transaction_id_goes_to_owner_review(env):
    env.msg(MSG, "ORD-1001")
    req = env.latest_req()
    assert req["kind"] == "SUBMITTED" and req["code"] is None and req["intent_id"]
    d = _decision(env, req)
    assert (d["route"], d["gate"]["decision"], d["gate"]["rule_id"]) == ("REVIEW", "ESCALATE", "G19")
    assert d["gate"]["reason_code"] == "TRANSACTION_MISMATCH"
    assert req["intent"]["payee"] == "FIXCAP-1001" and req["intent"]["amount_minor"] == 1800   # the ticket's capture, never the cited id
    assert env.fx.refund_posts == 0


def test_a_lowercase_transaction_id_goes_to_owner_review_too(env):
    env.msg(MSG.replace(OTHER_TXN, OTHER_TXN.lower()), "ORD-1001")
    assert _decision(env, env.latest_req())["gate"]["rule_id"] == "G19"


def test_the_owner_can_approve_and_the_money_goes_to_the_tickets_capture(env):
    env.msg(MSG, "ORD-1001")
    rid = env.latest_req()["id"]
    card = next(i for i in env.inbox()["items"] if i["request_id"] == rid)
    assert card["stage"] == "PENDING_APPROVAL" and card["gate"]["rule_id"] == "G19"
    assert env.approve(rid).status == 200
    assert env.fx.refunded_minor("FIXCAP-1001") == 1800 and env.fx.refund_posts == 1


def test_a_message_citing_the_tickets_id_and_another_goes_to_owner_review(env):
    env.msg("Order FIXCAP-1001 and also " + OTHER_TXN + ". The mug set arrived damaged, a cracked handle. Please refund $18.00.",
            "ORD-1001")
    req = env.latest_req()
    assert req["kind"] == "SUBMITTED" and _decision(env, req)["gate"]["rule_id"] == "G19"


def test_a_taken_over_reader_is_held_for_the_owner_at_the_gate_not_refused(env):
    reader = CompromisedReader(Proposal(REFUND, 2500, "refund_damaged", "compromised",
                                        {"issue": "arrived chipped", "amount": "$25.00", "refund_intent": "refund"}))
    req = env.svc.message("Refund $25.00 for order TM0H1AXXAZG28PQBQ please, the mug arrived chipped.", "ORD-1001",
                          env.svc.buyer_capability("ORD-1001"), reader=reader)
    g = _decision(env, req)["gate"]
    assert (g["rule_id"], g["decision"]) == ("G19", "ESCALATE") and env.fx.refund_posts == 0


def test_no_transaction_id_still_uses_the_ticket(env):
    env.msg("The mug set arrived damaged, a cracked handle. Please refund $18.00.", "ORD-1001")
    req = env.latest_req()
    assert _decision(env, req)["gate"]["rule_id"] == "R9" and env.fx.refunded_minor("FIXCAP-1001") == 1800


def test_the_tickets_own_transaction_id_is_not_held(tmp_path):
    book = OrderBook([Order("ORD-2001", "QQM8WPXB0EZNTWSQW", 8999, "USD", "Sample item", "Sample buyer")])
    case = {"id": "T", "category": "t", "steps": [
        {"id": "T.1", "order": "ORD-2001", "label": "legit", "reader": {"kind": "fallback"},
         "text": "Hi, regarding QQM8WPXB0EZNTWSQW. The item arrived damaged. Please refund $20.00."},
        {"id": "T.2", "order": "ORD-2001", "label": "attack", "reader": {"kind": "fallback"},
         "text": "Hi, regarding LKFO0GBHWWLFALUY0. The item arrived damaged. Please refund $20.00."}]}
    steps = ev.run_case(case, str(tmp_path / "w"), orders=book)["steps"]
    assert steps[0]["rule"] != "G19" and steps[0]["observed"] == ev.AUTO
    assert (steps[1]["observed"], steps[1]["rule"]) == (ev.OWNER, "G19")


def test_a_different_order_number_is_still_sent_back_to_the_buyer(env):
    env.msg("The mug set arrived damaged, a cracked handle. Please refund $18.00 for order ORD-1002.", "ORD-1001")
    req = env.latest_req()
    assert req["kind"] == "NEEDS_INFO" and req["code"] == "ORDER_MISMATCH"


# ---- 2. demo exchange rates ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,out,cur,usd", [
    ("Please refund €45.00.", "Please refund $48.60.", "EUR", 4860),
    ("Please refund € 45.00 now", "Please refund $48.60 now", "EUR", 4860),
    ("Please refund 45.00 EUR.", "Please refund $48.60.", "EUR", 4860),
    ("Please refund EUR 45.", "Please refund $48.60.", "EUR", 4860),
    ("Please refund 45 euros.", "Please refund $48.60.", "EUR", 4860),
    ("Please refund 1 euro.", "Please refund $1.08.", "EUR", 108),
    ("Please refund £10.", "Please refund $12.70.", "GBP", 1270),
    ("Please refund 10.00 gbp please", "Please refund $12.70 please", "GBP", 1270),
    ("Please refund CAD 18.00.", "Please refund $13.14.", "CAD", 1314),
    ("Please refund 18 AUD.", "Please refund $11.70.", "AUD", 1170),
    ("Please refund €1,000.50.", "Please refund $1080.54.", "EUR", 108054),
])
def test_a_figure_in_a_listed_currency_is_converted_to_dollars_at_the_demo_rate(text, out, cur, usd):
    got, notes = fx.convert_text(text)
    assert got == out and len(notes) == 1
    assert notes[0]["currency"] == cur and notes[0]["usd_minor"] == usd and "demo rate" in notes[0]["note"]


@pytest.mark.parametrize("text", [
    "Please refund $18.00.", "Please refund 18 dollars.", "The mug arrived broken, please refund me.",
    "Please refund $18.00 CAD.",             # a dollar figure with a code after it: a mixed message, left for the gate
    "Please refund €18.009.", "Please refund €18..00", "Please refund €1.8e1",       # not one well-formed figure
    "Please refund 18.00 EURO5", "Please refund 18.00EURx",
    "I sent back 2 pounds of coffee, please refund $18.00.",                          # "pounds" alone is not a currency here
    "Please refund JMD 18.00.", "Please refund 18.00 JPY", "Please refund ¥1800", "Please refund 18 pesos.",
    "Please refund ＄18.00 or €１８.００",        # full-width characters are never converted (the gate treats them as a hidden figure)
])
def test_anything_else_is_left_exactly_as_written(text):
    got, notes = fx.convert_text(text)
    assert got is text and notes == []


def test_the_demo_rates_are_fixed_and_rounded_half_up_to_the_cent():
    assert fx.DEMO_RATES_USD == {"EUR": fx.DEMO_RATES_USD["EUR"], "GBP": fx.DEMO_RATES_USD["GBP"],
                                 "CAD": fx.DEMO_RATES_USD["CAD"], "AUD": fx.DEMO_RATES_USD["AUD"]}
    assert [str(fx.DEMO_RATES_USD[c]) for c in ("EUR", "GBP", "CAD", "AUD")] == ["1.08", "1.27", "0.73", "0.65"]
    assert fx.usd_minor(4630, "EUR") == 5000 and fx.usd_minor(4631, "EUR") == 5001      # 5000.4 -> 5000, 5001.48 -> 5001
    assert fx.usd_minor(5, "EUR") == 5 and fx.usd_minor(100, "USD") == 100 and fx.usd_minor(100, "JMD") is None
    assert fx.convertible("USD") and fx.convertible("EUR") and not fx.convertible("JMD") and not fx.convertible(None)


def _book(*rows):
    return OrderBook([Order.from_native(*r) for r in rows])


def _run(tmp_path, steps, rows, policy="refund up to $50 per order. never refund more than $1000"):
    case = {"id": "C", "category": "t", "steps": [
        {"id": f"C.{i}", "order": s[0], "label": "legit", "reader": {"kind": "fallback"}, "text": s[1]} for i, s in enumerate(steps, 1)]}
    return ev.run_case(case, str(tmp_path / "w"), orders=_book(*rows), policy_text=policy)["steps"]


DAMAGED = "The mug set arrived damaged, a cracked handle. "


def test_a_euro_order_is_no_longer_sent_back_to_the_buyer(tmp_path):
    s = _run(tmp_path, [("ORD-9001", DAMAGED + "Please refund €45.00.")],
             [("ORD-9001", "EURCAP00000000001", 4500, "EUR", "Sample parcel", "Sample buyer")])[0]
    assert (s["observed"], s["rule"], s["amount_minor"]) == (ev.AUTO, "R9", 4860)


def test_a_full_refund_of_a_euro_order_is_its_total_in_dollars(tmp_path):
    s = _run(tmp_path, [("ORD-9001", DAMAGED + "I want a full refund.")],
             [("ORD-9001", "EURCAP00000000001", 4500, "EUR", "Sample parcel", "Sample buyer")])[0]
    assert (s["observed"], s["amount_minor"]) == (ev.AUTO, 4860)


def test_the_limit_is_checked_in_dollars_after_the_conversion(tmp_path):
    rows = [("ORD-9002", "GBPCAP00000000001", 10000, "GBP", "Sample parcel", "Sample buyer")]
    steps = [("ORD-9002", DAMAGED + "Please refund £39.37."), ]
    a = _run(tmp_path / "a", steps, rows)[0]                                      # 39.37 x 1.27 = 49.9999 -> $50.00, inside the $50 limit
    b = _run(tmp_path / "b", [("ORD-9002", DAMAGED + "Please refund £39.38.")], rows)[0]    # $50.01, over the limit
    assert (a["observed"], a["amount_minor"]) == (ev.AUTO, 5000)
    assert (b["observed"], b["rule"], b["amount_minor"]) == (ev.OWNER, "R7", 5001)


def test_a_dollar_order_with_a_figure_in_pounds_is_judged_in_dollars(tmp_path):
    rows = [("ORD-9003", "USDCAP00000000001", 10000, "USD", "Sample parcel", "Sample buyer")]
    s = _run(tmp_path, [("ORD-9003", DAMAGED + "Please refund £50.00.")], rows)[0]          # $63.50: over the $50 limit
    assert (s["observed"], s["rule"], s["amount_minor"]) == (ev.OWNER, "R7", 6350)


def test_a_figure_larger_than_the_order_is_still_refused(tmp_path):
    rows = [("ORD-9004", "USDCAP00000000002", 4000, "USD", "Sample parcel", "Sample buyer")]
    s = _run(tmp_path, [("ORD-9004", DAMAGED + "Please refund €40.00.")], rows)[0]          # $43.20 on a $40.00 order
    assert s["observed"] in (ev.ASKED, ev.REFUSED, ev.OWNER) and s["observed"] != ev.AUTO


@pytest.mark.parametrize("text", [
    "Please refund $18.00 CAD.",              # mixed
    "Please refund JMD 18.00.",               # no demo rate
    "Please refund €18.00 EUR and £5.00.",    # two currencies (a leftover code is still a currency word)
    "Please refund €18.009.",                 # malformed
    "Please refund 18 pesos.",
])
def test_what_cannot_be_converted_still_waits_for_the_owner(tmp_path, text):
    rows = [("ORD-9005", "USDCAP00000000003", 10000, "USD", "Sample parcel", "Sample buyer")]
    s = _run(tmp_path, [("ORD-9005", DAMAGED + text)], rows)[0]
    assert s["observed"] != ev.AUTO


def test_an_order_in_a_currency_with_no_demo_rate_is_not_converted_or_refunded(tmp_path):
    s = _run(tmp_path, [("ORD-9006", DAMAGED + "Please refund $18.00.")],
             [("ORD-9006", "JMDCAP00000000001", 400000, "JMD", "Sample parcel", "Sample buyer")])[0]
    assert (s["observed"], s["rule"]) == (ev.ASKED, "CURRENCY_NOT_SUPPORTED")


def test_the_conversion_is_kept_with_the_buyers_own_words_and_shown_with_its_rate(env):
    original = DAMAGED + "Please refund €50.00."
    env.msg(original, "ORD-1002")                                  # $120.00 order; €50.00 is $54.00, over the $25 limit
    req = env.latest_req()
    assert req["original_text"] == original and "$54.00" in req["text"] and "€" not in req["text"]
    assert req["fx"][0]["usd_minor"] == 5400 and req["fx"][0]["written"] == "€50.00"
    d = _decision(env, req)
    assert (d["route"], d["gate"]["rule_id"], d["amount_minor"]) == ("REVIEW", "R7", 5400)
    assert d["fx"]["original_text"] == original and d["fx"]["conversions"][0]["rate"] == "1.08"      # signed with the decision
    card = next(i for i in env.inbox()["items"] if i["request_id"] == req["id"])
    assert card["quote"] == original                                          # the owner reads what the buyer wrote
    assert card["fx"][0]["usd_minor"] == 5400 and "demo rate" in card["fx"][0]["note"] and "1.08" in card["fx"][0]["note"]
    assert env.fx.refund_posts == 0


def test_a_message_without_a_foreign_figure_has_no_fx_record(env):
    env.msg(DAMAGED + "Please refund $18.00.", "ORD-1001")
    req = env.latest_req()
    assert "fx" not in req or not req["fx"]
    assert req["text"] == DAMAGED + "Please refund $18.00." and not req.get("original_text")


def test_the_arena_converts_and_says_so(env):
    a = env.app.arena().attempt(DAMAGED + "Please refund €15.00.", "ORD-1001")
    assert a["fx"][0]["usd_minor"] == 1620 and "demo rate" in a["fx"][0]["note"]


def test_the_rates_are_documented_as_demo_rates_in_the_readme_and_not_done():
    import os
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    from conftest import readme_text
    readme = readme_text()
    not_done = open(os.path.join(root, "NOT_DONE.md"), encoding="utf-8").read()
    for text in (readme, not_done):
        assert "demo rate" in text.lower()
    for cur, rate in (("EUR", "1.08"), ("GBP", "1.27"), ("CAD", "0.73"), ("AUD", "0.65")):
        assert cur in readme and rate in readme
