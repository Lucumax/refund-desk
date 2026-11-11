"""Trusted reads of a signed chain.

`Ledger.receipts()` and `Ledger.records()` check the chain and its signatures but NOT the separately stored
checkpoint, so a chain whose newest records were cut off still reads as a valid, shorter history. Anything that
decides or shows money, policy or a verdict must read through here instead: the chain and the checkpoint are verified
together, under the ledger lock, in one pass, and a failure raises `LedgerIntegrityError` (fail closed)."""
from __future__ import annotations

from approval_receipt import Ledger, LedgerIntegrityError


def trusted_receipts(ledger: Ledger) -> list[dict]:
    """Every non-genesis record, after the chain, the signatures and the trusted checkpoint all verify."""
    with ledger.lock:
        recs = ledger._read()
        res = ledger._trust_check(recs)
        if not res.ok:
            raise LedgerIntegrityError(res.reason_code, res.first_broken_id)
    return [r for r in recs if r["event"] != "genesis"]


class TrustedLedger(Ledger):
    """A `Ledger` whose every read (`records`, `receipts`, `latest_for_intent`, `latest_for_key`, `checkpoint`) is a
    trusted read: chain, signatures and the stored checkpoint verified together. The vendored core is not modified;
    this only closes the gap that its plain read skips the checkpoint. Used for the three chains the service opens."""

    def records(self) -> list[dict]:
        with self.lock:
            recs = self._read()
            res = self._trust_check(recs)
            if not res.ok:
                raise LedgerIntegrityError(res.reason_code, res.first_broken_id)
        return recs
