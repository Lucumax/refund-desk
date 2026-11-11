"""Cross-process, re-entrant, thread-safe file lock. Stdlib only: msvcrt on Windows, fcntl
elsewhere. The one place that knows about the platform."""
from __future__ import annotations
import os, threading, time
from .errors import ApprovalError

try:  # Windows
    import msvcrt

    def _lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
except ImportError:  # POSIX
    import fcntl

    def _lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class FileLock:
    """Use as a context manager. Re-entrant within a thread; excludes other threads,
    other FileLock objects on the same path and other processes."""

    def __init__(self, path: str, timeout: float = 60.0, poll: float = 0.01) -> None:
        self.path, self.timeout, self.poll = path, timeout, poll
        self._tl = threading.RLock()
        self._depth = 0
        self._fh = None

    def __enter__(self):
        self._tl.acquire()
        try:
            if self._depth == 0:
                fh = open(self.path, "a+b")
                deadline = time.monotonic() + self.timeout
                while True:
                    try:
                        _lock(fh)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            fh.close()
                            raise ApprovalError("LEDGER_LOCK_TIMEOUT")
                        time.sleep(self.poll)
                self._fh = fh
            self._depth += 1
        except BaseException:
            self._tl.release()
            raise
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        try:
            if self._depth == 0:
                try:
                    _unlock(self._fh)
                finally:
                    self._fh.close()
                    self._fh = None
        finally:
            self._tl.release()
        return False
