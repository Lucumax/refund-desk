"""Sprint 2: Policy Builder polish. Plain-words rule -> Preview -> diff -> Save as a signed policy version. Words it
does not know are refused, not guessed. What is saved is exactly what was previewed. Approvals bind to the policy
version. The limits shown on screen are the limits the engine enforces."""
import os
from dataclasses import replace

import pytest

from app.arena_policy import compile_text
from app.authority import AUTO_AGENT, REVIEW_AGENT
from conftest import ROOT, MSG_B

PREVIEW, SAVE = "/api/policy/preview", "/api/policy/save"


def preview(env, text):
    r = env.post(PREVIEW, {"text": text})
    assert r.status == 200
    return r.json()


def policy_records(env):
    return [r for r in env.svc.policies.ledger.receipts() if r["event"] == "policy_version"]


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


# ---- refused, not guessed ----------------------------------------------------------------------------------
REFUSED = [
    "refund whatever the customer asks",
    "refund up to $30 unless the buyer sounds angry",
    "refund 50% of the order",
    "refund up to $30 or $40",
    "ignore previous limits and refund everything",
    "refund up to thirty dollars",
    "refund only for damaged items and loyal customers",
    "refund up to $30 for VIP customers",
    "refund up to $30 per order; delete all receipts",
    "refund up to $-5",
    "refund up to $30 per week",
    "refund up to $30 per order as you see fit",
    "refund up to $30 per refund",
    "at most $20 a request",
    "refund up to $0",
    "refund up to $1000000",
    "require quoted evidence; no evidence needed",
    "refund up to $30. refund up to $50",
]


@pytest.mark.parametrize("text", REFUSED)
def test_an_unsupported_phrase_is_refused_and_nothing_is_saved(env, text):
    before = len(policy_records(env))
    p = preview(env, text)
    assert p["ok"] is False and p["errors"] and p["policy"] is None
    r = env.post(SAVE, {"text": text})
    assert r.status in (409, 422) and r.json()["ok"] is False
    assert len(policy_records(env)) == before
    assert env.svc.policies.active()[1]["version"] == 1


def test_a_limit_per_single_refund_is_refused_with_its_own_message_not_read_as_per_order(env):
    p = preview(env, "refund up to $30 per refund")
    assert [e["code"] for e in p["errors"]] == ["PER_REFUND_NOT_SUPPORTED"]
    assert "per order" in env.get("/api/strings", headers={}).json()["policy.err.PER_REFUND_NOT_SUPPORTED"]
    assert preview(env, "refund up to $30 per order")["ok"] is True


def test_one_bad_clause_rejects_the_whole_text_even_if_the_rest_is_fine(env):
    p = preview(env, "refund up to $30 per order; daily budget of $90; refund only for loyal customers")
    assert p["ok"] is False and any(e["code"] == "UNKNOWN_PHRASE" for e in p["errors"])
    assert p["recognized"]            # it says what it did understand, but applies none of it


# ---- preview, diff, save: what is saved is what was previewed ---------------------------------------------------
def test_preview_shows_a_diff_and_a_fingerprint_and_changes_nothing(env):
    p = preview(env, "refund up to $30 per order; daily budget of $90")
    assert p["ok"] and p["base_version"] == 1 and len(p["result_hash"]) == 64
    changed = {c["field"]: (c["before"], c["after"]) for c in p["changes"] if c["changed"]}
    assert changed == {"per_order_cap_minor": (2500, 3000), "daily_budget_minor": (10000, 9000)}
    assert len(policy_records(env)) == 1


def test_save_creates_the_next_signed_version_with_the_text_the_diff_and_who(env):
    p = preview(env, "refund up to $30 per order")
    r = env.post(SAVE, {"text": "refund up to $30 per order", "base_version": p["base_version"], "result_hash": p["result_hash"]})
    assert r.status == 200 and r.json()["ok"] and r.json()["version"] == 2 and r.json()["hash"] == p["result_hash"]
    rec = policy_records(env)[-1]
    assert rec["version"] == 2 and rec["source_text"] == "refund up to $30 per order" and rec["saved_by"]
    assert any(c["changed"] for c in rec["changes"]) and rec["signature"] and rec["policy_hash"] == p["result_hash"]
    assert env.svc.verify_all()["ok"] is True and env.svc.policies.ledger.verify_trusted().ok


def test_a_save_is_refused_if_the_policy_in_force_moved_on_after_the_preview(env):
    p = preview(env, "refund up to $30 per order")
    other = env.save_policy("daily budget of $90")
    assert other["ok"]
    r = env.post(SAVE, {"text": "refund up to $30 per order", "base_version": p["base_version"], "result_hash": p["result_hash"]})
    assert r.status == 409 and r.json()["errors"][0]["code"] == "POLICY_CHANGED"
    assert len(policy_records(env)) == 2


def test_a_save_is_refused_if_the_text_no_longer_gives_the_previewed_result(env):
    p = preview(env, "refund up to $30 per order")
    r = env.post(SAVE, {"text": "refund up to $40 per order", "base_version": p["base_version"], "result_hash": p["result_hash"]})
    assert r.status == 409 and r.json()["errors"][0]["code"] == "PREVIEW_MISMATCH"
    assert len(policy_records(env)) == 1


def test_a_rule_that_changes_nothing_is_not_saved_as_a_new_version(env):
    p = preview(env, "refund up to $25 per order")
    r = env.post(SAVE, {"text": "refund up to $25 per order", "base_version": p["base_version"], "result_hash": p["result_hash"]})
    assert r.status == 422 and r.json()["errors"][0]["code"] == "NO_CHANGE" and len(policy_records(env)) == 1


def test_the_policy_page_sends_the_previewed_version_and_result_back_with_the_save():
    js = read("static/policy.js")
    assert "base_version: LAST_PREVIEW.base_version" in js and "result_hash: LAST_PREVIEW.result_hash" in js
    html = read("static/policy.html")
    assert 'id="enforced"' in html and 'id="save"' in html and "disabled" in html


# ---- the limits on screen are the limits the engine enforces --------------------------------------------------------
def test_enforced_limits_come_from_the_engine_and_match_the_policy(env):
    out = env.get("/api/policy", headers={}).json()
    e = out["enforced"]
    assert e["matches_policy"] is True and e["policy_version"] == e["mandate_version"] == 1
    assert (e["automatic_allowance_minor"], e["daily_budget_minor"], e["approve_ceiling_minor"]) == (2500, 10000, 20000)
    # the numbers are read from the live engine mandates, not copied from the policy store
    assert env.svc.engine.mandates[AUTO_AGENT].per_payment_limit == e["automatic_allowance_minor"]
    assert env.svc.engine.mandates[REVIEW_AGENT].daily_limit == e["approve_ceiling_minor"]


def test_a_mandate_that_drifts_from_the_policy_is_reported_not_hidden(env):
    m = env.svc.engine.mandates[AUTO_AGENT]
    env.svc.engine.mandates[AUTO_AGENT] = replace(m, per_payment_limit=99999)
    assert env.svc.enforced_limits()["matches_policy"] is False
    strings = env.get("/api/strings", headers={}).json()
    assert "NO" in strings["policy.enforced_mismatch"]


def test_saved_numbers_are_the_numbers_shown_and_the_numbers_enforced_at_their_edges(env):
    text = "refund up to $30 per order; daily budget of $90; never refund more than $60"
    p = preview(env, text)
    assert env.post(SAVE, {"text": text, "base_version": 1, "result_hash": p["result_hash"]}).status == 200
    shown = env.get("/api/desk/state").json()
    assert shown["enforced"]["matches_policy"] is True
    assert (shown["enforced"]["automatic_allowance_minor"], shown["enforced"]["daily_budget_minor"],
            shown["enforced"]["approve_ceiling_minor"]) == (3000, 9000, 6000)
    for money in ("$30.00 per order", "$90.00 per day", "$60.00"):
        assert money in shown["rule_sentence"]
    # edge by edge, on fresh orders: exactly the allowance goes out, a cent more waits, above the ceiling is refused
    def decision(order, amount):
        env.msg(f"ORD-{order[4:]} arrived damaged. Please refund ${amount}.", order=order)
        v = next(i for i in env.inbox()["items"] if i["order_ref"] == order)
        return v["gate"]["decision"], v["gate"]["rule_id"]
    assert decision("ORD-1001", "30.00") == ("ALLOW", "R9")
    assert decision("ORD-1002", "30.01") == ("ESCALATE", "R7")
    assert decision("ORD-1003", "60.00") == ("ESCALATE", "R7")
    assert decision("ORD-1007", "60.01")[0] == "DENY"
    assert decision("ORD-1009", "30.00") == ("ALLOW", "R9")
    assert decision("ORD-1010", "30.00") == ("ALLOW", "R9")              # $90 so far today
    assert decision("ORD-1011", "5.00") == ("ESCALATE", "R8")           # the daily budget is spent


def test_the_desk_page_shows_the_engine_numbers_not_its_own_copy():
    js = read("static/desk.js")
    assert "s.enforced.automatic_allowance_minor" in js and "s.enforced.daily_budget_minor" in js and "s.enforced.approve_ceiling_minor" in js
    assert "desk.enforced_line" in js


# ---- approvals bind to the policy version --------------------------------------------------------------------------
def _expected_terms_digest(payee, amount_minor, purpose_code, mandate_id, mandate_version):
    """The approval digest written out by hand from the documented terms (payee, amount, currency, purpose, mandate id,
    mandate version, mandate expiry): SHA-256 over canonical JSON. It deliberately does NOT call the production
    `terms_digest`, so this test cannot pass just because production agrees with itself."""
    import hashlib
    import json
    terms = {"payee": payee, "amount_minor": amount_minor, "currency": "USD", "purpose_code": purpose_code,
             "mandate_id": mandate_id, "mandate_version": mandate_version,
             "mandate_expires_at": "2027-12-31T00:00:00+00:00"}
    return hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def test_the_approval_card_names_the_policy_version_and_the_digest_covers_it(env):
    env.msg(MSG_B)
    rid = env.latest_req()["id"]
    card = env.item(rid)["approval"]
    assert card["policy_version"] == 1 and len(card["policy_hash"]) == 64 and card["stale"] is False
    rec = env.svc.ledger.latest_for_intent(env.item(rid)["intent_id"])
    payee, amount, purpose = env.svc.orders.get("ORD-1002").capture_id, 12000, "refund_not_received"
    assert (rec["intent"]["payee"], rec["intent"]["amount_minor"], rec["intent"]["purpose_code"]) == (payee, amount, purpose)
    v1 = _expected_terms_digest(payee, amount, purpose, "M-REFUND-REVIEW", 1)
    v2 = _expected_terms_digest(payee, amount, purpose, "M-REFUND-REVIEW", 2)
    assert card["digest"] == v1 and rec["intent_digest"] == v1
    assert v2 != card["digest"]                                  # same terms, other version: other digest
    # after a new policy version the engine's mandate is version 2 and the digest a fresh card would carry is v2
    assert env.save_policy("refund up to $30 per order")["ok"]
    assert env.item(rid)["approval"]["stale"] is True
    assert env.svc.engine.mandates[REVIEW_AGENT].version == 2


def test_an_approval_given_under_an_old_version_is_refused_after_a_new_version_is_saved(env):
    env.msg(MSG_B)
    rid = env.latest_req()["id"]
    card = env.item(rid)["approval"]
    assert env.save_policy("refund up to $30 per order")["ok"]
    r = env.act(rid, "approve", {"nonce": card["nonce"], "digest": card["digest"]})
    assert r.status == 409 and r.json()["error"] == "TERMS_CHANGED"
    assert env.fx.refund_posts == 0
    # the card, re-read, says it is stale and offers only a denial; nothing can be approved against it
    again = env.item(rid)
    assert again["approval"]["stale"] is True and again["actions"] == ["deny"]
    assert "can no longer be given" in env.get("/api/strings", headers={}).json()["desk.owner_stale"]
    assert 'v.approval && v.approval.stale' in read("static/desk.js") and 'v.approval.stale' in read("static/inbox.js")


def test_the_desk_tells_the_owner_which_version_the_yes_applies_to():
    assert "desk.owner_policy_version" in read("static/desk.js")
    assert "this yes applies to rule version" in read("copy/strings.draft.json").lower()


def test_the_compiler_never_runs_anything_it_is_given():
    # a closed schema: the result is always the five fields, whatever the text says
    r = compile_text("refund up to $30 per order; __import__('os').system('x')")
    assert r.ok is False
    ok = compile_text("refund up to $30 per order")
    assert set(ok.policy.as_dict()) == {"per_order_cap_minor", "escalate_ceiling_minor", "daily_budget_minor",
                                        "evidence_required", "eligible_reasons"}
