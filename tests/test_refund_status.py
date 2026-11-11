import pytest

from app.refund_status import advance, APPLIED, NOOP, STALE, CONFLICT, UNKNOWN_STATUS


@pytest.mark.parametrize("cur,inc,new,result", [
    (None, "PENDING", "PENDING", APPLIED),
    (None, "COMPLETED", "COMPLETED", APPLIED),
    ("PENDING", "COMPLETED", "COMPLETED", APPLIED),
    ("PENDING", "FAILED", "FAILED", APPLIED),
    ("PENDING", "CANCELLED", "CANCELLED", APPLIED),
    ("PENDING", "PENDING", "PENDING", NOOP),
    ("COMPLETED", "COMPLETED", "COMPLETED", NOOP),
    ("COMPLETED", "PENDING", "COMPLETED", STALE),
    ("FAILED", "PENDING", "FAILED", STALE),
    ("COMPLETED", "FAILED", "COMPLETED", CONFLICT),
    ("FAILED", "COMPLETED", "FAILED", CONFLICT),
    ("COMPLETED", "REVERSED", "COMPLETED", UNKNOWN_STATUS),
    ("PENDING", None, "PENDING", UNKNOWN_STATUS),
])
def test_transitions(cur, inc, new, result):
    t = advance(cur, inc)
    assert (t.new, t.result) == (new, result)
