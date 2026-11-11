"""Demo exchange rates (following Dhanush Kankanala's policy answer that a buyer who writes an
amount in another currency is not asked to restate it).

These are FIXED DEMO RATES, written in this file. They are not market rates, they are not fetched, and they do not move. They exist
so that the simulation can compare a figure written in euros, pounds, Canadian or Australian dollars with the owner's limits,
which are in US dollars. Everything this build counts, holds and refunds is in US dollars: a figure in another currency is turned
into US dollars at the rate below, rounded half up to the cent, BEFORE any limit is checked, and the refund that may follow is in
US dollars. A real PayPal refund must be made in the currency the buyer paid in; that is not done here, and an orders file for a
real sandbox must still be all US dollars (app/orders.py).

What is converted: a figure written with a currency sign or an ISO code beside it, in one of these forms only: the sign before the
figure (€18.00, £18.00), the code before or after it (EUR 18.00, 18.00 GBP, CAD 18, 18 AUD), or the word "euro" after it (18 euros).
The words "pounds" and "dollars" are NOT converted on their own ("2 pounds of coffee", "Canadian dollars"). Anything else that names
a currency, or any figure that is not one well-formed figure, is left exactly as written and stays held for the owner by the gate's
foreign-currency rule (app/arena_gate.py, G12), as before."""
from __future__ import annotations
import re
from decimal import Decimal, ROUND_HALF_UP

# 1 unit of the currency = this many US dollars. DEMO RATES: not market rates.
DEMO_RATES_USD = {"EUR": Decimal("1.08"), "GBP": Decimal("1.27"), "CAD": Decimal("0.73"), "AUD": Decimal("0.65")}

_SIGNS = {"€": "EUR", "£": "GBP"}
_NUM = r"(?:[1-9]\d{0,2}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
_TAIL = r"(?![.,]?\d)(?![.,]{2}\d)(?![A-Za-z_$])"
_LEAD = r"(?<![\w.,$€£])"
_CODES = "|".join(DEMO_RATES_USD)
_SIGN_FIRST = re.compile(r"(?<![\w$€£])([€£])\s?(" + _NUM + r")" + _TAIL, re.ASCII)
_CODE_FIRST = re.compile(_LEAD + r"\b(" + _CODES + r")\s?(" + _NUM + r")" + _TAIL, re.IGNORECASE | re.ASCII)
_CODE_AFTER = re.compile(_LEAD + r"(" + _NUM + r")\s?(" + _CODES + r"|euros?)\b(?!\s?\d)", re.IGNORECASE | re.ASCII)


def convertible(currency: str | None) -> bool:
    """True for US dollars and for a currency that has a demo rate."""
    return currency == "USD" or (currency or "").upper() in DEMO_RATES_USD


def usd_minor(native_minor: int, currency: str) -> int | None:
    """`native_minor` hundredths of `currency` in US-dollar hundredths at the demo rate, rounded half up; None when the currency has
    no demo rate."""
    if currency == "USD":
        return native_minor
    rate = DEMO_RATES_USD.get((currency or "").upper())
    if rate is None:
        return None
    return int((Decimal(native_minor) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _minor(figure: str) -> int:
    return int((Decimal(figure.replace(",", "")) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _dollars(minor: int) -> str:
    return f"${minor // 100}.{minor % 100:02d}"


def rate_note(currency: str) -> str:
    """The sentence shown wherever a conversion is shown. Fixed wording, no buyer words."""
    return f"1 {currency} = {DEMO_RATES_USD[currency]} USD (fixed demo rate, not a market rate)"


def convert_text(text: str) -> tuple[str, list[dict]]:
    """(working text, conversions). Each recognised figure in another currency is replaced by its US-dollar figure; the buyer's own
    words are kept in the request file and in the signed record (`fx`). With nothing to convert the text is returned unchanged
    (the same object) and the list is empty. Deterministic, so the same message always gives the same working text."""
    if not isinstance(text, str):
        return text, []
    found = []          # (start, end, currency, native figure text)
    for m in _SIGN_FIRST.finditer(text):
        found.append((m.start(), m.end(), _SIGNS[m.group(1)], m.group(2)))
    for m in _CODE_FIRST.finditer(text):
        found.append((m.start(), m.end(), m.group(1).upper(), m.group(2)))
    for m in _CODE_AFTER.finditer(text):
        code = m.group(2).upper()
        found.append((m.start(), m.end(), "EUR" if code.startswith("EURO") else code, m.group(1)))
    if not found:
        return text, []
    found.sort()
    kept, last_end = [], -1
    for f in found:
        if f[0] >= last_end:             # a figure two forms both matched is converted once
            kept.append(f)
            last_end = f[1]
    out, pos, notes = [], 0, []
    for start, end, cur, figure in kept:
        native = _minor(figure)
        usd = usd_minor(native, cur)
        out.append(text[pos:start])
        out.append(_dollars(usd))
        pos = end
        written = text[start:end]
        notes.append({"written": written, "currency": cur, "native_minor": native, "usd_minor": usd,
                      "rate": str(DEMO_RATES_USD[cur]), "note": rate_note(cur),
                      "label": f"{written} is counted as {_dollars(usd)} ({rate_note(cur)})"})
    out.append(text[pos:])
    return "".join(out), notes
