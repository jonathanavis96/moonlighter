"""Regression coverage for the `gc` command hardening and Sonnet quota-bucket derivation
(Codex / CodeRabbit findings on the consolidation PR).

gc must: reject a negative retention window; fail closed on a missing/invalid run
timestamp (never purge on unknown age); report deletion failures instead of claiming
success; and mark a purged run non-revertible so a later revert refuses rather than
silently restoring nothing.

runner.main() must derive the weekly quota bucket from the effective night model when the
caller didn't set ML_ACTIVE_BUCKET, so a Sonnet run is checked against the Sonnet reserve.
"""
import datetime
import json
import pathlib
import sys
import types

sys.modules.setdefault("yaml", types.SimpleNamespace(
    safe_load=lambda *a, **k: {}, safe_dump=lambda *a, **k: ""))

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import cli  # noqa: E402
import revert  # noqa: E402
import runner  # noqa: E402


def _make_run(runs_dir, name, *, status="clean", finished=None, with_data=True,
              with_revert=True):
    rd = runs_dir / name
    rd.mkdir(parents=True)
    meta = {"status": status}
    if finished is not None:
        meta["finished"] = finished
    (rd / "run.json").write_text(json.dumps(meta))
    if with_data:
        (rd / "trash").mkdir()
        (rd / "trash" / "f").write_text("x" * 100)
        (rd / "snapshot").mkdir()
        (rd / "snapshot" / "f").write_text("y" * 100)
    if with_revert:
        (rd / "revert.sh").write_text("#!/bin/bash\necho revert\n")
    return rd


def _old_iso(days_ago=30):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()


def test_gc_rejects_negative_days(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.state, "RUNS_DIR", tmp_path / "runs")
    rc = cli.cmd_gc(types.SimpleNamespace(days=-1, dry_run=False))
    assert rc == 2
    assert "must be >= 0" in capsys.readouterr().err


def test_gc_keeps_run_with_invalid_timestamp(monkeypatch, tmp_path, capsys):
    runs = tmp_path / "runs"
    rd = _make_run(runs, "20260101-000000", finished="not-a-date")
    monkeypatch.setattr(cli.state, "RUNS_DIR", runs)
    rc = cli.cmd_gc(types.SimpleNamespace(days=14, dry_run=False))
    assert rc == 0
    # Fail closed: data untouched, revert.sh intact, not flagged.
    assert (rd / "trash").exists() and (rd / "snapshot").exists()
    assert (rd / "revert.sh").exists()
    assert "invalid run timestamp" in capsys.readouterr().out
    assert json.loads((rd / "run.json").read_text()).get("revert_purged") is not True


def test_gc_purges_old_run_and_marks_non_revertible(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    rd = _make_run(runs, "20260101-000000", finished=_old_iso(30))
    monkeypatch.setattr(cli.state, "RUNS_DIR", runs)
    rc = cli.cmd_gc(types.SimpleNamespace(days=14, dry_run=False))
    assert rc == 0
    assert not (rd / "trash").exists() and not (rd / "snapshot").exists()
    # revert.sh removed and the run flagged so revert refuses.
    assert not (rd / "revert.sh").exists()
    meta = json.loads((rd / "run.json").read_text())
    assert meta.get("revert_purged") is True
    assert any("purged by gc" in e for e in meta.get("finalisation_errors", []))


def test_gc_marks_purged_when_rmtree_partially_fails(monkeypatch, tmp_path):
    # rmtree can delete some children and then raise (permission error / race), leaving the
    # top-level dir present so removed_any stays False. Restore data is already partially
    # gone, so the run must be marked non-revertible — not left advertised as revertible.
    runs = tmp_path / "runs"
    rd = _make_run(runs, "20260101-000000", finished=_old_iso(30))
    monkeypatch.setattr(cli.state, "RUNS_DIR", runs)

    def flaky_rmtree(path, *a, **k):
        p = pathlib.Path(path)
        for child in p.iterdir():
            child.unlink()            # partial deletion of restore data
        raise OSError("permission denied mid-purge")

    monkeypatch.setattr(cli.shutil, "rmtree", flaky_rmtree)
    cli.cmd_gc(types.SimpleNamespace(days=14, dry_run=False))
    meta = json.loads((rd / "run.json").read_text())
    assert meta.get("revert_purged") is True, "a partially-purged run must be non-revertible"


def test_gc_zero_byte_purge_marks_non_revertible(monkeypatch, tmp_path):
    # An empty trashed/snapshot dir frees 0 bytes but is still a revert record — removing it
    # must still mark the run non-revertible (key on removal, not byte count).
    runs = tmp_path / "runs"
    rd = runs / "20260101-000000"
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"status": "clean", "finished": _old_iso(30)}))
    (rd / "trash").mkdir()      # empty → 0 bytes
    (rd / "snapshot").mkdir()   # empty → 0 bytes
    (rd / "revert.sh").write_text("#!/bin/bash\n")
    monkeypatch.setattr(cli.state, "RUNS_DIR", runs)
    rc = cli.cmd_gc(types.SimpleNamespace(days=14, dry_run=False))
    assert rc == 0
    assert not (rd / "trash").exists() and not (rd / "snapshot").exists()
    assert json.loads((rd / "run.json").read_text()).get("revert_purged") is True
    assert not (rd / "revert.sh").exists()


def test_gc_dry_run_touches_nothing(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    rd = _make_run(runs, "20260101-000000", finished=_old_iso(30))
    monkeypatch.setattr(cli.state, "RUNS_DIR", runs)
    cli.cmd_gc(types.SimpleNamespace(days=14, dry_run=True))
    assert (rd / "trash").exists() and (rd / "revert.sh").exists()
    assert json.loads((rd / "run.json").read_text()).get("revert_purged") is not True


def test_run_revert_refuses_purged_run(monkeypatch, tmp_path, capsys):
    runs = tmp_path / "runs"
    rd = runs / "20260101-000000"
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"status": "clean", "revert_purged": True}))
    monkeypatch.setattr(revert, "STATE_RUNS", runs)
    called = []
    monkeypatch.setattr(revert.subprocess, "call", lambda *a, **k: called.append(a) or 0)
    rc = revert.run_revert("20260101-000000")
    assert rc == 1
    assert not called, "must not execute any revert script for a purged run"
    assert "no longer revertible" in capsys.readouterr().err


def test_revert_items_refuses_purged_run(monkeypatch, tmp_path):
    # The panel's approve/undo flow calls revert_items() directly, bypassing run_revert.
    # A purged run's per-item undo would skip the now-missing restore data and falsely
    # report success — the guard must cover this path too.
    runs = tmp_path / "runs"
    rd = runs / "20260101-000000"
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"status": "clean", "revert_purged": True}))
    (rd / "manifest.jsonl").write_text(
        json.dumps({"op": "trash", "path": "/x", "trash": "/gone"}) + "\n")
    monkeypatch.setattr(revert, "STATE_RUNS", runs)
    touched = []
    monkeypatch.setattr(revert, "_revert_one",
                        lambda run_dir, rec: touched.append(rec) or (True, "ok"))

    res = revert.revert_items("20260101-000000", [0])

    assert res["ok"] is False
    assert res["reverted"] == []
    assert not touched, "must not attempt per-item revert for a purged run"
    assert any("no longer revertible" in e for e in res["errors"])


def _drive_main(monkeypatch, tmp_path, cfg, env, usage=None):
    """Minimal runner.main() harness. Returns the dict _supervise was called with (empty if
    the run refused before launch)."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    usage = usage or {"five_hour": {"utilization": 5}, "seven_day": {"utilization": 20},
                      "seven_day_sonnet": {"utilization": 7}}
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
    monkeypatch.setattr(runner.usage_api, "get_usage", lambda force=False: usage)
    monkeypatch.setattr(runner.revertmod, "write_revert_script", lambda rd: None)
    monkeypatch.setattr(runner.reportmod, "write_report", lambda cfg, rd, meta: None)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    seen = {}

    def fake_supervise(cfg_arg, rd, summary_path, hard_deadline, bucket, five_target, weekly_cap):
        seen["bucket"] = bucket
        summary_path.write_text("done\n", encoding="utf-8")
        return "completed"

    monkeypatch.setattr(runner, "_supervise", fake_supervise)
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=""))
    seen["rc"] = runner.main()
    return seen


def _base_cfg(tmp_path):
    return {"kill_switch_path": tmp_path / "pause", "mode": "full-auto",
            "max_wallclock_min": 360, "five_hour_target_pct": 80,
            "weekly_reserve_pct": 10, "night_model": "default"}


def test_sonnet_night_model_uses_sonnet_bucket(monkeypatch, tmp_path):
    seen = _drive_main(monkeypatch, tmp_path, _base_cfg(tmp_path), {"ML_NIGHT_MODEL": "sonnet"})
    assert seen.get("bucket") == "seven_day_sonnet"


def test_sonnet_model_overrides_explicit_non_sonnet_bucket(monkeypatch, tmp_path):
    # The model is authoritative: a Sonnet run must draw the Sonnet pool even if a launcher
    # exported ML_ACTIVE_BUCKET=seven_day from cfg before seeing the ML_NIGHT_MODEL override.
    seen = _drive_main(monkeypatch, tmp_path, _base_cfg(tmp_path),
                       {"ML_NIGHT_MODEL": "sonnet", "ML_ACTIVE_BUCKET": "seven_day"})
    assert seen.get("bucket") == "seven_day_sonnet"


def test_non_sonnet_model_uses_general_bucket(monkeypatch, tmp_path):
    seen = _drive_main(monkeypatch, tmp_path, _base_cfg(tmp_path), {"ML_NIGHT_MODEL": "opus"})
    assert seen.get("bucket") == "seven_day"


def test_explicit_sonnet_model_id_uses_sonnet_bucket(monkeypatch, tmp_path):
    # The arbitrary-model passthrough can name an explicit Sonnet model (not the bare
    # keyword "sonnet"); it still launches a Sonnet session, so it must draw the Sonnet
    # weekly pool rather than being budgeted against the general seven_day bucket.
    seen = _drive_main(monkeypatch, tmp_path, _base_cfg(tmp_path),
                       {"ML_NIGHT_MODEL": "claude-sonnet-4-5-20250929"})
    assert seen.get("bucket") == "seven_day_sonnet"


def test_gate_active_bucket_name_matches_any_sonnet_model():
    import gate
    assert gate.active_bucket_name({"night_model": "sonnet"}) == "seven_day_sonnet"
    assert gate.active_bucket_name({"night_model": "claude-sonnet-4-5"}) == "seven_day_sonnet"
    assert gate.active_bucket_name({"night_model": "opus"}) == "seven_day"
    assert gate.active_bucket_name({"night_model": "default"}) == "seven_day"


def test_gate_active_bucket_name_honours_env_override(monkeypatch):
    # The gate must derive the bucket from the EFFECTIVE model — an ML_NIGHT_MODEL override
    # (cron / moonlight start / panel) that runner.main() acts on — not just cfg, or it
    # budgets scheduled/manual runs against the wrong weekly pool.
    import gate
    monkeypatch.setenv("ML_NIGHT_MODEL", "sonnet")
    assert gate.active_bucket_name({"night_model": "default"}) == "seven_day_sonnet"
    monkeypatch.setenv("ML_NIGHT_MODEL", "claude-sonnet-4-5")
    assert gate.active_bucket_name({"night_model": "default"}) == "seven_day_sonnet"
    monkeypatch.delenv("ML_NIGHT_MODEL", raising=False)
    assert gate.active_bucket_name({"night_model": "default"}) == "seven_day"


def test_refuses_observe_launch_when_weekly_cap_exhausted(monkeypatch, tmp_path):
    # Observe (dry-run) runs still start a real Claude survey session and spend quota, so the
    # pre-launch cap guard must refuse them too when the weekly bucket is already over cap.
    cfg = _base_cfg(tmp_path)
    cfg["mode"] = "observe"
    seen = _drive_main(monkeypatch, tmp_path, cfg, {},
                       usage={"five_hour": {"utilization": 5}, "seven_day": {"utilization": 95},
                              "seven_day_sonnet": {"utilization": 7}})
    assert "bucket" not in seen, "observe runs spend quota; must refuse when over the weekly cap"


def test_refuses_launch_when_weekly_cap_already_exhausted(monkeypatch, tmp_path):
    # ML_RESERVE=50 → cap 50; seven_day already at 60 → refuse before launching, don't supervise.
    seen = _drive_main(monkeypatch, tmp_path, _base_cfg(tmp_path), {"ML_RESERVE": "50"},
                       usage={"five_hour": {"utilization": 5}, "seven_day": {"utilization": 60},
                              "seven_day_sonnet": {"utilization": 7}})
    assert "bucket" not in seen, "must not launch/supervise when already over the weekly cap"
    assert seen["rc"] == 0


def test_gc_partial_purge_marks_non_revertible(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    rd = _make_run(runs, "20260101-000000", finished=_old_iso(30))
    monkeypatch.setattr(cli.state, "RUNS_DIR", runs)
    real_rmtree = cli.shutil.rmtree

    def flaky(path, *a, **k):
        if pathlib.Path(path).name == "snapshot":
            raise OSError("permission denied")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(cli.shutil, "rmtree", flaky)
    rc = cli.cmd_gc(types.SimpleNamespace(days=14, dry_run=False))
    assert rc == 1                              # a target failed
    assert not (rd / "trash").exists()          # one target really went
    assert (rd / "snapshot").exists()           # the other remained
    # Partial loss still means non-revertible — must be flagged, revert.sh gone.
    assert json.loads((rd / "run.json").read_text()).get("revert_purged") is True
    assert not (rd / "revert.sh").exists()
