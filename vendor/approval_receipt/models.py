from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime

ALLOW, ESCALATE, DENY = "ALLOW", "ESCALATE", "DENY"


@dataclass(frozen=True)
class Mandate:
    """All money is integer minor units (cents) in one explicit currency."""
    id: str
    agent_id: str
    per_payment_limit: int
    daily_limit: int
    payee_allowlist: tuple[str, ...]
    purpose_codes: tuple[str, ...]
    expires_at: datetime
    currency: str = "USD"
    version: int = 1

    def __post_init__(self):
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("Mandate.expires_at must be timezone aware")


@dataclass(frozen=True)
class Intent:
    id: str
    agent_id: str
    payee: str
    amount_minor: int
    currency: str
    purpose_code: str
    reason_text: str  # untrusted; never read by the policy checker
    idempotency_key: str
    created_at: datetime  # agent-supplied, informational only; the engine clock rules

    def __post_init__(self):
        if type(self.amount_minor) is not int:
            raise TypeError("amount_minor must be an int (minor units)")

    def snapshot(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d

    def payload(self) -> dict:
        """What an idempotency key is bound to (everything except the id and timestamp)."""
        d = self.snapshot()
        d.pop("created_at"), d.pop("id")
        return d

    @classmethod
    def from_snapshot(cls, d: dict) -> "Intent":
        return cls(**{**d, "created_at": datetime.fromisoformat(d["created_at"])})


@dataclass(frozen=True)
class Decision:
    decision: str      # ALLOW | ESCALATE | DENY
    rule_id: str       # e.g. "R7"
    reason_code: str   # machine code, e.g. "AMOUNT_OVER_PER_PAYMENT_LIMIT"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ApproverSession:
    """Issued by an authentication layer (not part of this core). The engine takes the
    approver identity only from here, never from agent-supplied text."""
    approver_id: str
    session_id: str
