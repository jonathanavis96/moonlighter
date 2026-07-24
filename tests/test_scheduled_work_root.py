"""Regression tests for the 2026-07-17 05:14 scheduled-run bug.

`build_scheduled_mission()` told the agent to "stay within the validated Work root"
without ever stating what that root WAS. An agent that cannot independently verify
an unstated root has no safe way to read that instruction except as "don't act" —
which is exactly what happened: the 05:14 run downgraded its whole mission to
audit-only instead of doing the scheduled task.

Covers:
  (a) the generated brief states the resolved Work root path literally, in both the
      dedicated section and the HARD RULES that reference it
  (b) an unresolvable Work root fails loudly (raises), both at the mission-builder
      level and end-to-end through runner.main() (which must LOG it too — run.sh
      discards stderr, so an uncaught exception alone would still be invisible)
  (c) the run.json `"apply": false` wording is clarified so it can't be misread as
      a blanket "do nothing" instruction
"""
import datetime
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import gate as gatemod    # noqa: E402
import runner             # noqa: E402
import schedule           # noqa: E402
import state as statemod  # noqa: E402

BASE_CFG = {"off_limits_resolved": ["/home/user/.ssh"]}


def _mission_kwargs(work_root, run_dir):
    return dict(
        cfg=BASE_CFG,
        run_dir=run_dir,
        scheduled_text="Tidy up the Downloads folder.",
        work_root=work_root,
        dry_run=False,
        five_now=10.0,
        five_target=80.0,
        weekly_now=20.0,
        weekly_cap=90.0,
        reserve=10.0,
        wallclock_min=60,
    )


def test_brief_states_the_resolved_work_root(tmp_path):
    run_dir = tmp_path / "runs" / "testrun"
    mission = runner.build_scheduled_mission(**_mission_kwargs("/home/user/Downloads", run_dir))

    assert "## Work root" in mission
    assert "Your validated Work root for this task is `/home/user/Downloads`" in mission

    # HARD RULES must reference the SAME concrete path, not a vague "that validated
    # Work root" with nothing for the agent to resolve it against.
    hard_rules = mission.split("## HARD RULES")[1]
    assert "/home/user/Downloads" in hard_rules


@pytest.mark.parametrize("bad_root", ["", None, "   "])
def test_unresolvable_work_root_fails_loudly(tmp_path, bad_root):
    run_dir = tmp_path / "runs" / "testrun"
    with pytest.raises(ValueError, match="no Work root resolved"):
        runner.build_scheduled_mission(**_mission_kwargs(bad_root, run_dir))


def test_apply_false_wording_is_clarified(tmp_path):
    run_dir = tmp_path / "runs" / "testrun"
    mission = runner.build_scheduled_mission(**_mission_kwargs("/home/user/Downloads", run_dir))

    assert '"apply": false' in mission
    assert "apply approved items" in mission
    assert "Do NOT read `apply: false` as a blanket instruction to do nothing" in mission


def test_main_refuses_to_launch_when_work_root_is_unresolved(tmp_path, monkeypatch):
    """End-to-end: ML_MISSION_FILE is set but ML_WORK_ROOT is not (e.g. the task
    record had no folder) — main() must log an explicit error and refuse, never
    launch a tmux session with an ambiguous brief."""
    run_dir = tmp_path / "runs" / "testrun"
    run_dir.mkdir(parents=True)
    logs = []

    cfg = {
        "kill_switch_path": tmp_path / "no-kill-switch",
        "mode": "full-auto",
        "five_hour_target_pct": 80,
        "weekly_reserve_pct": 10,
    }
    monkeypatch.setattr(runner.cfgmod, "load", lambda: cfg)
    monkeypatch.setattr(runner.state, "ensure_dirs", lambda: None)
    monkeypatch.setattr(runner.state, "gate_log", lambda msg: logs.append(msg))
    monkeypatch.setattr(runner.state, "new_run_dir", lambda: ("testrun", run_dir))
    monkeypatch.setattr(runner, "_session_alive", lambda: False)
    monkeypatch.setattr(runner.usage_api, "get_usage", lambda force=False: {})
    monkeypatch.setattr(runner, "_read_mission_file_env", lambda: "Tidy the Downloads folder.")
    monkeypatch.delenv("ML_WORK_ROOT", raising=False)

    rc = runner.main()

    assert rc == 1, "must refuse to launch rather than send an ambiguous brief"
    assert any("Work root" in m for m in logs), (
        "the failure must be LOGGED (gate_log), not just raised — run.sh launches "
        "runner.py with stderr=DEVNULL, so an uncaught exception alone is invisible"
    )


def test_main_passes_the_resolved_work_root_through_to_the_mission_builder(tmp_path, monkeypatch):
    """Sanity: with ML_WORK_ROOT set, main() must plumb it through to
    build_scheduled_mission() as the structural work_root argument — not leave it
    only optionally embedded in the free-form mission text."""
    run_dir = tmp_path / "runs" / "testrun"
    run_dir.mkdir(parents=True)

    cfg = {
        "kill_switch_path": tmp_path / "no-kill-switch",
        "mode": "full-auto",
        "five_hour_target_pct": 80,
        "weekly_reserve_pct": 10,
    }
    monkeypatch.setattr(runner.cfgmod, "load", lambda: cfg)
    monkeypatch.setattr(runner.state, "ensure_dirs", lambda: None)
    monkeypatch.setattr(runner.state, "gate_log", lambda msg: None)
    monkeypatch.setattr(runner.state, "new_run_dir", lambda: ("testrun", run_dir))
    monkeypatch.setattr(runner, "_session_alive", lambda: False)
    monkeypatch.setattr(runner.usage_api, "get_usage", lambda force=False: {})
    monkeypatch.setattr(runner, "_read_mission_file_env", lambda: "Tidy the Downloads folder.")
    monkeypatch.setenv("ML_WORK_ROOT", "/home/user/Downloads")

    seen = {}

    class _StopAfterBuild(Exception):
        pass

    def fake_build(cfg, run_dir, scheduled_text, work_root, *rest):
        seen["work_root"] = work_root
        raise _StopAfterBuild()

    monkeypatch.setattr(runner, "build_scheduled_mission", fake_build)

    with pytest.raises(_StopAfterBuild):
        runner.main()

    assert seen["work_root"] == "/home/user/Downloads"


def test_gate_process_scheduled_sets_ml_work_root_from_the_validated_folder(tmp_path, monkeypatch):
    """The other half of the wiring: gate.py::_process_scheduled() is the process
    that actually knows the task's validated `folder` (set by `_validate_schedule()`
    in panel/server.py). It must hand that to the runner subprocess as ML_WORK_ROOT
    — this is the only way runner.py's build_scheduled_mission() can ever see it."""
    monkeypatch.setattr(statemod, "STATE_DIR", tmp_path)

    run_at = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=5)
    task = {
        "id": schedule.new_id(run_at),
        "created": statemod.now_iso(),
        "run_at": run_at.isoformat(),
        "prompt": "Tidy up the Downloads folder.",
        "folder": "/home/user/Downloads",
        "docs": [],
        "wallclock_min": None,
        "five_target": None,
        "status": schedule.STATUS_PENDING,
        "fired_at": None,
        "run_id": None,
        "note": None,
    }
    schedule.add(task)

    cfg = {
        "kill_switch_path": tmp_path / "no-kill-switch",
        "_project_dir": tmp_path,
        "night_model": "default",
    }
    monkeypatch.setattr(gatemod, "run_in_flight", lambda: False)
    monkeypatch.setattr(gatemod, "gather_usage",
                        lambda: ({"five_hour": {"utilization": 10}, "seven_day": {"utilization": 20}}, None))
    monkeypatch.setattr(gatemod.budgetmod, "compute", lambda usage, cfg, abname: {"ok": True})

    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(kwargs)
        return object()

    monkeypatch.setattr(gatemod.subprocess, "Popen", fake_popen)

    fired = gatemod._process_scheduled(cfg)

    assert fired is True
    assert len(popen_calls) == 1
    assert popen_calls[0]["env"]["ML_WORK_ROOT"] == "/home/user/Downloads"
