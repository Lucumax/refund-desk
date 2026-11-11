"""Small JSON-file store for app state that is not a receipt: buyer requests (with the immutable
intent proposed for each), the refund-id hint index, persisted webhook events. Writes are atomic
(temp file then replace).

This file is NOT trusted for money. Held amounts, provider statuses and outcomes are derived from
the signed receipt chain (app/ledgerview.py). A request here is only a pointer; views check it
against the signed receipt before showing it or acting on it."""
from __future__ import annotations
import json
import os
import threading


class Store:
    def __init__(self, path: str | None = None):
        self.path, self._lock = path, threading.RLock()
        self.data = {"seq": 0, "requests": {}, "refund_index": {}, "events": {}, "conflicts": []}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)

    def next_seq(self) -> int:
        with self._lock:
            self.data["seq"] += 1
            self.save()
            return self.data["seq"]

    @property
    def requests(self) -> dict:
        return self.data["requests"]

    @property
    def refund_index(self) -> dict:
        return self.data["refund_index"]

    @property
    def events(self) -> dict:
        return self.data["events"]
