"""Demo exchange rates are a SIMULATION feature.

In fixture (demo) mode a figure in euros, pounds, Canadian or Australian dollars is converted at the fixed demo rate (app/fx.py), as
the team decided on 2026-11-02, so "18 euros" is $19.44 and goes out by itself. Against a real PayPal sandbox a refund must be made in
the currency the buyer paid in, and a demo rate is not a market rate, so a message that names another currency is NOT converted and
NOT judged: no proposal is made, nothing is sent, and the owner sees it in the list of requests that need a person."""
import pytest

from app.__main__ import build_sandbox_service
from app.extractor import RuleBasedExtractor
from app.fixture_transport import FixturePayPal
from app.orders import Order, OrderBook
from app.arena_reader import Proposal, REFUND

FIXTURE_ENV = {"PAYPAL_CLIENT_ID": "fixture-id", "PAYPAL_CLIENT_SECRET": "fixture-secret", "PAYPAL_ENV": "sandbox"}
DAMAGED = "Order ORD-9000 arrived damaged. "
FOREIGN = ["Please refund 18 euros.", "Please refund EUR 18.00.", "Please refund 18.00 GBP.", "Please refund €18.00.",
           "Please refund 18 pesos.", "Please refund $18.00 CAD."]


@pytest.fixture
def sandbox(tmp_path):
    fx = FixturePayPal({"CAP-1": 5_000})
    orders = OrderBook([Order("ORD-9000", "CAP-1", 5_000, "USD", "test order", "test buyer")])
    svc = build_sandbox_service(str(tmp_path / "sbdata"), orders, RuleBasedExtractor(), env=FIXTURE_ENV, transport=fx,
                                checkpoint_dir=str(tmp_path / "sbcheckpoint"), key="fx-lock-test-key-" + "0" * 40)
    return svc, svc.buyer_capability("ORD-9000"), fx


def test_the_service_knows_which_mode_allows_demo_rates(sandbox, env):
    assert env.svc.mode == "fixture" and env.svc.authority.demo_fx is True
    assert sandbox[0].mode == "sandbox" and sandbox[0].authority.demo_fx is False


@pytest.mark.parametrize("tail", FOREIGN)
def test_sandbox_mode_holds_a_foreign_currency_message_for_a_person(sandbox, tail):
    svc, tok, fx = sandbox
    req = svc.message(DAMAGED + tail, "ORD-9000", tok, label="fx")
    assert req["code"] == "FOREIGN_CURRENCY_HELD" and req["intent"] is None       # no proposal at all
    assert "fx" not in req and req["text"] == DAMAGED + tail                       # not converted: the buyer's words stand
    assert fx.refund_posts == 0 and fx.refunds_created == 0                        # nothing sent


def test_sandbox_mode_holds_a_taken_over_readers_dollar_proposal_too(sandbox):
    svc, tok, fx = sandbox

    class Takeover:
        name = "taken-over reader"

        def read(self, text, order):
            return Proposal(REFUND, 1800, "refund_damaged", "reader", {})

    req = svc.message(DAMAGED + "Please refund 18 euros.", "ORD-9000", tok, reader=Takeover(), label="fx-C")
    assert req["code"] == "FOREIGN_CURRENCY_HELD" and req["intent"] is None and fx.refund_posts == 0


def test_sandbox_recovery_holds_a_saved_request_that_carries_a_conversion(sandbox):
    """Codex fix P1: a request saved WITH a demo conversion (a crash before submit, or a data directory used in demo mode) must not
    reach the authority in sandbox mode."""
    svc, tok, fx = sandbox
    svc.authority.demo_fx = True                                     # as if it had been saved in demo mode
    saved, svc.authority.authorize = svc.authority.authorize, lambda *a, **k: None      # the crash: saved, never authorized
    req = svc.message(DAMAGED + "Please refund 18 euros.", "ORD-9000", tok, label="saved")
    svc.authority.authorize = saved
    svc.authority.demo_fx = False
    assert req["fx"] and req["intent"] and svc.agent.orphans() == [req]
    svc.recover()
    assert req["code"] == "FOREIGN_CURRENCY_HELD" and req["intent"] is None and svc.agent.orphans() == []
    assert fx.refund_posts == 0 and fx.refunds_created == 0


def test_sandbox_recovery_of_an_authorized_record_refuses_a_conversion(sandbox):
    svc = sandbox[0]
    assert svc.authority._recovery_verdict(None, {"fx": {"conversions": [1]}}, None, None, None) == ("FOREIGN_CURRENCY_HELD", None)


def test_sandbox_construction_refuses_a_non_usd_order(tmp_path):
    orders = OrderBook([Order.from_native("ORD-9001", "CAP-2", 5_000, "EUR", "x", "y")])
    with pytest.raises(ValueError, match="USD"):
        build_sandbox_service(str(tmp_path / "d"), orders, RuleBasedExtractor(), env=FIXTURE_ENV, transport=FixturePayPal({"CAP-2": 5_000}),
                              checkpoint_dir=str(tmp_path / "c"), key="k" * 48)


def test_sandbox_mode_still_refunds_a_plain_dollar_request(sandbox):
    svc, tok, fx = sandbox
    req = svc.message(DAMAGED + "Please refund $18.00.", "ORD-9000", tok, label="usd")
    assert req["kind"] == "SUBMITTED" and fx.refunds_created == 1


def test_sandbox_mode_does_not_hold_the_word_dollars_or_a_us_dollar_sign(sandbox):
    svc, tok, fx = sandbox
    req = svc.message(DAMAGED + "Please refund 18 dollars.", "ORD-9000", tok, label="usd2")
    assert req["code"] != "FOREIGN_CURRENCY_HELD"


def test_the_held_request_is_shown_to_the_owner_in_words(sandbox):
    from app.views import S
    for key in ("buyer.NEEDS_FOREIGN_CURRENCY_HELD", "needs.FOREIGN_CURRENCY_HELD"):
        assert S(key) and not S(key).startswith(key)


def test_fixture_mode_still_converts_at_the_demo_rate_and_releases_18_euros(env):
    env.msg("Order ORD-1001 arrived damaged. Please refund 18 euros.", "ORD-1001")
    req = env.latest_req()
    assert req["fx"][0]["usd_minor"] == 1944 and req.get("code") != "FOREIGN_CURRENCY_HELD"
    assert req["kind"] == "SUBMITTED" and env.fx.refunds_created == 1             # $19.44, inside the limits: by itself
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["intent"]["amount_minor"] == 1944


def test_fixture_mode_with_the_flag_off_is_the_sandbox_behaviour(env):
    env.svc.authority.demo_fx = False                                                  # the flag, not the mode name, decides
    env.msg("Order ORD-1001 arrived damaged. Please refund 18 euros.", "ORD-1001")
    assert env.latest_req()["code"] == "FOREIGN_CURRENCY_HELD" and env.fx.refunds_created == 0
