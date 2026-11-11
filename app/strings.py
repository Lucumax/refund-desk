"""Loads copy/strings.draft.json, the ONLY place user-facing text lives. Every string there can
be rewritten by the owner. Code asks for a key and fills named placeholders."""
from __future__ import annotations
import json
import os

from . import REPO_ROOT

PATH = os.path.join(REPO_ROOT, "copy", "strings.draft.json")


def load(path: str = PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return doc["strings"]


_CACHE: dict | None = None


def all_strings() -> dict:
    global _CACHE
    if _CACHE is None:
        _CACHE = load()
    return _CACHE


def S(key: str, **kw) -> str:
    text = all_strings()[key]
    return text.format(**kw) if kw else text
