"""Sprint 3c: the phone card is checked by RUNNING static/approve.js against a stand-in page, not by reading
its source text. Needs Node; skipped without it. No browser, no network."""
import json
import os
import shutil
import subprocess

import pytest

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "approve_harness.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_phone_card_renders_and_acts_as_the_actions_say():
    run = subprocess.run(["node", HARNESS], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["normal"] == ["approve", "deny"]
    assert out["stale"] == ["deny"]                                   # a stale card still offers Deny, never Approve
    assert out["done"] == {"buttons": [], "stamp": True}              # nothing to decide: the stamp, no buttons
    assert out["deny_call"]["url"] == "/api/requests/REQ-0001/deny" and out["deny_call"]["method"] == "POST"
    assert "nonce" not in out["deny_call"]["body"]
    # Sprint 3d: both actions name the exact intent and amount the card showed
    assert out["deny_call"]["body"]["intent_id"] == "I-0001" and out["deny_call"]["body"]["amount_minor"] == 12000
    assert out["approve_call"]["body"]["intent_id"] == "I-0001" and out["approve_call"]["body"]["amount_minor"] == 12000
    assert out["approve_call"]["url"] == "/api/requests/REQ-0001/approve"
    assert out["approve_call"]["body"]["nonce"] == "N1" and out["approve_call"]["body"]["digest"] == "D1"
