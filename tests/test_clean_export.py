"""Sprint 2b: the clean-export check validates a COMMIT (git archive of committed blobs), not the working tree,
keeps a manifest, and names the tracked internal files. Uses a throwaway git repository."""
import json
import os
import shutil
import subprocess
import sys

import pytest

from conftest import ROOT

needs_git = pytest.mark.skipif(shutil.which("git") is None or not os.path.isdir(os.path.join(ROOT, ".git")),
                               reason="needs git (a clean export has no .git)")
needs_private_tree = pytest.mark.skipif(not os.path.isdir(os.path.join(ROOT, ".git")) or not os.path.exists(os.path.join(ROOT, "team", "README_TEAM.md")),
                                        reason="a public export: the internal notes are not in this tree")
SCRIPTS = os.path.join(ROOT, "scripts")


def ce():
    sys.path.insert(0, SCRIPTS)
    try:
        import clean_export_check
        return clean_export_check
    finally:
        sys.path.remove(SCRIPTS)


def g(root, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], cwd=root, check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    (r / "team").mkdir(parents=True)
    g(r, "init", "-q")
    (r / "app.txt").write_text("committed\n")
    (r / "BUILD_REPORT.md").write_text("private\n")
    (r / "team" / "README_TEAM.md").write_text("private\n")
    g(r, "add", "."), g(r, "commit", "-q", "-m", "one")
    return r


@needs_git
def test_the_export_holds_the_committed_blob_not_the_edited_working_file(repo, tmp_path):
    (repo / "app.txt").write_text("EDITED AND NOT COMMITTED\n")
    (repo / "untracked.txt").write_text("never committed\n")
    out = tmp_path / "out"
    out.mkdir()
    n, left_out, manifest = ce().export_commit(str(out), False, "HEAD", str(repo))
    assert (out / "app.txt").read_text() == "committed\n"
    assert not (out / "untracked.txt").exists()
    assert sorted(left_out) == ["BUILD_REPORT.md", "team/README_TEAM.md"] and n == 1
    assert set(manifest["files"]) == {"app.txt"} and manifest["left_out"] == left_out


@needs_git
def test_the_manifest_is_deterministic_and_changes_with_the_commit(repo, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    m1 = ce().export_commit(str(a), False, "HEAD", str(repo))[2]
    m2 = ce().export_commit(str(b), False, "HEAD", str(repo))[2]
    assert m1 == m2 and len(m1["content_digest_sha256"]) == 64 and len(m1["commit"]) == 40
    (repo / "app.txt").write_text("second\n")
    g(repo, "commit", "-qam", "two")
    c = tmp_path / "c"
    c.mkdir()
    m3 = ce().export_commit(str(c), False, "HEAD", str(repo))[2]
    assert m3["content_digest_sha256"] != m1["content_digest_sha256"] and m3["commit"] != m1["commit"]
    old = tmp_path / "old"
    old.mkdir()
    assert ce().export_commit(str(old), False, "HEAD~1", str(repo))[2]["content_digest_sha256"] == m1["content_digest_sha256"]


@needs_git
def test_keep_notes_exports_the_internal_files_too(repo, tmp_path):
    out = tmp_path / "k"
    out.mkdir()
    n, left_out, _ = ce().export_commit(str(out), True, "HEAD", str(repo))
    assert n == 3 and left_out == [] and (out / "BUILD_REPORT.md").exists()


@needs_git
@needs_private_tree
def test_the_internal_files_that_must_stay_out_are_listed_for_this_repository():
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "clean_export_check.py"), "--list-internal"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0
    listed = r.stdout.split()
    for must in ("PROPOSED_CORE_CHANGES.md", "team/README_TEAM.md", "docs/internal/CHANGES_LOG.md"):
        assert must in listed, listed
    assert any(os.path.basename(x).startswith("CODEX_REVIEW_") for x in listed)


def test_install_is_offline_by_default_and_the_browser_pass_fails_on_findings():
    src = open(os.path.join(SCRIPTS, "clean_export_check.py"), encoding="utf-8").read()
    assert '"--no-index"' in src and "--allow-network" in src and "PIP_CACHE_DIR" in src
    js = open(os.path.join(SCRIPTS, "browser_pass.js"), encoding="utf-8").read()
    assert "process.exit(2)" in js and "unexpected" in js
