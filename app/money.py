"""Money helpers. All amounts are integer minor units (cents) in USD."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


MAX_DIGITS = 13                    # up to 9,999,999,999,999.99 dollars; anything larger is "too large", not a crash
ABSURD_MINOR = 10 ** (MAX_DIGITS + 2)   # what a figure that is too large reads as: above every cap and every order total


def to_minor(value: str) -> int:
    """'18.00' or '18' to 1800. Raises ValueError on anything that is not a plain decimal."""
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation as e:
        raise ValueError("NOT_A_NUMBER") from e
    if not d.is_finite():
        raise ValueError("NOT_A_NUMBER")
    if abs(d) >= 10 ** MAX_DIGITS:
        raise ValueError("AMOUNT_TOO_LARGE")           # never a decimal-arithmetic exception on a huge figure
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_value(minor: int) -> str:
    """1800 to '18.00' (the form PayPal-style APIs use for amounts)."""
    sign = "-" if minor < 0 else ""
    m = abs(minor)
    return f"{sign}{m // 100}.{m % 100:02d}"


def fmt(minor: int, currency: str = "USD") -> str:
    """Display form. USD only in this build."""
    return ("-" if minor < 0 else "") + f"${abs(minor) // 100:,}.{abs(minor) % 100:02d}"
