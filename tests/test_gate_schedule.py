"""Tests for the gate's scheduled-task integration (`gate._process_scheduled`).

Never launches a real run.sh or tmux — `subprocess.Popen` and `run_in_flight`
are monkeypatched, and the schedule file + mission-file target are redirected
into tmp_path so nothing here touches the real ~/.moonlighter.
"""
import datetime
import pathlib
import sys
import types

import pytest

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda *a, **k: {}, safe_dump=lambda *a, **k: ""))

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
    monkeypatch.setattr(gate, "gather_usage", lambda: ({
        "five_hour": {"utilization": 10},
        "seven_day": {"utilization": 20},
        "seven_day_sonnet": {"utilization": 20},
    }, None))

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
    assert launch_env["ML_ACTIVE_BUCKET"] == "seven_day"

    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_FIRED
    assert updated["fired_at"] is not None


def test_due_task_passes_sonnet_active_bucket(env):
    cfg, popen_calls = env
    cfg["night_model"] = "sonnet"

    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5))
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is True
    assert popen_calls[0]["env"]["ML_ACTIVE_BUCKET"] == "seven_day_sonnet"


def test_remaining_due_tasks_stay_pending_after_one_launch(env):
    cfg, popen_calls = env

    now = datetime.datetime.now().astimezone()
    first = _task(now - datetime.timedelta(minutes=10), id="first")
    second = _task(now - datetime.timedelta(minutes=5), id="second")
    schedule.save([second, first])

    fired = gate._process_scheduled(cfg)

    assert fired is True
    assert len(popen_calls) == 1
    assert schedule.get(first["id"])["status"] == schedule.STATUS_FIRED
    assert schedule.get(second["id"])["status"] == schedule.STATUS_PENDING
    assert schedule.get(second["id"])["note"] is None


def test_due_task_missed_when_launch_fails(env, monkeypatch):
    cfg, popen_calls = env

    def fail_popen(*args, **kwargs):
        popen_calls.append({"args": args, "kwargs": kwargs})
        raise OSError("boom")

    monkeypatch.setattr(gate.subprocess, "Popen", fail_popen)
    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5))
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is False
    assert len(popen_calls) == 1
    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_MISSED
    assert "launch failed" in updated["note"]


def test_due_task_not_launched_if_cancelled_before_claim(env, monkeypatch):
    cfg, popen_calls = env

    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5))
    schedule.save([task])

    real_claim = schedule.claim

    def cancel_then_claim(task_id):
        schedule.cancel(task_id)
        return real_claim(task_id)

    monkeypatch.setattr(schedule, "claim", cancel_then_claim)

    fired = gate._process_scheduled(cfg)

    assert fired is False
    assert popen_calls == []
    assert schedule.get(task["id"])["status"] == schedule.STATUS_CANCELLED

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


def test_due_task_missed_when_custom_five_hour_cap_already_hit(env):
    cfg, popen_calls = env

    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5), five_target=8)
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is False
    assert popen_calls == []
    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_MISSED
    assert "5h window" in updated["note"]


def test_due_task_missed_when_ml_reserve_stricter_than_config(env, monkeypatch):
    # A stricter ML_RESERVE than config means the runner will refuse before launching; the
    # scheduled preflight must honour the same reserve, or the task is marked FIRED here and
    # then silently vanishes when run.sh refuses. seven_day=60 is under the config cap (90)
    # but over the ML_RESERVE=50 cap.
    cfg, popen_calls = env
    cfg["weekly_reserve_pct"] = 10
    monkeypatch.setenv("ML_RESERVE", "50")
    monkeypatch.setattr(gate, "gather_usage", lambda: ({
        "five_hour": {"utilization": 10},
        "seven_day": {"utilization": 60},
        "seven_day_sonnet": {"utilization": 20},
    }, None))

    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5))
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is False
    assert popen_calls == [], "must not spawn run.sh that the runner will refuse on the stricter reserve"
    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_MISSED
    assert "weekly reserve" in updated["note"]


def test_due_task_missed_when_weekly_cap_already_hit(env, monkeypatch):
    cfg, popen_calls = env
    monkeypatch.setattr(gate, "gather_usage", lambda: ({
        "five_hour": {"utilization": 10},
        "seven_day": {"utilization": 95},
        "seven_day_sonnet": {"utilization": 20},
    }, None))

    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(minutes=5))
    schedule.save([task])

    fired = gate._process_scheduled(cfg)

    assert fired is False
    assert popen_calls == []
    updated = schedule.get(task["id"])
    assert updated["status"] == schedule.STATUS_MISSED
    assert "weekly reserve" in updated["note"]
