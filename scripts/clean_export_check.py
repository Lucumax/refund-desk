"""Clean-export check: does an export of a COMMIT install and pass its own tests?

What it does, in order:
1. Resolves a commit (default HEAD) and reads its tree with `git archive`, so it checks the COMMITTED blobs, not the
   working tree. Edits that are not committed, and files that are not tracked, are never in the export (a notice
   says when the working tree differs). INTERNAL notes (files for the owner, not for a public repository) are left out.
2. Writes a manifest (commit id, every exported file with its SHA-256 and size, the files left out, and one content
   digest over all of it) so a reviewer can compare what was checked with what is published. Same commit, same manifest.
3. Creates a virtual environment and installs requirements-dev.txt WITHOUT the network (`--no-index`, a cache inside the
   temporary directory). By default the environment can SEE the system site packages, so a pass proves the export runs
   against this machine's packages, not that requirements-dev.txt alone is enough; the installed packages are listed so
   a reviewer can see what was used. `--isolated` leaves the system packages out (then pip needs `--find-links DIR` with
   wheels, or `--allow-network`). Use --allow-network to let pip download; use --skip-install to reuse the current
   interpreter.
4. Runs the vendored-core manifest check and the full test suite from inside the export, with every PayPal,
   model-provider and signing-key environment variable removed.

It never uses credentials and never changes the repository.

IMPORTANT: this checks what a SEPARATELY BUILT export would contain. Pushing this BRANCH publishes every tracked file
and the whole history, including the INTERNAL files below and anything ever committed. To publish only the export,
build a new repository from it (and say that the history is not included).

    python scripts/clean_export_check.py                       # HEAD, internal notes left out, manifest written
    python scripts/clean_export_check.py --commit <sha>        # a specific commit (the frozen one)
    python scripts/clean_export_check.py --keep-notes          # also export the internal notes
    python scripts/clean_export_check.py --list-internal       # tracked files that must stay out of a public export
    python scripts/clean_export_check.py --skip-install -k test_export_and_docs   # extra args go to pytest
Exit code 0 only if every step passes."""
from __future__ import annotations
import argparse
import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files that exist for the owner and do not belong in a public export. Tests must pass without them.
INTERNAL = ("BUILD_REPORT.md", "LOCAL_SETUP_PRIVATE.md", "SPRINT1_REPORT.md", "SPRINT2_REPORT.md",
            "CODEX_REVIEW_*.md", "PROPOSED_CORE_CHANGES.md", "team/*",
            "docs/SESSION_REPORT_*.md", "docs/internal/*", "submission/*",       # session logs and drafts: not for a public repository
            "docs/PREFREEZE_*", "docs/README_PASS_*",                            # internal working notes
            ".github/CODEOWNERS",                                                # names private collaborators
            ".github/ISSUE_TEMPLATE/*", ".github/PULL_REQUEST_TEMPLATE.md",     # team process templates
            "qa/export-manifest.json",                                           # export tooling output; it lists the files left out
            "eval/heldout_corpus.json")      # the sealed corpus: its hashes stay in eval/SEALED_MANIFEST.json
SECRET_ENV = ("PAYPAL", "ANTHROPIC", "OPENAI", "APP_MODEL", "APPROVAL_RECEIPT", "APP_PASSCODE", "ARENA_READER")
DEFAULT_MANIFEST = os.path.join(ROOT, "qa", "export-manifest.json")


def git(args: list[str], root: str = ROOT) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def resolve(ref: str, root: str = ROOT) -> str:
    return git(["rev-parse", "--verify", ref + "^{commit}"], root).decode().strip()


def tracked_files(ref: str = "HEAD", root: str = ROOT) -> list[str]:
    """Paths in the COMMIT's tree (not the index, not the working tree)."""
    out = git(["ls-tree", "-r", "-z", "--name-only", ref], root).decode("utf-8")
    return [p for p in out.split("\0") if p]


def is_internal(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in INTERNAL)


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not any(k.upper().startswith(p) for p in SECRET_ENV)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def copy_export(dest: str, keep_notes: bool, ref: str = "HEAD", root: str = ROOT) -> tuple[int, list[str]]:
    """Extract the committed tree of `ref` into `dest`. Returns (files copied, files left out)."""
    n, left_out, _ = export_commit(dest, keep_notes, ref, root)
    return n, left_out


def export_commit(dest: str, keep_notes: bool, ref: str = "HEAD", root: str = ROOT) -> tuple[int, list[str], dict]:
    sha = resolve(ref, root)
    data = git(["archive", "--format=tar", sha], root)
    files, left_out = {}, []
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            if not keep_notes and is_internal(m.name):
                left_out.append(m.name)
                continue
            blob = tar.extractfile(m).read()
            target = os.path.join(dest, m.name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(blob)
            files[m.name] = {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
    left_out.sort()
    body = {"commit": sha, "internal_notes_included": keep_notes, "files": dict(sorted(files.items())),
            "left_out": left_out}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    manifest = {**body, "file_count": len(files), "content_digest_sha256": hashlib.sha256(canon).hexdigest()}
    return len(files), left_out, manifest


def working_tree_differs(root: str = ROOT) -> list[str]:
    out = git(["status", "--porcelain"], root).decode("utf-8")
    return [line for line in out.splitlines() if line.strip()]


def run(label: str, cmd: list[str], cwd: str, env: dict) -> bool:
    print(f"--- {label}: {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=cwd, env=env)
    print(f"--- {label}: {'ok' if r.returncode == 0 else 'FAILED (exit %d)' % r.returncode}", flush=True)
    return r.returncode == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", default="HEAD", help="the commit to export (default HEAD)")
    ap.add_argument("--keep-notes", action="store_true", help="also export the internal notes")
    ap.add_argument("--list-internal", action="store_true", help="list tracked files that must stay out of a public export")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST, help="where to write the export manifest ('' to skip)")
    ap.add_argument("--skip-install", action="store_true", help="use the current interpreter, no virtual environment")
    ap.add_argument("--allow-network", action="store_true", help="let pip download requirements (default: offline)")
    ap.add_argument("--isolated", action="store_true", help="build the virtual environment WITHOUT the system site packages")
    ap.add_argument("--find-links", default=None, help="a folder of wheels for an offline --isolated install")
    ap.add_argument("--keep-dir", action="store_true", help="do not delete the temporary export afterwards")
    args, pytest_args = ap.parse_known_args(argv)
    sha = resolve(args.commit)
    if args.list_internal:
        for rel in tracked_files(sha):
            if is_internal(rel):
                print(rel)
        return 0
    dirty = working_tree_differs()
    if dirty:
        print(f"NOTE: the working tree has {len(dirty)} uncommitted or untracked path(s); none of them is in this export:")
        for line in dirty[:12]:
            print("   ", line)
    tmp = tempfile.mkdtemp(prefix="refund-desk-export-")
    dest = os.path.join(tmp, "export")
    os.makedirs(dest)
    ok = False
    try:
        n, left_out, manifest = export_commit(dest, args.keep_notes, sha)
        print(f"exported commit {sha}: {n} files to {dest}; left out {len(left_out)} internal notes: {', '.join(left_out) or 'none'}")
        print("content digest:", manifest["content_digest_sha256"])
        if args.manifest:
            os.makedirs(os.path.dirname(args.manifest), exist_ok=True)
            with open(args.manifest, "w", encoding="utf-8", newline="\n") as f:
                json.dump(manifest, f, indent=1, sort_keys=True)
                f.write("\n")
            print("manifest written to", args.manifest)
        print("reminder: pushing the BRANCH publishes all tracked files and history, including the internal notes above;"
              " this export is what a separately built public repository would contain.")
        env = clean_env()
        py = sys.executable
        if not args.skip_install:
            venv = os.path.join(tmp, "venv")
            venv_cmd = [sys.executable, "-m", "venv", venv] if args.isolated else [sys.executable, "-m", "venv", "--system-site-packages", venv]
            if not run("venv", venv_cmd, dest, env):
                return 1
            print("environment:", "ISOLATED (no system site packages)" if args.isolated
                  else "can see the system site packages (use --isolated to leave them out)", flush=True)
            py = os.path.join(venv, "Scripts" if os.name == "nt" else "bin", "python")
            pip = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir"]
            if not args.allow_network:
                pip.append("--no-index")
            if args.find_links:
                pip += ["--find-links", args.find_links]
            if not run("install", [*pip, "-r", "requirements-dev.txt"], dest, {**env, "PIP_CACHE_DIR": os.path.join(tmp, "pipcache")}):
                return 1
            run("installed packages", [py, "-m", "pip", "list", "--disable-pip-version-check"], dest, env)
        vendor = os.path.join(dest, "vendor")
        steps = [("vendor manifest", [py, "-m", "approval_receipt", "manifest", "--check", "--root", "."], vendor),
                 ("tests", [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", *pytest_args], dest)]
        ok = all(run(label, cmd, cwd, env) for label, cmd, cwd in steps)
        print("CLEAN EXPORT CHECK:", "PASS" if ok else "FAIL", "for commit", sha)
        return 0 if ok else 1
    finally:
        if args.keep_dir:
            print("kept", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
