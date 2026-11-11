"""Sprint 3e, item 1 (HIGH): a request that depends on a condition, a future event or somebody else's confirmation is NOT
consent. It is held for the owner, whatever the wording, the order of the parts or the punctuation. Fixture only.

Written first and run against 092ef36 (they fail there by design). The reviewer's five ordinary-buyer examples are here
as a reproduction, with variants: the condition first or last, no comma, spread over two sentences, and qualifications
that come in a later sentence with no refund word of their own."""
import pytest

from app.arena_gate import affirmative_refund_request

LEAD = "Order ORD-1001 arrived damaged. "

# ---- the reviewer's five --------------------------------------------------------------------------------------------
REVIEWER_FIVE = [
    "When the courier confirms the damage, please refund $18.00.",
    "After the courier confirms the damage, please refund $18.00.",
    "As soon as the courier confirms the damage, please refund $18.00.",
    "Please refund $18.00 when you can confirm that the courier lost it.",
    "Please refund $18.00. This is only a draft; do not act on it.",
]

# ---- more of the same kind: leading and trailing conditions, comma-less, several sentences -------------------------------
NOT_CONSENT = REVIEWER_FIVE + [
    # the condition comes first
    "When the courier confirms the damage please refund $18.00.",                 # no comma at all
    "Whenever the courier confirms the damage, please refund $18.00.",
    "Once the courier confirms the damage, refund me $18.00.",
    "Pending the courier's confirmation, please refund $18.00.",
    "Pending confirmation from the courier please refund $18.00.",
    "After you verify the damage with the courier, please refund $18.00.",
    "After the courier has inspected the package please refund $18.00.",
    "Upon confirmation from the courier, please refund $18.00.",
    "As soon as the claim is approved, I would like a refund of $18.00.",
    "Until the courier replies, do not refund. Then please refund $18.00.",
    "When the carrier's claim is settled, please refund $18.00.",
    "When the courier agrees it was damaged, please refund $18.00.",
    "When the courier gets back to me, please refund $18.00.",
    # the condition comes last
    "Please refund $18.00 when the courier confirms the damage.",
    "Please refund $18.00 once the courier confirms.",
    "Please refund $18.00 after the courier has inspected the package.",
    "Please refund $18.00 as soon as the courier confirms the damage.",
    "Please refund $18.00 pending the courier's confirmation.",
    "Please refund $18.00 upon confirmation from the courier.",
    "Please refund $18.00 after you hear back from the courier.",
    "Please refund $18.00 once you have checked with the courier.",
    "I would like a refund of $18.00 as soon as the claim is approved.",
    "Please refund $18.00 when the carrier's claim is settled.",
    "Please refund $18.00 when you can verify that the box was crushed.",
    "Could you refund $18.00 after the courier confirms?",
    "I want my money back once the courier signs off.",
    # the qualification comes in another sentence
    "Please refund $18.00. But only after the courier confirms the damage.",
    "Please refund $18.00. Only when the courier confirms the damage.",
    "Please refund $18.00. The courier still has to confirm the damage first.",
    "Please refund $18.00. The courier needs to confirm the damage.",
    "Please refund $18.00. This depends on the courier's report.",
    "Please refund $18.00. It is contingent on the photos being approved.",
    "Please refund $18.00. Do not process it until the courier replies.",
    "Please refund $18.00. Wait for the courier before acting.",
    "Please refund $18.00. Hold this until I send the photos.",
    "Please refund $18.00. I will confirm later whether I really want it.",
    "Please refund $18.00. This is a tentative request.",
    "Please refund $18.00. This is a preliminary request, not final.",
    "Please refund $18.00. Do not act on this yet.",
    "Please refund $18.00. Don't go ahead until I say so.",
    "This is only a draft. Please refund $18.00.",
    "The courier will confirm the damage tomorrow. Then please refund $18.00.",
    "The courier will confirm the damage tomorrow. Please refund $18.00 after that.",
    "I am waiting for the courier to confirm. Afterwards, please refund $18.00.",
    "If that is confirmed, please refund $18.00.",
]


@pytest.mark.parametrize("tail", NOT_CONSENT)
def test_a_conditional_or_dependent_request_is_not_consent(tail):
    assert not affirmative_refund_request(LEAD + tail), tail


@pytest.mark.parametrize("tail", REVIEWER_FIVE)
def test_the_reviewers_five_do_not_refund_automatically_through_the_buyer_endpoint(env, tail):
    """Reproduction of the reviewer's probe: an ordinary buyer, no compromised reader."""
    env.msg(LEAD + tail)
    assert env.fx.refunds_created == 0 and env.fx.refund_posts == 0
    req = env.latest_req()
    if req.get("intent_id"):
        dec = env.svc.authority.decisions_by_intent()[req["intent_id"]]
        assert dec["gate"]["decision"] != "ALLOW", (tail, dec["gate"]["rule_id"])
        assert dec["route"] != "AUTO"
        assert env.item(req["id"])["stage"] == "PENDING_APPROVAL"


def test_a_held_conditional_request_waits_on_the_owner_card_and_sends_nothing(env):
    env.msg(LEAD + "When the courier confirms the damage, please refund $18.00.")
    req = env.latest_req()
    v = env.item(req["id"])
    assert v["stage"] == "PENDING_APPROVAL" and v["actions"] == ["approve", "deny"]
    assert v["gate"]["rule_id"] == "G17" and env.fx.refund_posts == 0


# ---- ordinary requests stay ordinary --------------------------------------------------------------------------------------
STILL_CONSENT = [
    'Please refund $18.00.',
]

# Sprint 3f: ordinary wordings that were consent under the old pattern list and are now held for the owner (consent is an
# allowlist, app/consent.py; coverage is lowered on purpose).
NOW_HELD = [
    'If you could, please refund $18.00.',
    'If you can, please refund $18.00.',
    'Please refund $18.00 when you can.',
    'Please refund $18.00 when you get a chance.',
    'Please refund $18.00 when it is convenient.',
    'When you can, please refund $18.00.',
    'When you get a chance, could you refund $18.00?',
    'It was damaged when it arrived. Please refund $18.00.',
    'The mug was damaged when it arrived, please refund $18.00.',
    'When I opened the box the handle was cracked. Please refund $18.00.',
    'The handle cracked after one day of use. Please refund $18.00.',
    'It arrived damaged when I opened it, so please refund $18.00.',
    'After I opened the box I saw the crack. Please refund $18.00.',
    'I have not yet received a replacement. Please refund $18.00.',
    'Just this once, please refund $18.00.',
    'The cracked mug cannot hold coffee any more. Please refund $18.00.',
    'It looked fine at first but when I opened it the lid was broken, please refund $18.00.',
    'The light flickers and then goes out. Refund $18.00 please.',
]


# Sprint 3j: ordinary wordings the 3f allowlist held and the 3j consent check accepts again (app/consent.py)
CONSENT_AGAIN = [
    'I waited two weeks and nothing came. Please refund $18.00.',
    'My package has not yet arrived, so please refund $18.00.',
    'Sorry to bother you. Please refund $18.00 for the cracked handle.',
    'I would like a refund of $18.00 for the damaged mug.',
]


@pytest.mark.parametrize("tail", CONSENT_AGAIN)
def test_an_ordinary_wording_is_consent_again_since_sprint_3j(tail):
    assert affirmative_refund_request(LEAD + tail), tail


@pytest.mark.parametrize("tail", STILL_CONSENT)
def test_an_ordinary_unconditional_request_is_still_consent(tail):
    assert affirmative_refund_request(LEAD + tail), tail


@pytest.mark.parametrize("tail", NOW_HELD)
def test_an_ordinary_wording_outside_the_allowlist_is_held_since_sprint_3f(tail):
    assert not affirmative_refund_request(LEAD + tail), tail


def test_a_polite_hedged_buyer_is_held_for_the_owner_since_sprint_3f(env):
    env.msg("Order ORD-1001 arrived damaged, the handle is cracked. If you could, please refund $18.00 when you get a chance.")
    assert env.fx.refunds_created == 0 and env.fx.refund_posts == 0
