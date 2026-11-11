"""Append-only, hash-chained, HMAC-signed receipts stored as JSON lines.

Format: each line is canonical JSON (sorted keys, no spaces, ASCII). A record has
seq (0 is the genesis record), id, key_id, prev_hash, hash = SHA-256 of the canonical
body (everything except hash and signature), signature = HMAC-SHA256(key, hash).
A signed checkpoint (key_id, seq, head_hash, signature) is saved after every append, at a
configurable path (put it on a different medium than the ledger) and detects truncation
and checkpoint rollback. Reads and appends verify the chain; appends also verify the
checkpoint. All writers share a cross-process file lock.
"""
from __future__ import annotations
import hashlib, hmac, json, logging, os, tempfile
from dataclasses import dataclass
from .errors import LedgerIntegrityError
from .filelock import FileLock

log = logging.getLogger("approval_receipt")

GENESIS_HASH = "0" * 64
KEY_ENV = "APPROVAL_RECEIPT_HMAC_KEY"
# Public, fixed, worthless as a secret. Used ONLY with an explicit allow_demo_key=True.
DEMO_KEY = "demo-only-key-not-a-secret"


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def rid(seq: int) -> str:
    return f"R-{seq:06d}"


def load_key(key: bytes | str | None = None, allow_demo_key: bool = False) -> bytes:
    """Key comes from the argument (tests) or the environment. Never stored or printed.
    There is no silent fallback: without a key this raises unless allow_demo_key is set."""
    if key is None:
        key = os.environ.get(KEY_ENV)
    if not key and allow_demo_key:
        key = DEMO_KEY
    if not key:
        raise RuntimeError(f"signing key missing: set {KEY_ENV}")
    return key.encode() if isinstance(key, str) else key


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records: int
    first_broken_id: str | None = None
    reason_code: str | None = None
    checkpoint_checked: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class Ledger:
    def __init__(self, path: str, key: bytes | str | None = None, key_id: str = "k1",
                 checkpoint_path: str | None = None, allow_demo_key: bool = False,
                 lock_timeout: float = 60.0) -> None:
        self.path, self.key_id = path, key_id
        self.checkpoint_path = checkpoint_path or path + ".checkpoint.json"
        self._key = load_key(key, allow_demo_key)
        self.demo_key = self._key == DEMO_KEY.encode()
        if self.demo_key:
            log.warning("APPROVAL RECEIPT DEMO KEY IN USE: receipts are signed with a PUBLIC key "
                        "and prove nothing. Every record is marked key_mode=DEMO.")
        self.lock = FileLock(path + ".lock", timeout=lock_timeout)
        with self.lock:
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                cp = self._load_checkpoint()
                if cp is not None and cp["seq"] > 0:
                    raise LedgerIntegrityError("TRUNCATED", rid(cp["seq"]))  # emptied ledger
                self._write_record({"event": "genesis"}, [])

    def mac(self, data: str) -> str:
        return hmac.new(self._key, data.encode(), hashlib.sha256).hexdigest()

    # ---- reading (always verified) --------------------------------------
    def _read(self) -> list:
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append(None)  # malformed line, reported by verify()
        return out

    def records(self) -> list[dict]:
        """All records, after verifying chain and signatures. Raises LedgerIntegrityError."""
        recs = self._read()
        res = self._verify_recs(recs, None)
        if not res.ok:
            raise LedgerIntegrityError(res.reason_code, res.first_broken_id)
        return recs

    def receipts(self) -> list[dict]:
        return [r for r in self.records() if r["event"] != "genesis"]

    # ---- appending ------------------------------------------------------
    def append(self, body: dict) -> dict:
        """Cross-process safe. Verifies the chain AND the trusted checkpoint first."""
        with self.lock:
            recs = self._read()
            res = self._trust_check(recs)
            if not res.ok:
                raise LedgerIntegrityError(res.reason_code, res.first_broken_id)
            return self._write_record(body, recs)

    def _write_record(self, body: dict, recs: list) -> dict:
        rec = dict(body)
        rec["seq"] = len(recs)
        rec["id"] = rid(len(recs))
        rec["key_id"] = self.key_id
        if self.demo_key:
            rec["key_mode"] = "DEMO"
        rec["prev_hash"] = recs[-1]["hash"] if recs else GENESIS_HASH
        rec["hash"] = digest(rec)
        rec["signature"] = self.mac(rec["hash"])
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            f.write(canonical(rec).decode() + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._save_checkpoint(self._sign_checkpoint(rec))
        return rec

    def latest_for_intent(self, intent_id: str) -> dict | None:
        found = [r for r in self.receipts() if r["intent"]["id"] == intent_id]
        return found[-1] if found else None

    def latest_for_key(self, idem_key: str) -> dict | None:
        found = [r for r in self.receipts() if r["intent"]["idempotency_key"] == idem_key]
        return found[-1] if found else None

    # ---- checkpoints ------------------------------------------------------
    def _sign_checkpoint(self, head: dict) -> dict:
        cp = {"key_id": self.key_id, "seq": head["seq"], "head_hash": head["hash"]}
        return {**cp, "signature": self.mac(digest(cp))}

    def _cp_valid(self, cp) -> bool:
        if not isinstance(cp, dict) or type(cp.get("seq")) is not int:
            return False
        c = {k: cp.get(k) for k in ("key_id", "seq", "head_hash")}
        return hmac.compare_digest(str(cp.get("signature")), self.mac(digest(c)))

    def checkpoint(self) -> dict:
        """Signed head of the chain. Also saved automatically after every append."""
        return self._sign_checkpoint(self.records()[-1])

    def _load_checkpoint(self) -> dict | None:
        """The stored checkpoint, None if missing, or {"invalid": True, "seq": -1}."""
        try:
            with open(self.checkpoint_path, encoding="utf-8") as f:
                cp = json.load(f)
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            return {"seq": -1, "invalid": True}
        return cp if self._cp_valid(cp) else {"seq": -1, "invalid": True}

    def save_checkpoint(self, cp: dict | None = None) -> dict:
        """Persist a checkpoint atomically. Refuses to go backwards (CHECKPOINT_ROLLBACK).
        With no argument it adopts the current verified head (use once when migrating a
        ledger that has no checkpoint yet, after checking the chain by other means)."""
        with self.lock:
            cp = cp or self.checkpoint()
            if not self._cp_valid(cp):
                raise LedgerIntegrityError("CHECKPOINT_INVALID")
            self._save_checkpoint(cp)
            return cp

    def _save_checkpoint(self, cp: dict) -> None:
        old = self._load_checkpoint()
        if old is not None and not old.get("invalid"):
            if cp["seq"] < old["seq"] or (cp["seq"] == old["seq"] and cp["head_hash"] != old["head_hash"]):
                raise LedgerIntegrityError("CHECKPOINT_ROLLBACK", rid(old["seq"]))
        d = os.path.dirname(os.path.abspath(self.checkpoint_path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".cp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cp, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.checkpoint_path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # ---- verification -----------------------------------------------------
    def verify(self, checkpoint: dict | None = None) -> VerifyResult:
        """Recompute the chain; name the FIRST broken record. Detects edits, deletion,
        reordering, and (only with a checkpoint) truncation."""
        return self._verify_recs(self._read(), checkpoint)

    def verify_trusted(self) -> VerifyResult:
        """Chain plus the stored checkpoint. A missing or invalid checkpoint fails closed."""
        return self._trust_check(self._read())

    def _trust_check(self, recs: list) -> VerifyResult:
        n = len(recs)
        stored = self._load_checkpoint()
        if stored is None:
            res = self._verify_recs(recs, None)
            return res if not res.ok else VerifyResult(False, n, None, "CHECKPOINT_MISSING", True)
        if stored.get("invalid"):
            return VerifyResult(False, n, None, "CHECKPOINT_INVALID", True)
        res = self._verify_recs(recs, stored)
        if not res.ok:
            return res
        if n - 1 - stored["seq"] > 1:  # every append advances it, so it lags by at most 1
            return VerifyResult(False, n, rid(stored["seq"]), "CHECKPOINT_ROLLBACK", True)
        return res

    def _verify_recs(self, recs: list, checkpoint: dict | None) -> VerifyResult:
        n, prev = len(recs), GENESIS_HASH
        for i, rec in enumerate(recs):
            def bad(code):
                return VerifyResult(False, n, rid(i), code, checkpoint is not None)
            if not isinstance(rec, dict):
                return bad("MALFORMED_RECORD")
            if rec.get("seq") != i or rec.get("id") != rid(i):
                return bad("SEQUENCE_BROKEN")
            if i == 0 and rec.get("event") != "genesis":
                return bad("GENESIS_MISSING")
            body = {k: v for k, v in rec.items() if k not in ("hash", "signature")}
            if rec.get("hash") != digest(body):
                return bad("HASH_MISMATCH")
            if rec.get("prev_hash") != prev:
                return bad("PREV_HASH_MISMATCH")
            if rec.get("key_id") != self.key_id:
                return bad("KEY_ID_UNKNOWN")
            if not hmac.compare_digest(str(rec.get("signature")), self.mac(rec["hash"])):
                return bad("SIGNATURE_INVALID")
            prev = rec["hash"]
        if n == 0:
            return VerifyResult(False, 0, rid(0), "GENESIS_MISSING", checkpoint is not None)
        if checkpoint is None:
            return VerifyResult(True, n)
        cp = {k: checkpoint.get(k) for k in ("key_id", "seq", "head_hash")}
        if not hmac.compare_digest(str(checkpoint.get("signature")), self.mac(digest(cp))):
            return VerifyResult(False, n, None, "CHECKPOINT_INVALID", True)
        if cp["seq"] >= n:
            return VerifyResult(False, n, rid(n), "TRUNCATED", True)
        if recs[cp["seq"]]["hash"] != cp["head_hash"]:
            return VerifyResult(False, n, rid(cp["seq"]), "CHECKPOINT_HEAD_MISMATCH", True)
        return VerifyResult(True, n, checkpoint_checked=True)
