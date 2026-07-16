"""Regression coverage for scheduled run.sh launches.

The gate passes one-off scheduled missions and caps to run.sh through
ML_MISSION_FILE, ML_WALLCLOCK_MIN, and ML_FIVE_TARGET. runner.main() is the
process that receives those env vars, so it must use them before launching and
supervising Claude.
"""
import datetime
import json
import pathlib
import sys
import types

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda *a, **k: {}, safe_dump=lambda *a, **k: ""))

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import runner  # noqa: E402


def test_scheduled_env_mission_and_caps_drive_runner_main(tmp_path, monkeypatch):
    custom_mission = "# Scheduled mission\n\nOnly audit /tmp/project and stop on custom caps.\n"
    mission_file = tmp_path / "scheduled.md"
    mission_file.write_text(custom_mission, encoding="utf-8")
    monkeypatch.setenv("ML_MISSION_FILE", str(mission_file))
    monkeypatch.setenv("ML_WALLCLOCK_MIN", "17")
    monkeypatch.setenv("ML_FIVE_TARGET", "42")

    kill_switch = tmp_path / "pause"
    cfg = {
        "kill_switch_path": kill_switch,
        "mode": "full-auto",
        "max_wallclock_min": 360,
        "five_hour_target_pct": 80,
        "weekly_reserve_pct": 10,
        "night_model": "default",
    }
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()

    monkeypatch.setattr(runner.cfgmod, "load", lambda: cfg)
    monkeypatch.setattr(runner.state, "ensure_dirs", lambda: None)
    monkeypatch.setattr(runner.state, "new_run_dir", lambda: ("run-1", run_dir))
    monkeypatch.setattr(runner.state, "gate_log", lambda msg: None)
    monkeypatch.setattr(runner.state, "append_calibration", lambda data: None)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(runner, "_session_alive", lambda: False)
    monkeypatch.setattr(runner, "_capture", lambda: ">")
    monkeypatch.setattr(runner, "_session_transcripts", lambda started_ts: [])
    monkeypatch.setattr(runner, "_sum_tokens", lambda paths: 0)
    monkeypatch.setattr(runner.usage_api, "get_usage", lambda force=False: {
        "five_hour": {"utilization": 5},
        "seven_day": {"utilization": 20},
        "seven_day_sonnet": {"utilization": 7},
    })
    monkeypatch.setattr(runner.revertmod, "write_revert_script", lambda rd: None)
    monkeypatch.setattr(runner.reportmod, "write_report", lambda cfg, rd, meta: None)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)

    supervise_calls = []

    def fake_supervise(cfg_arg, run_dir_arg, summary_path, hard_deadline, bucket,
                       five_target, weekly_cap):
        supervise_calls.append({
            "run_dir": run_dir_arg,
            "summary_path": summary_path,
            "hard_deadline": hard_deadline,
            "bucket": bucket,
            "five_target": five_target,
            "weekly_cap": weekly_cap,
        })
        summary_path.write_text("scheduled done\n", encoding="utf-8")
        return "completed"

    monkeypatch.setattr(runner, "_supervise", fake_supervise)

    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append(args[0])

        class _Result:
            returncode = 0
            stdout = ""
        return _Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    rc = runner.main()

    assert rc == 0
    mission = (run_dir / "mission.md").read_text(encoding="utf-8")
    assert custom_mission.strip() in mission
    assert "## MODE: FULL-AUTO" in mission
    assert "EVERY filesystem mutation MUST go through the helper" in mission
    assert "Nothing outward-facing, EVER" in mission
    assert "one-off scheduled task, not the broad nightly estate audit" in mission

    [supervise_call] = supervise_calls
    assert supervise_call["five_target"] == 42
    assert supervise_call["hard_deadline"] - datetime.timedelta(minutes=17) < datetime.datetime.now()
    assert supervise_call["hard_deadline"] - datetime.timedelta(minutes=17) > datetime.datetime.now() - datetime.timedelta(minutes=1)

    run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_meta["five_target_pct"] == 42
    assert run_meta["headline"] == "scheduled done"
