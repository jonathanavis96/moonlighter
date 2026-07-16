"""Regression tests for stopping an in-flight run when Moonlighter is
switched off.

The supervisor loop in lib/runner.py already breaks out on wall-clock and
budget stops by setting `stop_reason`, calling `_graceful_stop()`, and
breaking. This adds the kill-switch file as one more stop condition, using
the exact same break path so an interrupted run still gets revert.sh, the
report, and the notify step.

The loop lived inline inside main() (which drives a real tmux session and
isn't safely testable), so it was extracted into `_supervise()` — pure
extraction, no behaviour change beyond the new kill-switch check — to make
it directly exercisable here.
"""
import datetime
import inspect
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import runner  # noqa: E402


@pytest.fixture
def run_dir(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    return rd


def test_kill_switch_created_mid_run_stops_the_loop(run_dir, monkeypatch):
    """Creating the kill-switch file mid-run must set the switched-off
    stop_reason, invoke _graceful_stop() (the same helper the wall-clock and
    budget stops use) with the real reason, and break out of the loop."""
    kill_switch = run_dir / "pause"
    cfg = {"kill_switch_path": kill_switch}

    # ask.json present => the loop skips the pane-capture/idle bookkeeping
    # entirely, so this test needs no tmux mocking at all.
    (run_dir / "ask.json").write_text("{}", encoding="utf-8")
    summary_path = run_dir / "summary.md"

    # Simulate the panel switching Moonlighter off mid-run: the kill-switch
    # file appears between one iteration and the next. _session_alive() is
    # the first thing each iteration checks, so use it as the trigger point.
    calls = {"n": 0}

    def fake_session_alive():
        calls["n"] += 1
        if calls["n"] == 1:
            kill_switch.write_text("", encoding="utf-8")
        return True

    budget_stop_calls = []
    monkeypatch.setattr(runner, "_session_alive", fake_session_alive)
    monkeypatch.setattr(runner, "_graceful_stop", lambda why=None: budget_stop_calls.append(why))

    hard_deadline = datetime.datetime.now() + datetime.timedelta(hours=1)
    stop_reason = runner._supervise(
        cfg, run_dir, summary_path, hard_deadline,
        bucket="seven_day", five_target=80, weekly_cap=90,
    )

    assert stop_reason == "switched off from panel"
    assert budget_stop_calls == ["Switched off from the panel"], (
        "must call _graceful_stop() to wind the session down, and must tell it the real "
        "reason — a switch-off is not a budget stop"
    )
    assert not summary_path.exists(), "loop breaks promptly, without waiting for a session-authored summary"


def test_absent_kill_switch_does_not_stop_the_loop(run_dir, monkeypatch):
    """Sanity check: without the kill-switch file, the loop doesn't stop for
    that reason (it falls through to the next check, here session-ended)."""
    cfg = {"kill_switch_path": run_dir / "pause"}
    (run_dir / "ask.json").write_text("{}", encoding="utf-8")
    summary_path = run_dir / "summary.md"

    monkeypatch.setattr(runner, "_session_alive", lambda: False)

    hard_deadline = datetime.datetime.now() + datetime.timedelta(hours=1)
    stop_reason = runner._supervise(
        cfg, run_dir, summary_path, hard_deadline,
        bucket="seven_day", five_target=80, weekly_cap=90,
    )

    assert stop_reason == "session ended"


def test_finalize_runs_unconditionally_after_supervise():
    """Regression guard: main() must call revert/report/notify unconditionally
    after _supervise() returns, for ANY stop_reason (no early return that
    could let the new switched-off path skip finalisation)."""
    src = inspect.getsource(runner.main)
    idx_supervise = src.index("stop_reason = _supervise(")
    idx_revert = src.index("revertmod.write_revert_script(run_dir)")
    idx_report = src.index("reportmod.write_report(cfg, run_dir, run_meta)")
    assert idx_supervise < idx_revert < idx_report

    between = src[idx_supervise:idx_revert]
    assert "return" not in between, (
        "no early return may sit between the supervisor loop and the "
        "revert/report/notify wrap-up — every stop_reason must finalise"
    )
