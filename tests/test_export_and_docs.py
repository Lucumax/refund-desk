"""Sprint 2 tests (2026-10-24): toolkit facts and sources, clean-export hygiene, the evaluation harness
and its held-out seal, UNKNOWN ("cut the line") handling, and the Policy Builder polish. Everything runs on
the synthetic fixture and scripted readers. No network, no credentials, no live model."""
import glob
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import ROOT

SCRIPTS = os.path.join(ROOT, "scripts")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


# ---- 1. the toolkit facts, with sources ---------------------------------------------------------------
PUBLIC_DOCS = ["README.md", "NOT_DONE.md", "SUBMISSION_CHECKLIST.md", "DEMO_SCRIPT.md", "PROPOSED_CORE_CHANGES.md",
               "CONTRIBUTING.md",
               os.path.join("team", "README_TEAM.md"), os.path.join("team", "PLAN_FOR_CONTRIBUTORS.md"),
               os.path.join("copy", "strings.draft.json")]


def test_no_file_repeats_the_false_no_refund_tool_claim():
    for name in PUBLIC_DOCS + ["app/paypal_adapter.py", "scripts/live_sandbox_run.py"]:
        if not os.path.exists(os.path.join(ROOT, name)):
            continue
        text = read(name)
        assert not re.search(r"no refund tool|no refund (?:or|and) (?:no )?payouts?|lacks (?:a )?refund tool|without (?:a )?refund tool", text, re.I), name


def test_readme_cites_the_toolkit_sources_and_the_conflicting_page():
    text = read("README.md") + "\n" + read("docs/DETAILS.md")
    assert "https://github.com/paypal/agent-toolkit" in text
    assert "https://paypal.gitbook.com/agent-toolkit-and-mcp-server/available-tools.md" in text
    assert "create_refund" in text and "get_refund" in text and "2026-10-24" in text
    assert "which tools a given token exposes was not tested" in text


def test_the_readme_makes_no_claim_about_other_products_approval_gates():
    # The "hosted MCP server has no approval gate" statement rests on one third-party comparison and is not made.
    for name in ("README.md", "DEMO_SCRIPT.md", "SUBMISSION_CHECKLIST.md"):
        assert not re.search(r"(?:hosted|remote) (?:MCP|server)[^.]{0,60}no (?:server-side )?approval", read(name), re.I), name


# These two need a git checkout (they run `git ls-files`); inside a clean export there is no .git, so they skip.
needs_git = pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, ".git")), reason="not a git checkout (clean export)")
# The internal notes (team/README_TEAM.md and others) exist only in the private working repository. A public repository built from the
# export has a .git but not these files, so a test that expects the export to LEAVE THEM OUT has nothing to leave out there.
needs_private_tree = pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, ".git")) or not os.path.exists(os.path.join(ROOT, "team", "README_TEAM.md")),
                                        reason="a public export (or a clean export): the internal notes are not in this tree")


# ---- 2. clean-export hygiene ------------------------------------------------------------------------------
def _export_module():
    sys.path.insert(0, SCRIPTS)
    try:
        import clean_export_check
        return clean_export_check
    finally:
        sys.path.remove(SCRIPTS)


@needs_private_tree
def test_clean_export_leaves_internal_notes_out_and_copies_the_rest(tmp_path):
    ce = _export_module()
    n, left_out = ce.copy_export(str(tmp_path), keep_notes=False)
    assert n > 50
    assert "team/README_TEAM.md" in left_out and not (tmp_path / "team" / "README_TEAM.md").exists()
    for must in ("LICENSE", "README.md", "app/__init__.py", "tests/conftest.py", "vendor/VENDOR_MANIFEST.json",
                 "requirements-dev.txt", "scripts/clean_export_check.py"):
        assert (tmp_path / must).is_file(), must


def test_clean_export_environment_has_no_keys(monkeypatch):
    ce = _export_module()
    for k in ("PAYPAL_CLIENT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "APP_MODEL_ID", "APPROVAL_RECEIPT_HMAC_KEY"):
        monkeypatch.setenv(k, "x")
    env = ce.clean_env()
    assert not [k for k in env if k.startswith(("PAYPAL", "ANTHROPIC", "OPENAI", "APP_MODEL", "APPROVAL_RECEIPT"))]


@needs_git
def test_the_license_and_deliverable_tests_pass_in_a_clean_export(tmp_path):
    """Run the licence and deliverable tests in a copy that has none of the internal notes."""
    ce = _export_module()
    ce.copy_export(str(tmp_path), keep_notes=False)
    assert not (tmp_path / "team").exists()
    env = ce.clean_env()
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_hardening.py::test_license_is_the_mit_license_walter_chose",
                        "tests/test_copy_and_repo.py::test_required_deliverable_files_exist"],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_license_is_a_single_mit_file_with_no_pending_licence_note():
    """The MIT licence is final: no placeholder note sits next to it, in the tree or in the tracked files."""
    assert os.path.isfile(os.path.join(ROOT, "LICENSE"))
    assert not [f for f in os.listdir(ROOT) if f.upper().startswith("LICENSE") and f != "LICENSE"]


# ---- 6. fixes from the real-browser pass -----------------------------------------------------------------------
def test_browser_pass_fixes_are_in_place(env):
    css = read("static/app.css")
    assert 'button.preset .mono' in css and 'white-space: nowrap' in css
    assert '.chip[data-r="HIT"][data-d="ALLOW"]' in css                 # the allowing rule is not drawn as an alarm
    assert "minmax(0, 1fr)" in css and ".cols > section { min-width: 0; }" in css
    r = env.get("/favicon.ico", headers={})
    assert r.status == 204 and r.body == b""
    assert 'data-d": g.decision' in read("static/desk.js") and 'data-d": g.decision' in read("static/arena.js")


def test_the_browser_pass_script_and_findings_are_kept():
    assert os.path.exists(os.path.join(ROOT, "scripts", "browser_pass.js"))
    folder = os.path.join(ROOT, "qa", "browser-pass-2026-10-24")
    assert os.path.exists(os.path.join(folder, "FINDINGS.md"))
    assert len(glob.glob(os.path.join(folder, "*.png"))) >= 10
    assert "127.0.0.1" in read("scripts/browser_pass.js")
