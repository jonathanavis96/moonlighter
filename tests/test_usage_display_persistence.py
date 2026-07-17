"""Regression tests for display-persistence: the panel should keep showing the
last known usage reading (dated) instead of blanking once it ages past
STALE_GRACE, while the gate's decision path must stay untouched by that cached
value. See lib/gate.py::_display_freshness / lib/usage_api.py::last_known.
"""
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import config as cfgmod   # noqa: E402
import gate as gatemod    # noqa: E402
import state as statemod  # noqa: E402
import usage_api          # noqa: E402


def test_last_known_returns_load_last_good(monkeypatch):
    monkeypatch.setattr(usage_api, "_load_last_good", lambda: (123.0, {"five_hour": {}}))
    assert usage_api.last_known() == (123.0, {"five_hour": {}})


def test_last_known_returns_none_when_nothing_cached(monkeypatch):
    monkeypatch.setattr(usage_api, "_load_last_good", lambda: (0, None))
    assert usage_api.last_known() == (0, None)


def test_display_freshness_delegates_to_usage_freshness_when_usage_present(monkeypatch):
    monkeypatch.setattr(gatemod.usage_api, "last_serve_info",
                        lambda: {"fetched_at": 1_000.0, "age": 5.0, "stale": False})
    f = gatemod._display_freshness({"five_hour": {}}, 0.0, {"five_hour": {}})
    assert f["missing"] is False
    assert f["stale"] is False


def test_display_freshness_shows_cached_reading_as_stale_not_missing():
    disp_ts = 1_000.0
    f = gatemod._display_freshness(None, disp_ts, {"five_hour": {"utilization": 11}})
    assert f["missing"] is False
    assert f["stale"] is True
    assert f["as_of"] is not None


def test_display_freshness_missing_when_nothing_cached_at_all():
    f = gatemod._display_freshness(None, 0.0, None)
    assert f["missing"] is True
    assert f["stale"] is True
    assert f["as_of"] is None


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A real config with the usage API and status-cache write short-circuited,
    per the pattern in test_gate_paused.py."""
    base = cfgmod.load()
    c = dict(base)
    c["kill_switch_path"] = tmp_path / "pause"
    monkeypatch.setattr(statemod, "write_status_cache", lambda status: None)
    return c


def test_compute_status_falls_back_to_cached_reading_when_usage_none(cfg, monkeypatch):
    """decision-grade usage is unavailable, but a cached reading exists: the
    contract must expose it for DISPLAY (has_data True, missing False) while
    `live` stays False so the gate never launches on it."""
    monkeypatch.setattr(gatemod, "gather_usage", lambda: (None, "429 backing off"))
    cached = {
        "five_hour": {"utilization": 11.0, "resets_at": None},
        "seven_day": {"utilization": 28.0, "resets_at": None},
        "seven_day_sonnet": {"utilization": 2.0, "resets_at": None},
    }
    monkeypatch.setattr(gatemod.usage_api, "last_known", lambda: (1_000.0, cached))

    status = gatemod.compute_status(cfg)

    assert status["live"] is False
    assert status["usage"]["has_data"] is True
    assert status["usage"]["missing"] is False
    assert status["usage"]["stale"] is True
    assert status["usage"]["five_hour"]["utilization"] == 11.0
    assert status["usage"]["seven_day"]["utilization"] == 28.0


def test_compute_status_reports_missing_when_no_reading_ever_existed(cfg, monkeypatch):
    monkeypatch.setattr(gatemod, "gather_usage", lambda: (None, "no network yet"))
    monkeypatch.setattr(gatemod.usage_api, "last_known", lambda: (0.0, None))

    status = gatemod.compute_status(cfg)

    assert status["live"] is False
    assert status["usage"]["has_data"] is False
    assert status["usage"]["missing"] is True
    assert status["usage"]["five_hour"]["utilization"] is None
