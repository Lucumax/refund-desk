"""Error types shared by the engine and the ledger."""
from __future__ import annotations


class ApprovalError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LedgerIntegrityError(ApprovalError, RuntimeError):
    """The ledger chain, a signature or the trusted checkpoint failed verification.
    `code` is always LEDGER_INTEGRITY_FAILED; `reason_code` says why (HASH_MISMATCH,
    SIGNATURE_INVALID, TRUNCATED, CHECKPOINT_ROLLBACK, ...)."""
    def __init__(self, reason_code: str, first_broken_id: str | None = None):
        super().__init__("LEDGER_INTEGRITY_FAILED")
        self.reason_code, self.first_broken_id = reason_code, first_broken_id

    def __str__(self) -> str:
        return f"LEDGER_INTEGRITY_FAILED ({self.reason_code}, at {self.first_broken_id})"
