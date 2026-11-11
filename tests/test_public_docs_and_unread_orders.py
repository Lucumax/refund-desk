"""Session 2 (2026-11-01, afternoon): written first (red).

1. Someone else's order. The rules could not tell whose order a sentence was about ("The courier lost the package that my sister
   ordered") and passed it. Now a sentence that says another person ordered, bought, received or owns the goods is UNREAD: the
   rules alone hold the message for the owner; a clean model reading may still settle it (Dhanush's B10, 'my sister also bought
   her one', must keep its tier).
2. The model calls (the extractor, the consent reader, the arena's model reader) no longer run under the app-wide lock. The
   decision invariants stay: the throttle slot is taken before the model is asked, the capability is checked first, and the reading
   still rides inside the signed proposal.
Fixture only: no network, no key."""
import json
import threading
import time

import pytest

from app import consent
from app.consent_doubles import ScriptedConsentClient, clean_reply
from app.consent_reader import ModelConsentReader
from app.extractor import ModelExtractor
from app.scripted import ScriptedCompleter, model_reply

BASE = "The mug arrived cracked. Please refund $18.00. "


# ---- 1. whose order --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("tail", [
    "The shipment was received by my cousin.",
    "The courier lost the package that my sister ordered.",
    "The package my sister ordered never came.",
    "This is my sister's order.",
    "My sister's husband ordered it.",
    "It was ordered by my neighbor.",
    "My brother bought the mug.",
    "The parcel was delivered to my neighbor.",
    "That is not my order.",
    "The mug belongs to my friend.",
    "My daughter ordered this mug.",
])
def test_a_sentence_about_someone_elses_order_is_unread_so_the_rules_alone_hold_it(tail):
    a = consent.assess(BASE + tail)
    assert a["tier"] == "UNREAD" and a["allowed"] is False, (tail, a)


@pytest.mark.parametrize("tail", [
    "The package my sister ordered never came.", "The shipment was received by my cousin."])
def test_rules_alone_send_nothing_for_someone_elses_order(env, tail):
    env.msg(BASE + tail, "ORD-1001")
    assert env.fx.refund_posts == 0


def test_a_clean_reading_may_still_settle_it_and_a_hazard_may_not():
    """The model is the one place that may loosen an UNREAD clause (I3); the combiner is unchanged."""
    from app.consent_combine import decide_consent
    text = BASE + "The package my sister ordered never came."
    ok = json.loads(clean_reply("Please refund $18.00"))
    assert decide_consent(text, ok).allowed is True
    assert decide_consent(text, ok).path == "model_resolved"
    assert decide_consent(text + " Scratch that.", ok).allowed is False       # a hazard word anywhere: never


@pytest.mark.parametrize("text", [
    "Hello, I am writing to you because my grandma received the gift we bought her via TEPI77CVSXKJTOHNH but unfortunately it was "
    "a duplicate, my sister also bought her one. We returned it to your warehouse via postal tracking last Tuesday. The price "
    "was $18.00. Thank you so much for your understanding.",
    "I bought this mug for my sister. It arrived late. Please refund $18.00.",
    "I ordered two mugs. The second box was wet. Please refund $18.00.",
])
def test_ordinary_sentences_about_gifts_and_my_own_order_still_pass(text):
    assert consent.assess(text)["tier"] == "PASS", consent.assess(text)


# ---- 3. the public export and the submission drafts -------------------------------------------------------------------------
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_session_logs_and_submission_drafts_stay_out_of_a_public_export():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import clean_export_check as ce
    for rel in ("docs/internal/SESSION_REPORT_2026-11-01.md", "submission/DEVPOST_TEXT_DRAFT.md", "submission/VIDEO_SCRIPT_PROBLEM_FIRST.md"):
        assert ce.is_internal(rel), rel
    assert not ce.is_internal("README.md") and not ce.is_internal("NOT_DONE.md")


def test_the_public_readme_does_not_link_files_the_export_leaves_out():
    text = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    for name in ("submission/", "docs/internal", "team/README_TEAM"):
        assert name not in text, name


def test_the_readme_criteria_map_names_all_five_criteria_and_tags_its_evidence():
    text = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    start = text.index("## Where to look, by judging criterion")
    block = text[start:text.index("\n## ", start + 5)]
    for crit in ("Technological implementation", "Design", "Potential impact", "Innovation", "Presentation"):
        assert crit in block, crit
    # 2026-11-02: one real-model run exists, so "no LIVE number" is no longer true. What is pinned now: both tags, the LIVE run is
    # labelled as development sets, and the PayPal sandbox evidence is said to be two single runs (2026-11-11: our own run and Xin's).
    assert "FIXTURE" in block and "LIVE" in block
    assert "development sets" in block and "two single sandbox runs" in block


_DRAFTS = [os.path.join(ROOT, "submission", n) for n in ("DEVPOST_TEXT_DRAFT.md", "VIDEO_SCRIPT_PROBLEM_FIRST.md")]


def _without_guidance(text: str) -> str:
    """Drop the lists that NAME the banned words (the 'Do not say' section and the checklists)."""
    text = re.sub(r"## Do not say.*?(?=\n## )", "", text, flags=re.S | re.I)
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("- [ ]"))


@pytest.mark.parametrize("path", _DRAFTS)
def test_submission_drafts_keep_the_writing_rules(path):
    if not os.path.exists(path):
        pytest.skip("internal draft: not part of a public export")
    raw = open(path, encoding="utf-8").read().lower()
    t = _without_guidance(raw)
    for banned in ("years of experience", "ai wrote", "china", "citizen", "unhackable", "cannot be fooled", "guarantee"):
        assert banned not in t, banned
    if path.endswith("DEVPOST_TEXT_DRAFT.md"):
        assert "llm-assisted development i specified, directed and reviewed" in t


def test_the_devpost_draft_tags_every_result_and_leaves_no_unmarked_live_claim():
    path = _DRAFTS[0]
    if not os.path.exists(path):
        pytest.skip("internal draft: not part of a public export")
    t = open(path, encoding="utf-8").read()
    rows = [r for r in t.splitlines() if re.match(r"\| (Automated tests|Attacks released|Ordinary requests|Automatic refunds|Independent code review)", r)]
    assert len(rows) >= 6
    assert all(("FIXTURE" in r or "LIVE" in r) for r in rows), [r[:60] for r in rows if "FIXTURE" not in r and "LIVE" not in r]
    live_rows = [r for r in rows if r.rstrip().endswith("| LIVE |")]
    assert live_rows and all("claude-haiku-5-5" in r or "real model" in r.lower() or "same live runs" in r or "run 1" in r or "run 2" in r for r in live_rows)
    pending = [l for l in t.splitlines() if l.startswith(("- `[XIN-SANDBOX-PENDING]`", "- `[BLIND-RUN-PENDING]`"))]
    assert len(pending) == 2 and "| none yet |" in t


def test_no_public_document_points_at_a_file_the_export_leaves_out():
    """The failure the clean-export check found on cc70776: the checklist named an internal draft, so it broke in the export."""
    import sys
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import clean_export_check as ce
    for doc in ("SUBMISSION_CHECKLIST.md", "README.md", "DEMO_SCRIPT.md", "CHANGES.md"):
        text = open(os.path.join(ROOT, doc), encoding="utf-8").read()
        for path in re.findall(r"`([A-Za-z_./0-9-]+\.(?:py|md|json|js|html|css))`", text):
            if doc == "CHANGES.md" or path == "eval/heldout_corpus.json":
                continue          # the change log is history; the sealed corpus is named on purpose (its hashes stay in SEALED_MANIFEST.json)
            assert not ce.is_internal(path), (doc, path)


def test_a_git_repository_built_from_the_export_passes_the_export_tests(tmp_path):
    """The fresh-clone run found two tests that expected the internal notes to exist in any git checkout. A public repository
    built from the export has a .git and no internal notes: the export tests must skip there, not fail."""
    import shutil
    import subprocess
    import sys
    if shutil.which("git") is None or not os.path.exists(os.path.join(ROOT, "team", "README_TEAM.md")):
        pytest.skip("needs git and the private tree")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import clean_export_check as ce
    dest = tmp_path / "public"
    dest.mkdir()
    ce.export_commit(str(dest), False, "HEAD")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x"]):
        subprocess.run(["git", *cmd], cwd=dest, check=True, capture_output=True)
    env = ce.clean_env()
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_export_and_docs.py", "tests/test_clean_export.py",
                        "tests/test_authority_fixes.py::test_the_export_of_the_committed_tree_leaves_out_the_four_internal_files"],
                       cwd=dest, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]
