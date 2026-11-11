"""Order book: the merchant's own list of paid orders and their PayPal capture IDs.
The demo reads fixtures/paypal/orders.json (invented samples). For a real sandbox, pass
your own file with the same shape (see README)."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass

from . import REPO_ROOT, fx
from .money import to_minor

DEFAULT_ORDERS = os.path.join(REPO_ROOT, "fixtures", "paypal", "orders.json")


@dataclass(frozen=True)
class Order:
    """`total_minor` is always US-dollar hundredths: everything this build counts, holds and refunds is in US dollars. For an order
    captured in another currency that has a demo rate (app/fx.py) it is the native total converted at that FIXED DEMO RATE, and
    `native_total_minor` is what the buyer paid; with no demo rate it is the native figure and `usd_convertible` is False (such an
    order is never refunded: the buyer is told a person will look)."""
    order_ref: str
    capture_id: str
    total_minor: int
    currency: str
    item: str
    buyer_label: str
    native_total_minor: int | None = None

    @classmethod
    def from_native(cls, order_ref: str, capture_id: str, native_minor: int, currency: str, item: str = "",
                    buyer_label: str = "") -> "Order":
        usd = fx.usd_minor(native_minor, currency)
        if usd is None or currency == "USD":
            return cls(order_ref, capture_id, native_minor, currency, item, buyer_label)
        return cls(order_ref, capture_id, usd, currency, item, buyer_label, native_minor)

    @property
    def usd_convertible(self) -> bool:
        return fx.convertible(self.currency)


class OrderBook:
    def __init__(self, orders: list[Order]):
        self.by_ref = {o.order_ref: o for o in orders}
        self.by_capture = {o.capture_id: o for o in orders}

    @classmethod
    def load(cls, path: str = DEFAULT_ORDERS) -> "OrderBook":
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)["orders"]
        bad = sorted(o.get("order_ref", "?") for o in raw if o.get("currency") != "USD")
        if bad:
            # every refund this build makes is in US dollars
            raise ValueError("Only USD orders are supported; not USD: " + ", ".join(bad))
        return cls([Order(o["order_ref"], o["capture_id"], to_minor(o["total"]), o["currency"],
                          o.get("item", ""), o.get("buyer_label", "")) for o in raw])

    def get(self, order_ref: str | None) -> Order | None:
        return self.by_ref.get(order_ref or "")

    @property
    def capture_ids(self) -> tuple[str, ...]:
        return tuple(self.by_capture)
