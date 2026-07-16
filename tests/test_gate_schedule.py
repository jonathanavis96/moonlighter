"""Tests for the gate's scheduled-task integration (`gate._process_scheduled`).

Never launches a real run.sh or tmux — `subprocess.Popen` and `run_in_flight`
are monkeypatched, and the schedule file + mission-file target are redirected
into tmp_path so nothing here touches the real ~/.moonlighter.
"""
import datetime
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import gate       # noqa: E402
import schedule   # noqa: E402


def _task(run_at, **extra):
    t = {
        "id": schedule.new_id(run_at),
        "created": "2026-07-16T21:40:00+02:00",
        "run_at": run_at.isoformat(),
        "prompt": "Fix security patching",
        "folder": "/home/user/code/example",
        "docs": ["/home/user/notes/brief.md"],
        "wallclock_min": 300,
        "five_target": 80,
        "status": schedule.STATUS_PENDING,
        "fired_at": None,
        "run_id": None,
        "note": None,
    }
    t.update(extra)
    return t


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate schedule storage, the gate's state writes, and Popen."""
    sched_path = tmp_path / "scheduled.json"
    monkeypatch.setattr(schedule, "_path", lambda: sched_path)

    monkeypatch.setattr(gate.state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(gate.state, "ensure_dirs", lambda: None)
    monkeypatch.setattr(gate.state, "gate_log", lambda line: None)

    popen_calls = []

    def fake_popen(cmd, env=None, **kwargs):
        popen_calls.append({"cmd": cmd, "env": env, "kwargs": kwargs})

        class _P:
            pid = 12345
        return _P()

    monkeypatch.setattr(gate.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(gate, "run_in_flight", lambda: False)

    cfg = {
        "kill_switch_path": tmp_path / "pause",  # not created => ON by default
        "_project_dir": tmp_path,
    }
    return cfg, popen_calls


def test_due_task_missed_when_switched_off_and_never_launched(env):
    cfg, popen_calls = env
    cfg["kill_switch_path"].write_text("off", encoding="utf-8")

    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5))
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is False
    assert popen_calls == []
    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_MISSED
    assert updated["note"] == "switched off"


def test_due_task_missed_when_run_already_in_flight(env, monkeypatch):
    cfg, popen_calls = env
    monkeypatch.setattr(gate, "run_in_flight", lambda: True)

    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5))
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is False
    assert popen_calls == []
    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_MISSED
    assert "in flight" in updated["note"]


def test_due_task_launches_with_correct_env_and_marked_fired(env):
    cfg, popen_calls = env

    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5))
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is True
    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["cmd"] == ["bash", str(cfg["_project_dir"] / "run.sh")]
    assert call["kwargs"]["start_new_session"] is True

    launch_env = call["env"]
    assert launch_env["ML_MISSION_FILE"].endswith(f"scheduled-mission-{task['id']}.md")
    mission_path = pathlib.Path(launch_env["ML_MISSION_FILE"])
    assert mission_path.exists()
    assert "Fix security patching" in mission_path.read_text(encoding="utf-8")
    assert launch_env["ML_WALLCLOCK_MIN"] == "300"
    assert launch_env["ML_FIVE_TARGET"] == "80"

    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_FIRED
    assert updated["fired_at"] is not None


def test_future_task_left_pending_and_not_launched(env):
    cfg, popen_calls = env

    now = datetime.datetime.now().astimezone()
    task = _task(now + datetime.timedelta(hours=1))
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is False
    assert popen_calls == []
    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_PENDING
