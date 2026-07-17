"""Regression tests for the usage-API backoff gate.

get_usage() must never attempt a fetch while _may_attempt() is False — even
when there is no cached value to serve instead. Falling through to the API in
the no-cache case (fresh install whose first request 429'd, or an expired
cache) is exactly how the Retry-After window gets ignored and the 429 state
sustains itself.
"""
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import usage_api  # noqa: E402


def test_backoff_with_no_cache_raises_instead_of_fetching(monkeypatch):
    monkeypatch.setattr(usage_api, "_load_last_good", lambda: (0.0, None))
    monkeypatch.setattr(usage_api, "_may_attempt", lambda now: False)
    monkeypatch.setitem(usage_api._mem, "data", None)
    attempts = []
    monkeypatch.setattr(usage_api, "_fetch", lambda: attempts.append(1))

    with pytest.raises(RuntimeError):
        usage_api.get_usage()

    assert not attempts, "must not hit the API during the backoff window"


def test_backoff_with_cache_still_serves_stale(monkeypatch):
    import time
    now = time.time()
    monkeypatch.setattr(usage_api, "_load_last_good",
                        lambda: (now - usage_api.CACHE_TTL - 1, {"five_hour": {}}))
    monkeypatch.setattr(usage_api, "_may_attempt", lambda now: False)
    monkeypatch.setitem(usage_api._mem, "data", None)

    assert usage_api.get_usage() == {"five_hour": {}}
    assert usage_api.last_serve_info()["stale"] is True
