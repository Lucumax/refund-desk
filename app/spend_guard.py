"""A hard USD cap on model calls (2026-11-01).

Every call that goes through app/model_client.make_completer(guard=...) is RESERVED here before the request is built: the reservation
is an upper bound (input size estimate plus the full max_tokens at the model's output price). A call whose reservation does not fit
under the cap is never sent. After the reply, the reservation is replaced by the cost of the usage the provider reports.

  - the cap counts money already spent (this process, and earlier commands when a ledger file is shared) plus calls in flight;
  - a call that fails after it may have been billed (timeout, connection error, HTTP 5xx) is charged in full;
    a call the provider rejected before running it (HTTP 4xx) is refunded, and a rejected key (401/403) halts the run at once;
  - three failed calls in a row halt the run: a dead key or a dead network must not turn a "live" run into a rules-only run;
  - once halted (cap, key, streak, a call that cost more than was reserved) every later reservation is refused;
  - guards that share one ledger file share one budget: every reserve, settle and fail runs under an exclusive file lock on the ledger
    and starts from what the file says, so two commands (or two guards in one process) cannot both spend the same dollars.
    The arena's model reader and the desk's readers use ONE guard per session (app/__main__.py).

PRICES: Anthropic first-party API, base input and output rates per million tokens, global endpoint, no caching, no batch discount.
Source: PRICE_SOURCE. Read from the live pricing page on 2026-11-01 (a page read, not a billed call). A model id that is not in the
table has no price and the guard refuses to start: it never guesses.

Money is Decimal; nothing here touches the network and nothing here ever holds, prints or stores a key."""
from __future__ import annotations
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

PRICE_SOURCE = ("platform.claude.com/docs/en/about-claude/pricing, 'Model pricing' table, read 2026-11-01: base input and output "
                "USD per million tokens (global endpoint, no cache, no batch discount; US-only inference would add 1.1x)")
# model id -> (input $/MTok, output $/MTok). Haiku 5.5 charges more when a prompt is over 100K tokens: see _LONG.
PRICES = {
    "claude-fable-5-1": (Decimal("10"), Decimal("50")),
    "claude-fable-5": (Decimal("10"), Decimal("50")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25")),
    "claude-opus-4-7": (Decimal("5"), Decimal("25")),
    "claude-opus-4-6": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-sonnet-4-6": (Decimal("3"), Decimal("15")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}
_LONG = {"claude-haiku-5-5": (100_000, (Decimal("0.50"), Decimal("2.50")))}
FAILURE_STREAK_LIMIT = 3
MICRO = Decimal(1_000_000)


def _lock_file(fh, timeout: float = 60.0) -> None:
    """Exclusive advisory lock on an open file, across threads, guards and processes. Fails closed after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise RuntimeError("LEDGER LOCK: another process holds the spend ledger; no call is made") from None
            time.sleep(0.01)


def _unlock_file(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class SpendCapReached(Exception):
    """The next model call would exceed the cap (or the run is halted). Nothing was sent."""


class UnknownModelPrice(RuntimeError):
    """The model id has no entry in the price table: the guard will not guess a price."""


def price_for(model: str, input_tokens: int = 0) -> tuple[Decimal, Decimal]:
    if model not in PRICES:
        raise UnknownModelPrice(f"no price for model {model!r} in the table ({PRICE_SOURCE}); add it to app/spend_guard.PRICES "
                                f"after reading the pricing page, or choose a model that is listed")
    if model in _LONG and input_tokens > _LONG[model][0]:
        return _LONG[model][1]
    return PRICES[model]


def estimate_input_tokens(system: str | None, user: str) -> int:
    """No tokenizer is available offline, so this is an upper bound: a token is at least one byte, so the UTF-8 byte count cannot be
    exceeded by the token count (English is nearer one token per four bytes; this over-reserves by that factor, which at the listed
    input prices is a fraction of a cent per call), plus a fixed allowance for message framing."""
    return len((system or "").encode("utf-8")) + len(user.encode("utf-8")) + 64


def money(x: Decimal) -> str:
    return f"${x:.4f}"


@dataclass(frozen=True)
class Reservation:
    cost: Decimal
    input_tokens: int
    max_tokens: int


class SpendGuard:
    def __init__(self, model: str, cap_usd, ledger: str | None = None, on_halt=None):
        self.model = model
        price_for(model)                                   # refuses an unpriced model before anything else
        try:
            self.cap = Decimal(str(cap_usd))
        except Exception:
            raise ValueError("the spend cap must be a number") from None
        if not self.cap.is_finite() or self.cap <= 0:
            raise ValueError("the spend cap must be a finite number more than zero")
        self.ledger, self.on_halt = ledger, on_halt
        self._lock = threading.RLock()
        self.committed = Decimal(0)
        self.inflight = Decimal(0)
        self.calls = 0
        self.by_set: dict = {}
        self.label = ""
        self.halted: str | None = None
        self._streak = 0
        self._load()

    # ---- one budget per ledger -----------------------------------------------------------------------------
    @contextmanager
    def _txn(self):
        """Exclusive section for one read-check-write on the shared ledger. Without a ledger it is only the in-process lock."""
        with self._lock:
            if not self.ledger:
                yield
                return
            fh = open(self.ledger + ".lock", "a+b")
            try:
                _lock_file(fh)
                try:
                    self._refresh()
                    yield
                finally:
                    _unlock_file(fh)
            finally:
                fh.close()

    def _refresh(self) -> None:
        """Take the file's numbers as they are NOW (another guard may have spent or reserved since this one last looked)."""
        if not self.ledger or not os.path.exists(self.ledger):
            return
        try:
            with open(self.ledger, encoding="utf-8") as f:
                d = json.load(f)
            self.committed = Decimal(d["committed_usd"])
            self.inflight = Decimal(d.get("inflight_usd", "0"))
            self.calls = int(d["calls"])
            self.by_set = {k: {"calls": int(v["calls"]), "usd": Decimal(v["usd"])} for k, v in d.get("by_set", {}).items()}
        except Exception as e:
            raise ValueError(f"LEDGER {self.ledger} cannot be read ({type(e).__name__}); fix or remove it by hand: "
                             f"the spend is not reset silently") from None

    # ---- state -------------------------------------------------------------------------------------------
    @property
    def spent(self) -> Decimal:
        """Money already spent plus money reserved for calls in flight: what the cap is measured against."""
        return self.committed + self.inflight

    @property
    def remaining(self) -> Decimal:
        return self.cap - self.spent

    def can_afford(self, amount: Decimal) -> bool:
        return not self.halted and self.spent + Decimal(amount) <= self.cap

    def set_label(self, label: str) -> None:
        self.label = label

    def estimate(self, system: str | None, user: str, max_tokens: int) -> Decimal:
        n = estimate_input_tokens(system, user)
        pin, pout = price_for(self.model, n)
        return (n * pin + max_tokens * pout) / MICRO

    def halt(self, reason: str) -> None:
        with self._lock:
            if self.halted:
                return
            self.halted = reason
        if self.on_halt:
            try:
                self.on_halt(reason)
            except Exception:                              # a notice must never break the guard
                pass

    # ---- reserve / settle / fail -------------------------------------------------------------------------
    def reserve(self, system: str | None, user: str, max_tokens: int) -> Reservation:
        with self._txn():
            if self.halted:
                raise SpendCapReached("HALTED: " + self.halted)
            n = estimate_input_tokens(system, user)
            pin, pout = price_for(self.model, n)
            cost = (n * pin + max_tokens * pout) / MICRO
            if self.spent + cost > self.cap:
                reason = (f"CAP: the next call could cost {money(cost)} and {money(self.spent)} of the {money(self.cap)} cap "
                          f"is already spent or reserved")
                self.halted = reason
                halted_now = reason
            else:
                self.inflight += cost
                self._save()
                return Reservation(cost, n, max_tokens)
        if self.on_halt:
            try:
                self.on_halt(halted_now)
            except Exception:
                pass
        raise SpendCapReached(halted_now)

    def settle(self, res: Reservation, usage: dict | None) -> Decimal:
        overrun = None
        with self._txn():
            cost = res.cost
            if isinstance(usage, dict):
                i, o = usage.get("input_tokens"), usage.get("output_tokens")
                if isinstance(i, int) and isinstance(o, int) and i >= 0 and o >= 0:
                    pin, pout = price_for(self.model, i)
                    cost = (i * pin + o * pout) / MICRO
            self.inflight = max(Decimal(0), self.inflight - res.cost)
            self.committed += cost
            self.calls += 1
            row = self.by_set.setdefault(self.label or "-", {"calls": 0, "usd": Decimal(0)})
            row["calls"] += 1
            row["usd"] += cost
            self._streak = 0
            if cost > res.cost:
                overrun = (f"OVERRUN: a call cost {money(cost)} but {money(res.cost)} was reserved for it; the estimate cannot be "
                           f"trusted, so no further call is made")
            self._save()
        if overrun:
            self.halt(overrun)
        return cost

    def fail(self, res: Reservation, billed: bool, status: int | None = None) -> None:
        """The call did not give a usable reply. billed=True: it may have been billed, so the full reservation is charged."""
        reason = None
        with self._txn():
            self.inflight = max(Decimal(0), self.inflight - res.cost)
            if billed:
                self.committed += res.cost
                self.calls += 1
                row = self.by_set.setdefault(self.label or "-", {"calls": 0, "usd": Decimal(0)})
                row["calls"] += 1
                row["usd"] += res.cost
            self._streak += 1
            if status in (401, 403):
                reason = f"KEY: the provider rejected the key (HTTP {status}); no further call is made"
            elif self._streak >= FAILURE_STREAK_LIMIT:
                reason = f"FAILURES: {self._streak} model calls failed in a row; no further call is made"
            self._save()
        if reason:
            self.halt(reason)

    # ---- the ledger --------------------------------------------------------------------------------------
    def _load(self) -> None:
        if not self.ledger or not os.path.exists(self.ledger):
            return
        try:
            with open(self.ledger, encoding="utf-8") as f:
                d = json.load(f)
            self.committed = Decimal(d["committed_usd"]) + Decimal(d.get("inflight_usd", "0"))   # a call that never settled counts
            self.calls = int(d["calls"])
            self.by_set = {k: {"calls": int(v["calls"]), "usd": Decimal(v["usd"])} for k, v in d.get("by_set", {}).items()}
        except Exception as e:
            raise ValueError(f"LEDGER {self.ledger} cannot be read ({type(e).__name__}); fix or remove it by hand: "
                             f"the spend is not reset silently") from None

    def _save(self) -> None:
        if not self.ledger:
            return
        data = {"version": 1, "model": self.model, "price_source": PRICE_SOURCE,
                "committed_usd": str(self.committed), "inflight_usd": str(self.inflight), "calls": self.calls,
                "by_set": {k: {"calls": v["calls"], "usd": str(v["usd"])} for k, v in self.by_set.items()}}
        tmp = f"{self.ledger}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.ledger)

    def summary(self) -> dict:
        return {"model": self.model, "cap_usd": float(self.cap), "spent_usd": float(self.committed), "calls": self.calls,
                "price_per_mtok_usd": [float(x) for x in price_for(self.model)], "price_source": PRICE_SOURCE,
                "ledger": self.ledger, "halted": self.halted,
                "by_set": {k: {"calls": v["calls"], "usd": float(v["usd"])} for k, v in self.by_set.items()}}
