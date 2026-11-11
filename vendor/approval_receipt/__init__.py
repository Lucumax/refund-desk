"""Approval Receipt core: mandate, policy, approvals, signed receipt chain."""
__version__ = "2.0.0"

from .models import Mandate, Intent, Decision, ApproverSession, ALLOW, ESCALATE, DENY
from .policy import check
from .adapters import (PaymentAdapter, MockAdapter, PaymentTimeout, PaymentRejected, SimulatedCrash,
                       LookupResult, normalize_lookup, is_definitive_failure,
                       FOUND, ABSENT, INDETERMINATE)
from .errors import ApprovalError, LedgerIntegrityError
from .receipts import Ledger, VerifyResult, DEMO_KEY
from .engine import Engine, Result, IdempotencyConflict

__all__ = ["Mandate", "Intent", "Decision", "ApproverSession", "ALLOW", "ESCALATE", "DENY",
           "check", "PaymentAdapter", "MockAdapter", "PaymentTimeout", "PaymentRejected",
           "SimulatedCrash", "LookupResult", "normalize_lookup", "is_definitive_failure",
           "FOUND", "ABSENT", "INDETERMINATE", "Ledger", "VerifyResult", "DEMO_KEY", "Engine",
           "Result", "ApprovalError", "LedgerIntegrityError", "IdempotencyConflict",
           "__version__"]
