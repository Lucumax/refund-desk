from __future__ import annotations
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass

FOUND, ABSENT, INDETERMINATE = "FOUND", "ABSENT", "INDETERMINATE"


@dataclass(frozen=True)
class LookupResult:
    """Tri-state answer to "did the provider accept this idempotency key?".
    FOUND: yes, payment_ref set. ABSENT: the provider DEFINITIVELY has no record (safe to
    re-dispatch with the same key). INDETERMINATE: no definitive answer (never re-dispatch)."""
    status: str
    payment_ref: str | None = None

    @classmethod
    def found(cls, ref: str) -> "LookupResult":
        return cls(FOUND, ref)

    @classmethod
    def absent(cls) -> "LookupResult":
        return cls(ABSENT)

    @classmethod
    def indeterminate(cls) -> "LookupResult":
        return cls(INDETERMINATE)


def normalize_lookup(raw, none_is_absent: bool = False) -> LookupResult:
    """Backward-compatible shim. A legacy adapter returns a ref string (FOUND) or None.
    None is NOT proof of absence, so it maps to INDETERMINATE unless the adapter sets
    `lookup_none_is_absent = True`. Anything unrecognised is INDETERMINATE."""
    if isinstance(raw, LookupResult):
        if raw.status == FOUND and not raw.payment_ref:
            return LookupResult.indeterminate()
        return raw if raw.status in (FOUND, ABSENT) else LookupResult.indeterminate()
    if isinstance(raw, str) and raw:
        return LookupResult.found(raw)
    if raw is None and none_is_absent:
        return LookupResult.absent()
    return LookupResult.indeterminate()


def is_definitive_failure(exc: BaseException) -> bool:
    """Only PaymentRejected, or an exception the adapter marks `definitive_failure = True`,
    proves the provider did not accept the payment. Everything else leaves the outcome UNKNOWN."""
    return isinstance(exc, PaymentRejected) or getattr(exc, "definitive_failure", False) is True


class PaymentTimeout(Exception):
    """Outcome unknown: the provider may or may not have accepted the payment."""


class PaymentRejected(Exception):
    """Definite failure: the provider did not accept the payment."""


class SimulatedCrash(BaseException):
    """Test only: process dies. BaseException so the engine does not catch it."""


class PaymentAdapter(ABC):
    @abstractmethod
    def pay(self, payee: str, amount_minor: int, currency: str, idempotency_key: str) -> str:
        """Execute a payment and return a payment_ref. The provider must dedupe on the key."""

    @abstractmethod
    def lookup(self, idempotency_key: str) -> "LookupResult | str | None":
        """Return LookupResult (FOUND / ABSENT definitive / INDETERMINATE). Legacy adapters may
        return the payment_ref string (FOUND) or None (treated as INDETERMINATE, see
        normalize_lookup). Raising also means INDETERMINATE."""


class MockAdapter(PaymentAdapter):
    """Deterministic sandbox. `calls` counts pay() calls. One-shot fault switches for tests."""

    def __init__(self) -> None:
        self.calls = 0
        self.lookups = 0
        self.accepted: dict[str, str] = {}
        self.reject_next = False
        self.timeout_before_accept = False
        self.timeout_after_accept = False
        self.crash_after_accept = False

    def pay(self, payee, amount_minor, currency, idempotency_key):
        self.calls += 1
        if self.reject_next:
            self.reject_next = False
            raise PaymentRejected("rejected")
        if self.timeout_before_accept:
            self.timeout_before_accept = False
            raise PaymentTimeout("timeout")
        ref = self.accepted.setdefault(
            idempotency_key, "mock-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:12])
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise PaymentTimeout("timeout")
        if self.crash_after_accept:
            self.crash_after_accept = False
            raise SimulatedCrash()
        return ref

    def lookup(self, idempotency_key):
        self.lookups += 1
        ref = self.accepted.get(idempotency_key)
        return LookupResult.found(ref) if ref else LookupResult.absent()
