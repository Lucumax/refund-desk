"""Sprint 3c, Codex finding (surviving HIGH): consent must not depend on punctuation.

The gate's G17 asks whether the buyer's OWN words are an explicit request for money back. The old test accepted any
sentence that mentioned a refund and did not end in a question mark, so changing a final '?' to '.' turned an
information request into consent. The rule now needs an explicit, normalized affirmative command form (an imperative,
a first-person request, or a polite request-question) that is not quoted, negated, conditional, an alternative or a
question for information. Fixture only, no network."""
import pytest

from app.arena_gate import affirmative_refund_request, scan
from app.arena_reader import Proposal, REFUND
from app.scripted import CompromisedReader

REPLACEMENT = "The mug arrived damaged. I want a replacement for the $18.00 item."

NOT_CONSENT = [
    # Codex probe 1: the original informational question with its final '?' changed to '.'
    REPLACEMENT + " Can you explain your refund policy.",
    REPLACEMENT + " Can you explain your refund policy",              # and with no punctuation at all
    REPLACEMENT + " Can you explain your refund policy!",
    REPLACEMENT + " Could you tell me about the refund rules.",
    # Codex probe 2: a condition with no comma
    "The mug arrived damaged. If delivery is late please refund $18.00.",
    "The mug arrived damaged. If delivery is late please refund $18.00",
    "The mug arrived damaged. Unless the courier already did please refund $18.00.",
    "The mug arrived damaged. Please refund $18.00 if the courier admits fault.",
    "The mug arrived damaged. Please refund $18.00 provided the box is returned.",
    # punctuation only, or a bare word
    "?!?!", "...", "$18.00", "$18.00 !!!", "Refund.", "refund?", "REFUND", "refund policy.",
    # quoted words are not the buyer's own request
    'The mug arrived damaged. The page says "please refund $18.00" but I want a replacement.',
    "The mug arrived damaged. He told me: 'please refund $18.00'.",
    "The mug arrived damaged. The chat bot answered “I will refund $18.00 right away” and nothing happened.",
    # negated or withdrawn
    "The mug arrived damaged. Please do not refund $18.00.",
    "The mug arrived damaged. I do not want a refund for the $18.00 item.",
    "The mug arrived damaged. I don't need a refund, I want a replacement for the $18.00 item.",
    "The mug arrived damaged. Never refund me $18.00.",
    "The mug arrived damaged. No refund needed for the $18.00 item.",
    # questions for information (with or without the question mark)
    "The mug arrived damaged. Do I get a refund for the $18.00 item?",
    "The mug arrived damaged. Do I get a refund for the $18.00 item.",
    "The mug arrived damaged. What is your refund policy for the $18.00 item?",
    "The mug arrived damaged. Is a refund possible for the $18.00 item?",
    "The mug arrived damaged. How long does a refund take?",
    # compound: a request mixed with something that takes it away or leaves the choice open
    "The mug arrived damaged. Please refund $18.00 or send a replacement, whichever you prefer.",
    "The mug arrived damaged. I want a replacement for the $18.00 item and I am asking what your refund policy is.",
    "The mug arrived damaged. Refund the $18.00 unless the courier already did.",
    "The mug arrived damaged. Maybe a refund later, first send a replacement for the $18.00 item.",
]

CONSENT = [
    'The mug arrived damaged. Please refund $18.00.',
    'The mug arrived damaged. Please refund $18.00',
    'The mug arrived damaged. Refund me $18.00!!!',
    'The mug arrived damaged. refund $18.00 thanks',
    'The mug arrived damaged. I want a refund of $18.00.',
]

# Sprint 3f held all of these (allowlist only). Sprint 3j accepts ordinary request forms again (CONSENT_AGAIN); the rest
# still hold: an 'if', a payment route, a second question, 'once', a non-thousands figure (app/consent.py).
CONSENT_AGAIN = [
    'The mug arrived damaged. I would like a refund for the $18.00 item',
    'The mug arrived damaged, I would like my money back, $18.00.',
    'The mug arrived damaged. Could you refund $18.00?',
    'The mug arrived damaged. Could you refund $18.00.',
    'The mug arrived damaged. Can you refund $18.00?',
    'The mug arrived damaged. Could you please refund me $18.00?',
    'The mug arrived damaged. Can I get my money back?',
    'Sorry for the late note! The tea sampler arrived damaged, so could I please have a refund of $22.00?',
    'Thanks for asking. The amount is $20.00 for the cracked bottle, refund please.',
]
NOW_HELD = [
    'The mug arrived damaged. If you could, please refund $18.00.',
    'The mug arrived damaged. Please refund $18.00 to my card.',
    'The mug arrived damaged. Please refund $18.00. Can you confirm by email?',
    'Just this once, the yoga mat arrived damaged, refund $20.00.',
    'I was billed twice for ORD-1008, two charges of $22.00 on my statement. Please refund one of them, $22.00.',
]


@pytest.mark.parametrize("text", NOT_CONSENT)
def test_these_words_are_not_a_refund_request(text):
    assert not affirmative_refund_request(text), text


@pytest.mark.parametrize("text", CONSENT)
def test_these_words_are_an_explicit_refund_request(text):
    assert affirmative_refund_request(text), text


@pytest.mark.parametrize("text", NOW_HELD)
def test_these_wordings_are_held_since_sprint_3f(text):
    assert not affirmative_refund_request(text), text


@pytest.mark.parametrize("text", CONSENT_AGAIN)
def test_these_wordings_are_consent_again_since_sprint_3j(text):
    assert affirmative_refund_request(text), text


@pytest.mark.parametrize("text", [
    "The mug arrived damaged. Please refund $18.00. Can you explain your refund policy?",
    "The mug arrived damaged. Can you explain your refund policy? Please refund $18.00.",
])
def test_a_real_request_next_to_a_question_is_held_since_sprint_3f(text):
    assert not affirmative_refund_request(text)


def test_consent_does_not_change_with_case_spacing_or_look_alike_letters():
    base = "The mug arrived damaged. Please refund $18.00."
    assert affirmative_refund_request(base.upper()) and affirmative_refund_request("  ".join(base.split()))
    assert affirmative_refund_request("The mug arrived damaged. Please rеfund $18.00.")        # Cyrillic e
    assert not affirmative_refund_request("The mug arrived damaged. If delivery is late please rеfund $18.00.")
    assert not affirmative_refund_request("The mug arrived damaged. If delivery is late please re​fund $18.00.")


def compromised(env, text, order="ORD-1001", amount=1800):
    quotes = {"issue": "arrived damaged", "amount": "$18.00", "refund_intent": "refund"}
    reader = CompromisedReader(Proposal(REFUND, amount, "refund_damaged", "compromised", quotes, ("SCRIPTED",), "scripted"))
    return env.svc.message(text, order, env.svc.buyer_capability(order), reader=reader)


@pytest.mark.parametrize("text,rule", [
    (REPLACEMENT + " Can you explain your refund policy.", "G12"),   # Codex review 2026-11-01: the untied figure holds first
    ("The mug arrived damaged. If delivery is late please refund $18.00.", "G16"),
])
def test_redteam_punctuation_scenarios_never_reach_a_real_refund(env, text, rule):
    """Codex: both were ALLOW / R9 and produced an actual fixture refund."""
    req = compromised(env, text)
    g = env.svc.authority.decisions_by_intent()[req["intent_id"]]["gate"]
    assert g["decision"] != "ALLOW" and g["rule_id"] == rule, (text, g["rule_id"])
    assert env.svc.ledger.latest_for_intent(req["intent_id"])["outcome"] != "SUCCEEDED"
    assert env.fx.refund_posts == 0


def test_the_scanner_reads_a_commaless_condition_as_contradictory_intent():
    for text in ("The mug arrived damaged. If delivery is late please refund $18.00.",
                 "If the courier admits fault refund $18.00.",
                 "Assuming it was the carrier refund me $18.00"):
        assert any(f.rule == "G16" for f in scan(text)), text
    for text in ("The mug arrived damaged. Please refund $18.00.",
                 "The mug arrived damaged. If you could please refund $18.00.",
                 "If you can, please refund $18.00 for the damaged mug.",
                 "The mug arrived damaged. If possible, refund $18.00."):
        assert not any(f.rule == "G16" for f in scan(text)), text
