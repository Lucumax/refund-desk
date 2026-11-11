"""Live diagnostic 2026-11-02 (claude-haiku-5-5, Dhanush's M1 development set): 21 of 22 extractor replies were thrown away as
ORDER_NOT_IN_MESSAGE although the model had quoted the buyer's own words. The buyers there name their payment by a 17-character
PayPal transaction id ("My order RQJ76PU7RTWBRJROM was missing one item."), not by ORD-1234, and the validator only knew ORD-style
references. These tests use the real replies (tests/fixtures), offline, and keep every safety property.

What an extractor order reference can and cannot do: it is DISPLAY ONLY. The order a request is about is the one the buyer's link
proves; money moves only through the rules and the gate. The validator only decides whether a quoted reference is really in the
buyer's words."""
import json
import os

import pytest

from conftest import build_env
from app.extractor import ModelExtractor
from app.scripted import model_reply

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = json.load(open(os.path.join(HERE, "fixtures", "haiku_extractor_exchanges_2026-11-02.json"), encoding="utf-8"))["exchanges"]
BY_STEP = {x["step"]: x for x in FIX}
REJECTED_ON_ORDER = [x["step"] for x in FIX if x["verdict_old_validator"] == "REJECTED:ORDER_NOT_IN_MESSAGE"]


def _extract(raw, message):
    return ModelExtractor(lambda user, system=None: raw).extract(message)


def _reply(order, quote, **kw):
    return model_reply(order, quote, **kw)


TXN = "RQJ76PU7RTWBRJROM"
MSG = f"My order {TXN} was missing one item. Please refund me $15.00."


def test_the_fixture_is_the_live_diagnostic_21_of_22():
    assert len(FIX) == 22 and len(REJECTED_ON_ORDER) == 20
    assert {x["verdict_old_validator"] for x in FIX} == {"REJECTED:ORDER_NOT_IN_MESSAGE", "ACCEPTED", "REJECTED:STOP_REASON:refusal"}


# After the fix the reference is no longer the reason. Four replies still fail, each on a DIFFERENT validator that is doing its job
# (the model's own amount, quote or issue did not hold up); they are listed so that no reply is quietly lost or quietly widened.
STILL_REJECTED_FOR_OTHER_REASONS = {"M1-A04.1": "AMOUNT_NOT_IN_MESSAGE", "M1-B03.1": "AMOUNT_NOT_TIED_TO_REFUND",
                                    "M1-B09.1": "ISSUE_NOT_SUPPORTED_BY_QUOTE", "M1-B10.1": "ISSUE_NOT_SUPPORTED_BY_QUOTE"}


@pytest.mark.parametrize("step", REJECTED_ON_ORDER)
def test_a_live_reply_that_quotes_the_buyers_own_reference_is_no_longer_thrown_away_for_the_reference(step):
    x = BY_STEP[step]
    m = ModelExtractor(lambda user, system=None: x["raw_reply"])
    f = m.extract(x["message"])
    if step in STILL_REJECTED_FOR_OTHER_REASONS:
        assert m.last["verdict"] == "REJECTED:" + STILL_REJECTED_FOR_OTHER_REASONS[step]
    else:
        assert m.last["verdict"] == "ACCEPTED" and f.source == "model" and "MODEL_OUTPUT_REJECTED" not in f.notes


def test_sixteen_of_the_twenty_reference_rejections_are_now_accepted():
    assert len(REJECTED_ON_ORDER) - len(STILL_REJECTED_FOR_OTHER_REASONS) == 16


def test_a_transaction_id_is_not_taken_for_an_order():
    f = _extract(_reply(TXN, TXN), MSG)
    assert f.source == "model" and f.order_ref is None          # it names a payment; the order stays the one the link proves
    assert f.evidence.get("transaction_id") == TXN


def test_the_whole_quote_with_words_around_the_reference_is_fine():
    f = _extract(_reply(TXN, "My order " + TXN + " was missing one item."), MSG)
    assert f.source == "model" and f.order_ref is None


def test_a_reference_in_another_format_is_accepted_when_verbatim_and_never_becomes_an_order():
    msg = "Invoice INV-77421 arrived cracked, refund $9.00 please."
    f = _extract(_reply("INV-77421", "Invoice INV-77421"), msg)
    assert f.source == "model" and f.order_ref is None and f.evidence.get("reference") == "Invoice INV-77421"


def test_an_ord_style_reference_still_works_as_before():
    f = _extract(_reply("ORD-1001", "ORD-1001"), "ORD-1001 is damaged, refund $18.00")
    assert f.source == "model" and f.order_ref == "ORD-1001"


# ---- every safety property stays --------------------------------------------------------------------------------------
@pytest.mark.parametrize("order,quote,message", [
    ("QQQ111WWW222EEE33", "QQQ111WWW222EEE33", MSG),                 # a reference that is not in the buyer's words
    (TXN, "My order was missing one item.", MSG),                     # claimed, but the quote does not carry it
    (TXN, TXN.lower(), MSG),                                          # case-sensitive: a lower-case copy is not the buyer's token
    (TXN.lower(), TXN.lower(), MSG),
    (TXN[:-1], TXN[:-1], MSG),                                        # a piece of a longer token is not the token
    (TXN + "9", TXN + "9", MSG),
    (TXN, TXN, MSG.replace(TXN, "X" + TXN)),                          # the buyer's token is longer than the claim
    ("ORD-9999", "ORD-9999", "ORD-1001 is damaged"),                  # an invented ORD reference (existing test, kept)
    ("12", "12", "refund 12 dollars for the mug, order 12"),          # too short / no letters: not a reference
    ("broken", "broken", "The item was broken, refund $9.00"),        # an ordinary word with no digit is not a reference
    ("RQJ76 PU7RTWBRJROM", "RQJ76 PU7RTWBRJROM", "My order RQJ76 PU7RTWBRJROM was missing"),    # not one token
    (TXN, TXN, MSG + " ORD-1002"),                                    # the message also names an order: the reference is not the order
])
def test_a_reference_that_is_not_the_buyers_own_token_is_still_rejected(order, quote, message):
    f = _extract(_reply(order, quote), message)
    assert "MODEL_OUTPUT_REJECTED" in f.notes and f.source == "rules", (order, quote)


def test_a_non_string_reference_is_rejected():
    for bad in (1234, ["ORD-1001"], {"a": 1}, True):
        f = _extract(json.dumps({**json.loads(model_reply()), "order_ref": bad, "order_quote": "ORD-1001"}), "ORD-1001 is damaged")
        assert "MODEL_OUTPUT_REJECTED" in f.notes


def test_a_reference_quote_longer_than_the_limit_or_injected_around_it_is_rejected():
    f = _extract(_reply(TXN, MSG * 20), MSG)
    assert "MODEL_OUTPUT_REJECTED" in f.notes


def test_the_unaccepted_reference_changes_no_money_decision_end_to_end(tmp_path):
    """A model that names an order the buyer never wrote cannot move money, and a model that names the buyer's own transaction
    id (accepted now) still leaves the decision to the rules: the unknown-order message is blocked and released nothing."""
    env = build_env(tmp_path / "d", extractor=ModelExtractor(
        lambda user, system=None: _reply(TXN, TXN, issue="refund_damaged", issue_quote="was broken", intent_quote="refund",
                                         amount=15, amount_quote="$15.00")))
    msg = f"Order ORD-1001 regarding {TXN} was broken. Please refund $15.00."
    env.msg(msg)
    assert env.fx.refund_posts == 0                                   # a transaction id that is not the order's capture: asked, never paid
