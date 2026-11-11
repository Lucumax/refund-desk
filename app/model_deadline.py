"""A wall-clock deadline and a cap on calls in flight for any model call.

The socket timeout in model_client bounds each wait on the network, not the whole call: a provider that sends a byte every 29
seconds keeps a call alive for ever. Model calls now run outside the app-wide lock, so such a call holds a thread, not the shop;
but threads must not pile up either. `with_deadline(complete, seconds, max_in_flight)` runs each call on its own daemon thread and
gives up at the deadline (ModelTimeout). A call that has been abandoned keeps its place until it really ends, so a stuck provider
fills the cap and further calls are refused at once (ModelBusy) instead of starting more threads. Both are ordinary exceptions:
the extractor answers by rules, and the consent reader gives no reading, exactly as for any other model failure.

This lives in the app, not in vendor/: the vendored core is untouched. Python cannot kill a thread; an abandoned call is left to
finish on its own socket timeout and its result is dropped."""
from __future__ import annotations
import threading
from typing import Callable


class ModelTimeout(Exception):
    """The model did not answer within the wall-clock deadline."""


class ModelBusy(Exception):
    """Too many model calls are already in flight (some may be hung): refused without starting another."""


def with_deadline(complete: Callable[..., str], seconds: float = 45.0, max_in_flight: int = 8) -> Callable[..., str]:
    if seconds <= 0 or max_in_flight < 1:
        raise ValueError("deadline and cap must be positive")
    places = threading.BoundedSemaphore(max_in_flight)

    def guarded(*args, **kwargs):
        if not places.acquire(blocking=False):
            raise ModelBusy(f"{max_in_flight} model calls already in flight")
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["value"] = complete(*args, **kwargs)
            except BaseException as e:            # noqa: BLE001 - handed to the caller below
                box["error"] = e
            finally:
                places.release()
                done.set()
        try:
            threading.Thread(target=run, daemon=True, name="model-call").start()
        except BaseException:
            places.release()
            raise
        if not done.wait(seconds):
            raise ModelTimeout(f"no answer within {seconds:g} s")
        if "error" in box:
            raise box["error"]
        return box["value"]

    for attr in ("supports_system", "provider", "model"):
        if hasattr(complete, attr):
            setattr(guarded, attr, getattr(complete, attr))
    return guarded
