"""Regression tests for the panel on/off toggle (docs/superpowers/specs/
2026-07-16-moonlighter-on-off-toggle-design.md).

`gate.compute_status()` must expose an explicit top-level `paused` boolean derived
from kill-switch FILE EXISTENCE — never string-matched from a check's `name`/`why`
(brittle: renaming the check would silently break the UI). These tests build a real
config (via `config.load()`) and only swap `kill_switch_path` to a tmp_path file, so
every other cfg key behaves exactly as in production.
"""
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import config as cfgmod   # noqa: E402
import gate as gatemod    # noqa: E402
import state as statemod  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A real config with kill_switch_path redirected into tmp_path, and the usage
    API / status-cache write short-circuited so the test never touches the network
    or the live install's ~/.moonlighter/last_status.json."""
    base = cfgmod.load()
    c = dict(base)
    c["kill_switch_path"] = tmp_path / "pause"

    # No network: gather_usage() would otherwise call the real Anthropic usage API.
    monkeypatch.setattr(gatemod, "gather_usage", lambda: (None, "test — no network"))
    # Don't clobber the real panel's status cache file with test data.
    monkeypatch.setattr(statemod, "write_status_cache", lambda status: None)

    return c


def test_paused_false_when_kill_switch_absent(cfg):
    assert not cfg["kill_switch_path"].exists()
    status = gatemod.compute_status(cfg)
    assert status["paused"] is False


def test_paused_true_when_kill_switch_present(cfg):
    cfg["kill_switch_path"].parent.mkdir(parents=True, exist_ok=True)
    cfg["kill_switch_path"].write_text("2026-07-16T00:00:00", encoding="utf-8")

    status = gatemod.compute_status(cfg)

    assert status["paused"] is True
    # Verdict must SKIP with the kill-switch check FAILing (existing contract,
    # unaffected by adding `paused` — the check list is not being replaced).
    assert status["gate"]["verdict"] == "SKIP"
    kill_checks = [c for c in status["gate"]["checks"] if c["name"] == "kill switch"]
    assert len(kill_checks) == 1
    assert kill_checks[0]["verdict"] == "FAIL"


def test_paused_flips_back_when_kill_switch_removed(cfg):
    p = cfg["kill_switch_path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    assert gatemod.compute_status(cfg)["paused"] is True

    p.unlink()
    assert gatemod.compute_status(cfg)["paused"] is False
