"""Text helpers shared by the Injection Arena reader and gate: normalizing hostile text and finding
money figures, including amounts written in words. Pure functions, standard library only."""
from __future__ import annotations
import re
import unicodedata

from .extractor import find_amounts

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
# Look-alike letters people use to dodge keyword checks: Cyrillic and Greek letters (after lower-casing), Latin small
# capitals, and a few Latin variants. It folds what it knows; the refund-consent allowlist (app/consent.py) does not rely on
# this list being complete, because it refuses any character that is still not plain ASCII after the fold.
_LOOKALIKES = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ѕ": "s",
    "ј": "j", "ԛ": "q", "ӏ": "l", "ԁ": "d", "ь": "b",
    # Greek
    "ο": "o", "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "η": "n", "μ": "u", "β": "b", "γ": "y",
    "ω": "w", "σ": "o", "ζ": "z", "θ": "o", "π": "n", "δ": "d", "λ": "l",
    # Latin variants and small capitals
    "ı": "i", "ɡ": "g", "ɑ": "a", "ſ": "s", "ȷ": "j", "ǀ": "l", "ɩ": "i",
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ɢ": "g", "ʜ": "h",
    "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o",
    "ᴘ": "p", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w",
    "ʏ": "y", "ᴢ": "z",
}
_CONFUSABLE = str.maketrans(_LOOKALIKES)


_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u00b4": "'", "`": "'"})


def normalize(text: str) -> str:
    """Lower case, fold look-alike characters, drop invisible characters, collapse spaces."""
    s = unicodedata.normalize("NFKC", text or "").lower().translate(_ZERO_WIDTH).translate(_CONFUSABLE).translate(_APOSTROPHES)
    return " ".join(s.split())


def squeezed(text: str) -> str:
    """Letters and digits only, so 'i g n o r e' and 'i.g.n.o.r.e' both become 'ignore'."""
    return re.sub(r"[^a-z0-9]+", "", normalize(text))


_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_NUMWORD = "|".join(sorted(list(_UNITS) + list(_TENS) + ["hundred", "thousand", "and", "a"], key=len, reverse=True))
_WORD_AMOUNT = re.compile(r"\b((?:(?:" + _NUMWORD + r")[\s-]+)*(?:" + _NUMWORD + r"))\s+(dollars?|usd|bucks|grand)\b")


def _words_to_int(words: str) -> int | None:
    total, cur, seen = 0, 0, False
    for tok in re.split(r"[\s-]+", words):
        if tok in ("and", ""):
            continue
        if tok == "a":
            cur = cur or 1
            continue
        seen = True
        if tok in _UNITS:
            cur += _UNITS[tok]
        elif tok in _TENS:
            cur += _TENS[tok]
        elif tok == "hundred":
            cur = (cur or 1) * 100
        elif tok == "thousand":
            total += (cur or 1) * 1000
            cur = 0
        else:
            return None
    return total + cur if seen else None


def word_amounts(text: str) -> list[int]:
    """Amounts written in words ('one thousand dollars', 'five grand'), in minor units."""
    out = []
    for m in _WORD_AMOUNT.finditer(normalize(text)):
        n = _words_to_int(m.group(1))
        if n is None:
            continue
        out.append(n * (1000 if m.group(2) == "grand" else 1) * 100)
    return [x for x in out if x > 0]


def figures(text: str) -> list[int]:
    """Every money figure in the text, digits and words, in order of appearance (digits first)."""
    return [a[0] for a in find_amounts(text)] + word_amounts(text)
