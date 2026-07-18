"""Regression tests for the usage-API backoff gate.

get_usage() must never attempt a fetch while _may_attempt() is False — even
when there is no cached value to serve instead. Falling through to the API in
the no-cache case (fresh install whose first request 429'd, or an expired
cache) is exactly how the Retry-After window gets ignored and the 429 state
sustains itself.
"""
import datetime
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import usage_api  # noqa: E402


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def test_backoff_with_no_cache_raises_instead_of_fetching(monkeypatch):
    monkeypatch.setattr(usage_api, "_load_last_good", lambda: (0.0, None))
    monkeypatch.setattr(usage_api, "_may_attempt", lambda now, window_reset=False: False)
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
    monkeypatch.setattr(usage_api, "_may_attempt", lambda now, window_reset=False: False)
    monkeypatch.setitem(usage_api._mem, "data", None)

    assert usage_api.get_usage() == {"five_hour": {}}
    assert usage_api.last_serve_info()["stale"] is True


def test_backoff_bypassed_once_cached_window_has_reset(monkeypatch):
    """A long Retry-After must not outlive the very window it is throttling.

    Once the cached five-hour window has rolled over, the cached utilization
    (e.g. 100%) is definitively wrong, so one throttled re-fetch is allowed even
    though the server's Retry-After has not elapsed — otherwise a scheduled run
    is wrongly held/missed against a pre-reset reading for up to an hour.
    """
    import time
    now = time.time()
    stale = {"five_hour": {"utilization": 100.0, "resets_at": _iso(now - 60)}}
    fresh = {"five_hour": {"utilization": 1.0, "resets_at": _iso(now + 5 * 3600)}}
    monkeypatch.setattr(usage_api, "_load_last_good", lambda: (now - 120, stale))
    monkeypatch.setitem(usage_api._mem, "data", None)
    monkeypatch.setitem(usage_api._mem, "ts", 0.0)
    # Backoff armed 120s ago with a 1-hour Retry-After: normally blocks any fetch.
    monkeypatch.setattr(usage_api, "_last_attempt", lambda: (now - 120, 3600.0))
    monkeypatch.setattr(usage_api, "_record_attempt", lambda *a, **k: None)
    monkeypatch.setattr(usage_api, "_save_last_good", lambda d: None)
    fetched = []

    def fake_fetch():
        fetched.append(1)
        return fresh

    monkeypatch.setattr(usage_api, "_fetch", fake_fetch)

    out = usage_api.get_usage()

    assert fetched, "a reset cached window must permit a re-fetch despite Retry-After"
    assert out["five_hour"]["utilization"] == 1.0


def test_reset_bypass_still_honours_min_interval(monkeypatch):
    """Even after a reset, don't hammer: the short anti-stampede floor still applies."""
    import time
    now = time.time()
    stale = {"five_hour": {"utilization": 100.0, "resets_at": _iso(now - 60)}}
    monkeypatch.setattr(usage_api, "_load_last_good", lambda: (now - 120, stale))
    monkeypatch.setitem(usage_api._mem, "data", None)
    monkeypatch.setitem(usage_api._mem, "ts", 0.0)
    # Last attempt only 5s ago (< MIN_ATTEMPT_INTERVAL) — reset or not, still throttled.
    monkeypatch.setattr(usage_api, "_last_attempt", lambda: (now - 5, 3600.0))
    fetched = []
    monkeypatch.setattr(usage_api, "_fetch", lambda: fetched.append(1))

    out = usage_api.get_usage()

    assert not fetched, "reset bypass must still honour MIN_ATTEMPT_INTERVAL"
    assert out == stale
