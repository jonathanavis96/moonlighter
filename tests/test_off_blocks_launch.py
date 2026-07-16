"""Switched off must refuse a launch up front, on every path.

The supervisor's kill-switch check only fires once the tmux session exists and the
mission has been sent, so by then an OFF Moonlighter has already spent quota and can
have acted. `moonlight start` and /api/start check the kill switch themselves, but
run.sh -> runner.main() is reached by paths that don't:

  - /api/apply (Apply on the night report) spawns run.sh directly
  - one-off env-override launchers (e.g. the Fable weekly burn) invoke run.sh directly
    specifically to bypass the CLI gate

runner.main() is the one gate all of them pass through, which is why the check lives
there rather than only at the callers.
"""
import pathlib
import sys

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import runner  # noqa: E402


def test_main_refuses_to_launch_when_switched_off(tmp_path, monkeypatch):
    """OFF must be rejected before any session is created."""
    kill_switch = tmp_path / "pause"
    kill_switch.write_text("off", encoding="utf-8")

    monkeypatch.setattr(
        runner.cfgmod, "load", lambda: {"kill_switch_path": kill_switch, "mode": "full-auto"}
    )
    monkeypatch.setattr(runner.state, "ensure_dirs", lambda: None)
    monkeypatch.setattr(runner.state, "gate_log", lambda msg: None)

    launched = []
    monkeypatch.setattr(runner, "_session_alive", lambda: launched.append("checked") or False)

    rc = runner.main()

    assert rc == 1, "main() must refuse to launch while the kill switch is present"
    assert launched == [], (
        "must bail out before even probing for a session — nothing may be launched "
        "while Moonlighter is switched off"
    )


def test_main_proceeds_past_the_gate_when_switched_on(tmp_path, monkeypatch):
    """Sanity: absent kill switch, main() gets past the gate to the usual checks.

    Stops at the in-flight guard so the test never drives a real tmux session.
    """
    kill_switch = tmp_path / "pause"  # deliberately not created

    monkeypatch.setattr(
        runner.cfgmod, "load", lambda: {"kill_switch_path": kill_switch, "mode": "full-auto"}
    )
    monkeypatch.setattr(runner.state, "ensure_dirs", lambda: None)
    monkeypatch.setattr(runner.state, "gate_log", lambda msg: None)
    monkeypatch.setattr(runner, "_session_alive", lambda: True)  # pretend one is running

    rc = runner.main()

    assert rc == 1, "reaches the in-flight guard, i.e. it passed the kill-switch gate"
