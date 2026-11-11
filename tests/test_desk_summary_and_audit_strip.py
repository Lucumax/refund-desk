"""Branch video-ui-polish (Nov 1 2026), from the Codex enterprise UX review of 00ac3fa: the transaction comes before
approval (rank 1), an audit strip sits next to the decision (rank 9), and three strings stop overclaiming. The phone card
and the shared helpers are checked by RUNNING the JavaScript against stand-in pages (Node; skipped without it)."""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "tests", "js")
COPY = json.load(open(os.path.join(ROOT, "copy", "strings.draft.json"), encoding="utf-8"))["strings"]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")


def run(harness):
    r = subprocess.run(["node", os.path.join(JS, harness)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@needs_node
def test_phone_card_puts_the_transaction_and_its_evidence_before_the_buttons():
    out = run("approve_harness.js")
    assert out["order"] == ["summary", "why", "buyer", "scope", "button:approve", "button:deny", "audit"]


@needs_node
def test_phone_card_approve_button_says_simulated_in_the_simulation():
    out = run("approve_harness.js")
    assert out["approve_text"] == "approve.button_sim"


@needs_node
def test_approval_summary_names_amount_in_usd_recipient_order_request_and_rule_version():
    out = run("summary_harness.js")
    s = out["summary"]
    assert s["tag"] == "dl" and "approve-summary" in s["cls"]
    assert "$120.00 USD" in s["text"]
    assert "Sample buyer B, ORD-1002" in s["text"]                 # the trusted order file, never the reader's prose
    assert "ORD-1002" in s["text"] and "REQ-0004" in s["text"]
    assert COPY["summary.rule_value"].replace("{version}", "1") in s["text"]


@needs_node
def test_approval_summary_says_unavailable_rather_than_guessing_a_recipient():
    out = run("summary_harness.js")
    assert COPY["summary.unavailable"] in out["summary_no_who"]
    assert "Sample buyer" not in out["summary_no_who"]


@needs_node
def test_audit_strip_names_the_record_rule_version_and_source_and_never_claims_a_verification_it_did_not_run():
    out = run("summary_harness.js")
    a = out["audit_sim"]["text"]
    assert "D-000012" in a and "I-0007" in a
    assert COPY["audit.source_sim"] in a and COPY["audit.source_sandbox"] not in a
    assert COPY["audit.unverified"] in a
    assert "not" in COPY["audit.unverified"].lower()                # says the check was not run on this screen
    assert COPY["audit.source_sandbox"] in out["audit_sandbox"]
    assert out["audit_none"] is None


def test_desk_shows_the_buyer_words_and_both_panels_before_the_owner_card_and_an_audit_strip_after():
    src = open(os.path.join(ROOT, "static", "desk.js"), encoding="utf-8").read()
    body = src[src.index("function renderFlow("):]
    body = body[:body.index("\n}\n")]
    assert body.index("panel.buyer_title") < body.index('class: "two"') < body.index("ownerCard(v)")
    assert "auditStrip(v" in body
    owner = src[src.index("function ownerCard("):]
    owner = owner[:owner.index("\n}\n")]
    assert "approvalSummary(v)" in owner


def test_strings_no_longer_overclaim():
    assert not COPY["headline.SENT_PENDING"].startswith("Refunded")
    assert "submitted" in COPY["headline.SENT_PENDING"] and "still processing" in COPY["headline.SENT_PENDING"]
    assert "honest" not in COPY["tile.held_label"].lower()
    assert "model" not in COPY["inbox.helper_note"].lower()
    inbox = open(os.path.join(ROOT, "static", "inbox.js"), encoding="utf-8").read()
    line = next(l for l in inbox.splitlines() if "inbox.helper_note" in l)
    assert "desk.reader_label" in line                              # the summary names the reader that wrote it
