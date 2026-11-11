# CORE_CHANGES

Version 2.0.0 (was unversioned). Hardened by the author in an earlier prototype before this entry; this repository copies it unmodified. Python 3.11 stdlib only. The 35 original tests are unchanged and passing.

## What changed and why

1. Reconcile and retry. `adapter.lookup` is now tri-state: `LookupResult` with status FOUND, ABSENT (definitive) or INDETERMINATE. `retry()` re-dispatches only on definitive ABSENT, with the same idempotency key. A lookup that raises, or returns anything else, leaves the record UNKNOWN and never dispatches. A generic exception from `pay()` becomes UNKNOWN (the money may have moved); only `PaymentRejected` or an exception with `definitive_failure = True` becomes FAILED. Backward-compatible shim `normalize_lookup`: a returned string means FOUND; a returned None means INDETERMINATE (not absent) unless the adapter sets `lookup_none_is_absent = True`.
2. Crash recovery. `Engine.recover()` runs the integrity guard, then drives AUTHORIZED records that never reached SUBMITTING through the same execution path (policy, terms and validity rechecked, SUBMITTING persisted before dispatch, so a second recover cannot dispatch again). SUBMITTING and UNKNOWN records are only reconciled, never blindly re-dispatched. Returns the new receipts.
3. Integrity gate. The ledger verifies chain and signatures on every read and before every append (`LedgerIntegrityError`, code `LEDGER_INTEGRITY_FAILED`, `reason_code` carries HASH_MISMATCH, SIGNATURE_INVALID, TRUNCATED, CHECKPOINT_MISSING, CHECKPOINT_INVALID, CHECKPOINT_ROLLBACK and so on). Every Engine operation verifies chain plus trusted checkpoint before any side effect and fails closed. The checkpoint is saved atomically after every append, at `checkpoint_path` (default beside the ledger; set it to another disk or service for real protection). Saving an older checkpoint is refused, and a stored checkpoint more than one record behind the chain is reported as CHECKPOINT_ROLLBACK. The public fallback key is gone: no key in the argument or `APPROVAL_RECEIPT_HMAC_KEY` raises, unless `allow_demo_key=True`, which logs a warning and stamps `key_mode: DEMO` on every record.
4. Cross-process safety. `filelock.FileLock` (msvcrt on Windows, fcntl elsewhere, re-entrant, timeout raises `LEDGER_LOCK_TIMEOUT`) is held by every Engine operation for its whole duration (duplicate, limit and reservation checks, id allocation, dispatch) and around every append. Two Engines or two processes sharing a ledger cannot double-spend or corrupt the chain. Dispatch happens under the lock, so a slow provider call blocks other writers (bounded by the lock timeout of 60 s).
5. Expiry. AUTHORIZED receipts carry `execute_by` (now plus `execution_ttl`, default 15 minutes, capped at mandate expiry). Before every dispatch (first dispatch, recover, retry) the engine rechecks policy hard blocks, the approved terms digest and `execute_by`; failures write BLOCKED or EXPIRED and nothing is sent.
6. Other bugs found. An intent id reused under a different idempotency key silently mixed two payments in `latest_for_intent`: now `IdempotencyConflict`. A timezone-naive `Mandate.expires_at` crashed policy with TypeError: now ValueError at construction. Two mandates for one agent silently overwrote each other: now ValueError. The CLI demo kept a stale checkpoint on rerun: now removed with the ledger.

New: `__version__ = "2.0.0"`, `VERSION`, `VENDOR_MANIFEST.json`, `python -m approval_receipt manifest`.

## Breaking changes and migration for vendored copies

- Public fallback key removed. Set `APPROVAL_RECEIPT_HMAC_KEY` (or pass `key=`), or pass `allow_demo_key=True` for demos only. Remove any code that relied on `cli.DEMO_KEY`; it now lives at `approval_receipt.DEMO_KEY`.
- Legacy adapters whose `lookup` returns None now stay UNKNOWN on retry. Return `LookupResult.absent()` when the provider definitively has no record, or set `lookup_none_is_absent = True` on the adapter if None really is definitive. Return `LookupResult.indeterminate()` for "cannot tell".
- Generic `pay()` exceptions are UNKNOWN, not FAILED. Raise `PaymentRejected` (or set `definitive_failure = True`) only when the provider definitively refused.
- Ledger reads raise `LedgerIntegrityError` (also a RuntimeError and an ApprovalError) on any tampering instead of returning data. `ledger.verify()` is unchanged and still returns a result without raising. `ApprovalError` moved to `approval_receipt.errors` (still importable from the package and from `engine`).
- Existing ledger files from 1.x have no checkpoint, so Engine operations fail with CHECKPOINT_MISSING. After checking the chain yourself (`ledger.verify().ok`, plus any checkpoint you saved earlier via `ledger.verify(cp)`), call `ledger.save_checkpoint()` once to adopt the current head. Old records lack `execute_by`; they dispatch with no validity limit beyond the mandate.
- Apps that deleted or recreated the ledger on startup must also delete the checkpoint, or the ledger constructor raises TRUNCATED.
- New receipt field `execute_by` (and `key_mode` in demo mode). Receipt hashes of new records differ from 1.x.
- Every operation now holds a lock file `LEDGER.lock` beside the ledger (git-ignore it).
- Call `engine.recover()` at startup, then reconcile or retry UNKNOWN records on a schedule.

## Verifying a vendored copy

Copy the `approval_receipt/` directory and `VENDOR_MANIFEST.json` (and `VERSION`) into the downstream repo, keeping them side by side in one folder, then run from any directory:

    python -m approval_receipt manifest --check --root <folder containing approval_receipt/ and VENDOR_MANIFEST.json>

It prints `manifest_ok` and lists MODIFIED, MISSING or UNEXPECTED files, and exits 1 on any problem. Hashes are SHA-256 with CRLF normalised to LF. Programmatic: `approval_receipt.manifest.verify_manifest(root)`. Core maintainers regenerate with `--write` after every package change (a test fails if the manifest is stale).

## Not fixed (out of scope or not possible in the core)

- App-level findings (ownership checks, duplicate PO detection, auth, startup deleting history, immutable dispatch digest, refresh of pending provider status).
- The checkpoint is only as trustworthy as where you store it: an attacker who can restore both the ledger and a consistent older checkpoint on the same disk is undetectable. Store the checkpoint elsewhere.
- The lock is advisory, local-filesystem only, and not meant for network filesystems or multiple hosts.
- Idempotency-key retention at real providers (finite window) is adapter policy; the core only offers the ABSENT/INDETERMINATE vocabulary.
