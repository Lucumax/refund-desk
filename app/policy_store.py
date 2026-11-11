"""The owner's refund policy as a chain of signed versions. ONE source of truth: the refund service
(inbox and arena alike) reads the active version from here for every decision.

What the five fields mean (the whole contract, repeated in README.md and on screen):

  per_order_cap_minor     AUTOMATIC allowance per order. The most that automatic refunds may add up to on
                          one order (each refund is also at most this). Not a per-message limit: five
                          small automatic refunds on one order still stop at this amount.
  escalate_ceiling_minor  The most the owner may approve in ONE refund. Above it the request is denied.
  daily_budget_minor      The most that AUTOMATIC refunds may add up to in one UTC day. Reservations that
                          are still PENDING at PayPal or UNKNOWN count against it.
  evidence_required       The buyer's own words must support the reason.
  eligible_reasons        Reasons that may be refunded automatically (others go to the owner).

Refunds the owner approves by hand do NOT use up the daily automatic budget. They count against the
order's remaining balance and the ceiling, and each one is the owner's decision.

Each saved version is a signed receipt. A new version changes the terms an open approval was bound to,
so an approval card shown under an older version can no longer be approved."""
from __future__ import annotations
import os
from datetime import datetime
from typing import Callable

from approval_receipt import Ledger, LedgerIntegrityError

from . import arena_policy as ap
from .trusted import trusted_receipts


class PolicyStore:
    def __init__(self, ledger: Ledger, clock: Callable[[], datetime]):
        self.ledger, self.clock = ledger, clock
        self._high = 0           # the highest policy version this store has ever read: the version can only go up
        if not any(r["event"] == "policy_version" for r in self.ledger.receipts()):
            self.ledger.append(self._body(1, ap.DEFAULT, "", [], None))

    def _body(self, version: int, policy: ap.Policy, source: str, changes: list, who) -> dict:
        return {"event": "policy_version", "at": self.clock().isoformat(), "version": version,
                "policy": policy.as_dict(), "policy_hash": policy.hash(), "source_text": source,
                "changes": changes, "saved_by": who}

    def _versions(self) -> list[dict]:
        """The policy versions, read ONLY after the chain AND its trusted checkpoint verify (`Ledger.records()` alone
        does not look at the checkpoint, so a truncated chain would read as an older, valid policy). Every call is a
        fresh trusted read, so a running service is protected, not just a starting one. The version must also be
        strictly sequential and can never be lower than one this store already read: a chain that was rolled back
        together with its checkpoint is refused while the service runs (POLICY_ROLLBACK)."""
        versions = [r for r in trusted_receipts(self.ledger) if r["event"] == "policy_version"]
        if any(v["version"] != i + 1 for i, v in enumerate(versions)):
            raise LedgerIntegrityError("POLICY_VERSION_SEQUENCE", versions[0]["id"] if versions else None)
        top = versions[-1]["version"] if versions else 0
        if top < self._high:
            raise LedgerIntegrityError("POLICY_ROLLBACK", versions[-1]["id"] if versions else None)
        self._high = top
        return versions

    def active(self) -> tuple[ap.Policy, dict]:
        rec = self._versions()[-1]
        return ap.Policy.from_dict(rec["policy"]), rec

    def state(self) -> dict:
        versions = self._versions()
        policy = ap.Policy.from_dict(versions[-1]["policy"])
        rec = versions[-1]
        listing = [{"version": p["version"], "hash": p["policy_hash"], "at": p["at"], "source_text": p["source_text"],
                    "receipt_id": p["id"], "saved_by": p.get("saved_by")} for p in versions]
        return {"active": {**policy.as_dict(), "version": rec["version"], "hash": rec["policy_hash"]},
                "versions": listing, "examples": list(ap.HELP_EXAMPLES),
                "ranges": {"cap": ap.CAP_RANGE, "ceiling": ap.CEILING_RANGE, "daily": ap.DAILY_RANGE},
                "max_text": ap.MAX_TEXT}

    def preview(self, text: str) -> dict:
        policy, rec = self.active()
        res = ap.compile_text(text, policy)
        out = res.as_dict()
        out["base_version"] = rec["version"]
        out["base_hash"] = rec["policy_hash"]
        out["result_hash"] = res.policy.hash() if res.ok else None
        out["no_change"] = res.ok and not any(c["changed"] for c in res.changes)
        return out

    def save(self, text: str, who: str, expected_base_version: int | None = None,
             expected_result_hash: str | None = None) -> dict:
        """Compile again and save as the next signed version. BOTH `expected_base_version` (the version the owner
        previewed against) and `expected_result_hash` (the result the owner saw) are REQUIRED: a save without them is
        refused, so what is saved is exactly what was previewed whoever calls this. The whole compare-and-append runs
        under the policy chain's cross-process lock, so two saves from the same preview cannot both become the next
        version: the second finds the version moved and is refused (POLICY_CHANGED)."""
        if (type(expected_base_version) is not int or not isinstance(expected_result_hash, str)
                or len(expected_result_hash) != 64):
            return {"ok": False, "errors": [{"clause": "", "code": "PREVIEW_REQUIRED"}], "recognized": []}
        with self.ledger.lock:
            policy, rec = self.active()
            res = ap.compile_text(text, policy)
            if expected_base_version != rec["version"]:
                return {"ok": False, "errors": [{"clause": "", "code": "POLICY_CHANGED"}], "recognized": res.recognized}
            if not res.ok:
                return {"ok": False, "errors": res.errors, "recognized": res.recognized}
            if expected_result_hash != res.policy.hash():
                return {"ok": False, "errors": [{"clause": "", "code": "PREVIEW_MISMATCH"}], "recognized": res.recognized}
            if not any(c["changed"] for c in res.changes):
                return {"ok": False, "errors": [{"clause": "", "code": "NO_CHANGE"}], "recognized": res.recognized}
            if any(v["version"] == rec["version"] + 1 for v in self._versions()):     # cannot happen under the lock; belt and braces
                return {"ok": False, "errors": [{"clause": "", "code": "POLICY_CHANGED"}], "recognized": res.recognized}
            new = self.ledger.append(self._body(rec["version"] + 1, res.policy, text.strip(), res.changes, who))
            return {"ok": True, "version": new["version"], "hash": new["policy_hash"], "receipt_id": new["id"],
                    "changes": res.changes, "policy": res.policy.as_dict()}

    def diff_versions(self, a: int, b: int) -> dict | None:
        by = {p["version"]: p for p in self._versions()}
        if a not in by or b not in by:
            return None
        pa, pb = ap.Policy.from_dict(by[a]["policy"]), ap.Policy.from_dict(by[b]["policy"])
        return {"from": a, "to": b, "changes": ap.diff(pa, pb), "from_hash": by[a]["policy_hash"],
                "to_hash": by[b]["policy_hash"]}


def open_ledger(path: str, checkpoint_path: str, key, allow_demo_key: bool) -> tuple[Ledger, bool]:
    """Open a signed ledger. Returns (ledger, existed). The caller verifies an existing chain
    against its checkpoint before writing anything (see Service)."""
    existed = os.path.exists(path) and os.path.getsize(path) > 0
    return Ledger(path, key, checkpoint_path=checkpoint_path, allow_demo_key=allow_demo_key), existed
