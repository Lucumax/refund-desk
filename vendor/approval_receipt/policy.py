"""Policy checker. Pure function. It never sees reason_text."""
from __future__ import annotations
from datetime import datetime
from .models import Mandate, Intent, Decision, ALLOW, ESCALATE, DENY


def check(mandate: Mandate | None, intent: Intent, reserved_today: int, now: datetime) -> Decision:
    """Rules run in order; the first hit wins. Hard blocks (DENY) come before ESCALATE."""
    if mandate is None or mandate.agent_id != intent.agent_id:
        return Decision(DENY, "R1", "MANDATE_NOT_FOUND")
    if now >= mandate.expires_at:
        return Decision(DENY, "R2", "MANDATE_EXPIRED")
    if intent.amount_minor <= 0:
        return Decision(DENY, "R3", "AMOUNT_NOT_POSITIVE")
    if intent.currency != mandate.currency:
        return Decision(DENY, "R4", "CURRENCY_MISMATCH")
    if intent.payee not in mandate.payee_allowlist:
        return Decision(DENY, "R5", "PAYEE_NOT_ALLOWLISTED")
    if intent.purpose_code not in mandate.purpose_codes:
        return Decision(DENY, "R6", "PURPOSE_NOT_PERMITTED")
    if intent.amount_minor > mandate.per_payment_limit:
        return Decision(ESCALATE, "R7", "AMOUNT_OVER_PER_PAYMENT_LIMIT")
    if reserved_today + intent.amount_minor > mandate.daily_limit:
        return Decision(ESCALATE, "R8", "DAILY_LIMIT_EXCEEDED")
    return Decision(ALLOW, "R9", "WITHIN_MANDATE")
