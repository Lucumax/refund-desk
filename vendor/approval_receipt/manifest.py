"""Content hash manifest so a downstream repo can verify its vendored copy.

    python -m approval_receipt manifest --check [--root DIR]
    python -m approval_receipt manifest --write [--root DIR]     (core maintainers only)

ROOT is the directory that contains the approval_receipt/ package and VENDOR_MANIFEST.json.
Hashes are SHA-256 over file bytes with CRLF normalised to LF, so Windows checkouts match.
"""
from __future__ import annotations
import hashlib, json, os

MANIFEST = "VENDOR_MANIFEST.json"
PKG = "approval_receipt"


def default_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def _package_files(root: str) -> list[str]:
    out = []
    for d, dirs, files in os.walk(os.path.join(root, PKG)):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        out += [os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                for f in files if not f.endswith(".pyc")]
    return sorted(out)


def build_manifest(root: str | None = None) -> dict:
    from . import __version__
    root = root or default_root()
    return {"version": __version__,
            "files": {rel: _hash(os.path.join(root, rel)) for rel in _package_files(root)}}


def write_manifest(root: str | None = None) -> dict:
    root = root or default_root()
    m = build_manifest(root)
    with open(os.path.join(root, MANIFEST), "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, indent=2, sort_keys=True)
        f.write("\n")
    return m


def verify_manifest(root: str | None = None) -> tuple[bool, list[str]]:
    """Returns (ok, problems). Flags modified, missing and unexpected package files."""
    root = root or default_root()
    try:
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as f:
            want = json.load(f)
    except (OSError, ValueError) as e:
        return False, [f"cannot read {MANIFEST}: {e}"]
    problems, have = [], _package_files(root)
    for rel, h in want["files"].items():
        p = os.path.join(root, rel)
        if not os.path.exists(p):
            problems.append(f"MISSING {rel}")
        elif _hash(p) != h:
            problems.append(f"MODIFIED {rel}")
    problems += [f"UNEXPECTED {rel}" for rel in have if rel not in want["files"]]
    return not problems, problems
