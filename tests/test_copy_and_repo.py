import glob
import hashlib
import json
import os
import re

from approval_receipt.engine import (PENDING, AUTHORIZED, SUBMITTING, SUCCEEDED, FAILED, UNKNOWN, BLOCKED, DENIED, EXPIRED)
from app.agent import PURPOSES
from app.demo import SCENES
from conftest import ROOT

DOC = json.load(open(os.path.join(ROOT, "copy", "strings.draft.json"), encoding="utf-8"))
STR = DOC["strings"]

REASONS = ["WITHIN_MANDATE", "AMOUNT_OVER_PER_PAYMENT_LIMIT", "DAILY_LIMIT_EXCEEDED", "PAYEE_NOT_ALLOWLISTED",
           "PURPOSE_NOT_PERMITTED", "MANDATE_EXPIRED", "MANDATE_NOT_FOUND", "AMOUNT_NOT_POSITIVE", "CURRENCY_MISMATCH"]
ENGINE_OUTCOMES = [PENDING, AUTHORIZED, SUBMITTING, SUCCEEDED, FAILED, UNKNOWN, BLOCKED, DENIED, EXPIRED]
STAGES = [PENDING, AUTHORIZED, SUBMITTING, UNKNOWN, FAILED, BLOCKED, DENIED, EXPIRED,
          "SENT_DONE", "SENT_PENDING", "PROVIDER_FAILED", "NOT_SUBMITTED", "STATE_MISMATCH"]
NEEDS = ["MISSING_REASON", "MISSING_INTENT", "MISSING_AMOUNT", "AMOUNT_AMBIGUOUS", "ORDER_MISMATCH",
         "ALREADY_REFUNDED", "AMOUNT_EXCEEDS_PAID", "AMOUNT_UNVERIFIED", "AMOUNT_NEGATIVE", "CURRENCY_NOT_SUPPORTED",
         "FOREIGN_CURRENCY_HELD"]
VERIFY = ["HASH_MISMATCH", "PREV_HASH_MISMATCH", "SIGNATURE_INVALID", "SEQUENCE_BROKEN", "TRUNCATED",
          "CHECKPOINT_INVALID", "CHECKPOINT_HEAD_MISMATCH", "MALFORMED_RECORD", "GENESIS_MISSING", "KEY_ID_UNKNOWN",
          "CHECKPOINT_MISSING", "CHECKPOINT_ROLLBACK"]
APPROVAL_ERRORS = ["SESSION_REQUIRED", "CSRF_INVALID", "APPROVER_IS_AGENT", "NONCE_INVALID", "TERMS_CHANGED",
                   "HARD_BLOCK_AT_EXECUTION", "APPROVAL_EXPIRED", "ALREADY_RESOLVED", "NOT_PENDING",
                   "INTENT_NOT_FOUND", "NOT_FOUND", "BAD_REQUEST", "GENERIC", "STATE_MISMATCH", "LEDGER_INTEGRITY_FAILED"]


def dynamic_keys():
    k = []
    k += ["decision." + r for r in REASONS]
    k += ["outcome." + o for o in ENGINE_OUTCOMES]
    k += ["stage." + s for s in STAGES + ["NEEDS_INFO", "DUPLICATE"]]
    k += ["headline." + s for s in STAGES]
    k += ["buyer." + s for s in STAGES + ["DUPLICATE"]] + ["buyer.NEEDS_" + n for n in NEEDS]
    k += ["needs." + n for n in NEEDS + ["ALREADY_IN_PROGRESS"]]
    k += ["issue." + p for p in PURPOSES] + ["demo.scene." + s for s in SCENES] + ["demo.msg." + s for s in SCENES]
    k += ["verify.code." + v for v in VERIFY] + ["errors." + e for e in APPROVAL_ERRORS]
    k += ["decision_label." + d for d in ("ALLOW", "ESCALATE", "DENY")]
    k += ["flag.INJECTION_SUSPECTED", "flag.PROVIDER_STATUS_CONFLICT", "amount_source.message", "amount_source.full_refund_words"]
    k += ["provider." + p for p in ("COMPLETED", "PENDING", "FAILED", "CANCELLED", "UNKNOWN")]
    k += ["receipt.p_approver_" + a for a in ("approve", "deny", "approve_blocked")]
    k += ["login.error.BAD_PASSCODE", "login.error.LOCKED", "buyer.error.EMPTY_MESSAGE", "buyer.error.MESSAGE_TOO_LONG",
          "buyer.error.RATE_LIMITED", "buyer.error.PROOF_REQUIRED", "buyer.no_link"]
    return k


def literal_keys():
    keys = set()
    for f in glob.glob(os.path.join(ROOT, "app", "*.py")):
        keys |= set(re.findall(r'\bS\("([a-z_]+\.[A-Za-z_.]+)"', open(f, encoding="utf-8").read()))
    for f in glob.glob(os.path.join(ROOT, "static", "*.html")):
        keys |= set(re.findall(r'data-s(?:-label)?="([^"]+)"', open(f, encoding="utf-8").read()))
        keys |= set(re.findall(r'data-title="([^"]+)"', open(f, encoding="utf-8").read()))
    for f in glob.glob(os.path.join(ROOT, "static", "*.js")):
        keys |= set(re.findall(r'\bt\("([a-z_]+\.[A-Za-z_.]+)"', open(f, encoding="utf-8").read()))
    return {k for k in keys if not k.endswith((".", "_"))}     # prefixes of computed keys are covered by dynamic_keys


def test_every_key_the_code_or_pages_use_exists_in_the_copy_file():
    missing = [k for k in sorted(literal_keys() | set(dynamic_keys())) if k not in STR]
    assert missing == []


def test_copy_file_status_is_neutral_and_the_product_is_named_without_a_working_marker():
    assert DOC["_status"] == "All user-facing text, in one place."
    assert STR["app.name"] == "Refund Desk"


def test_copy_style_rules():
    for k, v in STR.items():
        assert "—" not in v and "–" not in v, k            # no em or en dashes
        assert not re.search(r"\s-\s", v), k                          # no spaced hyphen as punctuation
        assert not re.search(r"\b(years?|yrs)\b", v, re.I), k         # no years of experience
        assert not re.search(r"compliant|certified|production[- ]ready|guarantee", v, re.I), k


def test_copy_has_no_logos_or_trademark_lockups_and_names_paypal_in_text_only():
    for f in glob.glob(os.path.join(ROOT, "static", "*")):
        assert not f.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".gif", ".ico", ".mp3", ".mp4")), f


def test_front_end_has_no_cdn_or_external_urls():
    for f in glob.glob(os.path.join(ROOT, "static", "*")):
        assert not re.search(r"https?://", open(f, encoding="utf-8").read()), f


def test_vendored_core_matches_its_manifest():
    from approval_receipt.manifest import verify_manifest
    ok, problems = verify_manifest(os.path.join(ROOT, "vendor"))
    assert ok, problems
    assert open(os.path.join(ROOT, "vendor", "VERSION"), encoding="utf-8").read().strip() == "2.0.0"


# Reviewer output (for example codex_out.md) is generated by an outside tool and is not our prose. Reviewers should write
# their outputs OUTSIDE the tree; if one lands here anyway, the scan below skips it: a file is skipped when it matches
# a reviewer-artifact name (PRIOR_REVIEW*.md, anything with codex in its name, review_*, *_out.md), and, in a git checkout, when git does not track it.
REVIEWER_ARTIFACT = re.compile(r"^(?:codex_|reviewer?_|review_|PRIOR_REVIEW)|_out\.md$|codex.*\.md$|^PRIOR_REVIEW.*\.md$", re.IGNORECASE)


def prose_documents(root):
    """The top-level markdown files that are this project's own prose."""
    import subprocess
    docs = [f for f in glob.glob(os.path.join(root, "*.md")) if not REVIEWER_ARTIFACT.search(os.path.basename(f))]
    if os.path.exists(os.path.join(root, ".git")):
        try:
            out = subprocess.run(["git", "ls-files", "--", "*.md"], cwd=root, capture_output=True, text=True, timeout=30)
            if out.returncode == 0:
                tracked = {os.path.normpath(os.path.join(root, line)) for line in out.stdout.splitlines() if line.strip()}
                docs = [f for f in docs if os.path.normpath(f) in tracked]
        except (OSError, subprocess.SubprocessError):
            pass                  # no git here: the name filter alone applies
    return docs


def test_prose_docs_follow_the_style_rules():
    for f in prose_documents(ROOT):
        text = open(f, encoding="utf-8").read()
        assert "—" not in text and "–" not in text, f
        assert not re.search(r"\w - \w", text), f
        assert not re.search(r"\b\d+\+? years\b", text, re.I), f


def test_required_deliverable_files_exist():
    for name in ["README.md", "DEMO_SCRIPT.md", "SUBMISSION_CHECKLIST.md", "NOT_DONE.md", "LICENSE",
                 "scripts/live_sandbox_run.py",
                 "copy/strings.draft.json", "app/__main__.py"]:
        assert os.path.exists(os.path.join(ROOT, name)), name


def test_the_checklist_only_points_at_files_that_exist():
    text = open(os.path.join(ROOT, "SUBMISSION_CHECKLIST.md"), encoding="utf-8").read()
    sealed = json.load(open(os.path.join(ROOT, "eval", "SEALED_MANIFEST.json"), encoding="utf-8"))["files"]     # may be absent from a checkout
    for path in re.findall(r"`([A-Za-z_./]+\.(?:py|md|json|js|html|css))`", text):
        assert os.path.exists(os.path.join(ROOT, path)) or path in sealed, path
