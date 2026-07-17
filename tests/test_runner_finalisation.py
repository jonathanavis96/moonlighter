"""Behavioural regression tests: run finalisation is guaranteed.

After _supervise() returns, main()'s wrap-up does fallible bookkeeping (pane
capture, transcript write, usage read, token accounting, calibration). A
failure in any of those must NEVER prevent finalisation: revert.sh and the
morning report are the user's only handles on what the run did, so they must
ALWAYS be generated.

These tests drive runner.main() end-to-end (tmux and network stubbed out),
inject wrap-up failures, and assert revert.sh + the report still exist. They
replace the old source-inspection guard
(`test_finalize_runs_unconditionally_after_supervise`) that merely checked
main()'s source text for statement ordering.
"""
import pathlib
import sys
import types

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import runner  # noqa: E402


class Harness:
    def __init__(self, run_dir, report_dir, logs):
        self.run_dir = run_dir
        self.report_dir = report_dir
        self.logs = logs
        self.supervised = False        # set once _supervise() has returned
        self.capture_raises_after_supervise = False

    def report_files(self):
        return list(self.report_dir.glob("moonlighter-*-testrun.md"))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Stub every external effect of runner.main() (tmux, usage API, real
    state dirs, notifications) so the wrap-up path runs for real against a
    tmp run dir. revert.sh and the report are written by the REAL revert and
    report modules."""
    run_dir = tmp_path / "runs" / "testrun"
    run_dir.mkdir(parents=True)
    report_dir = tmp_path / "reports"
    logs = []
    h = Harness(run_dir, report_dir, logs)

    cfg = {
        "kill_switch_path": tmp_path / "no-kill-switch",
        "report_dir_path": report_dir,
        "mode": "observe",
        "max_wallclock_min": 1,
        "night_model": "default",
        "five_hour_target_pct": 80,
        "weekly_reserve_pct": 10,
    }
    monkeypatch.setattr(runner.cfgmod, "load", lambda: cfg)

    # state: no real ~/.moonlighter side effects
    monkeypatch.setattr(runner.state, "ensure_dirs", lambda: None)
    monkeypatch.setattr(runner.state, "new_run_dir", lambda: ("testrun", run_dir))
    monkeypatch.setattr(runner.state, "gate_log", lambda msg: logs.append(msg))
    monkeypatch.setattr(runner.state, "append_calibration", lambda rec: None)

    # no live session, no env overrides, canned mission
    monkeypatch.setattr(runner, "_session_alive", lambda: False)
    monkeypatch.setattr(runner, "_read_budget_env",
                        lambda cfg: ("seven_day", None, 1, None))
    monkeypatch.setattr(runner, "_read_mission_file_env", lambda: None)
    monkeypatch.setattr(runner.digestmod, "prior_brief", lambda: "")
    monkeypatch.setattr(runner, "build_mission", lambda *a, **k: "test mission")

    # network + token accounting stubbed
    monkeypatch.setattr(runner.usage_api, "get_usage", lambda force=False: {})
    monkeypatch.setattr(runner, "_session_transcripts", lambda since_ts: [])
    monkeypatch.setattr(runner, "_sum_tokens", lambda paths: 0)

    # tmux + timing stubbed (which() too, so the tests stay hermetic on
    # machines without tmux — runner.main() prechecks it before any of the
    # patched subprocess.run calls)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(runner, "SESSION_CWD", tmp_path / "session")
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)

    def fake_supervise(*a, **k):
        h.supervised = True
        return "session ended"
    monkeypatch.setattr(runner, "_supervise", fake_supervise)

    def fake_capture():
        if h.supervised and h.capture_raises_after_supervise:
            raise RuntimeError("tmux capture-pane exploded")
        return "> claude is ready\nfinal pane contents"
    monkeypatch.setattr(runner, "_capture", fake_capture)

    # report's notify hook must not fire real notifications
    monkeypatch.setattr(runner.reportmod.notifymod, "report_ready",
                        lambda *a, **k: False)
    return h


def _assert_finalised(h):
    revert_sh = h.run_dir / "revert.sh"
    assert revert_sh.exists(), "revert.sh must ALWAYS be generated"
    assert "Revert" in revert_sh.read_text(encoding="utf-8")
    reports = h.report_files()
    assert reports, "the run report must ALWAYS be generated"
    assert "testrun" in reports[0].read_text(encoding="utf-8")


def test_wrapup_succeeds_finalises_normally(harness):
    """Sanity baseline: with no injected failure the wrap-up writes the
    transcript AND finalises."""
    rc = runner.main()
    assert rc == 0
    assert (harness.run_dir / "transcript.txt").exists()
    _assert_finalised(harness)
    import json
    meta = json.loads((harness.run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "observed"
    assert meta["stop_reason"] == "session ended"


def test_capture_failure_still_writes_revert_and_report(harness):
    """The final _capture() raising must not skip revert.sh or the report."""
    harness.capture_raises_after_supervise = True

    rc = runner.main()

    assert rc == 0, "main() must still complete, not crash out"
    _assert_finalised(harness)
    assert not (harness.run_dir / "transcript.txt").exists()
    import json
    meta = json.loads((harness.run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "wrapup-error"
    assert "capture-pane exploded" in meta["wrapup_error"]
    assert any("finalising anyway" in m for m in harness.logs)


def test_report_failure_marks_run_failed_and_returns_nonzero(harness, monkeypatch):
    """write_report() raising must not leave a 'clean' run.json and rc 0:
    the run is marked finalisation-error, the failure is recorded, and
    main() returns non-zero — while revert.sh is still written (each
    finalisation step stays independent)."""
    def boom(*a, **k):
        raise OSError("report_dir is a regular file")
    monkeypatch.setattr(runner.reportmod, "write_report", boom)

    rc = runner.main()

    assert rc != 0, "a run missing its report must not exit clean"
    revert_sh = harness.run_dir / "revert.sh"
    assert revert_sh.exists(), "revert.sh must ALWAYS be generated"
    import json
    meta = json.loads((harness.run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "finalisation-error"
    assert any("write_report" in e for e in meta["finalisation_errors"])
    assert any("finalisation errors" in m for m in harness.logs)


def test_revert_failure_is_visible_in_the_report_it_writes(harness, monkeypatch):
    """write_revert_script() failing must be annotated in run_meta BEFORE the
    report renders — the morning report must not claim a clean, fully
    revertible run while revert.sh is missing."""
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(runner.revertmod, "write_revert_script", boom)
    seen = {}
    real_write_report = runner.reportmod.write_report
    def spying_write_report(cfg, run_dir, meta):
        seen["status_at_report_time"] = meta.get("status")
        seen["errors_at_report_time"] = list(meta.get("finalisation_errors", []))
        return real_write_report(cfg, run_dir, meta)
    monkeypatch.setattr(runner.reportmod, "write_report", spying_write_report)

    rc = runner.main()

    assert rc != 0
    assert seen["status_at_report_time"] == "finalisation-error"
    assert any("write_revert_script" in e for e in seen["errors_at_report_time"])
    reports = harness.report_files()
    assert reports, "the report itself must still be written"
    report_text = reports[0].read_text(encoding="utf-8")
    assert "NOT one-command revertible" in report_text, \
        "the report must not claim revertibility without revert.sh"
    assert "fully revertible" not in report_text
    import json
    meta = json.loads((harness.run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "finalisation-error"


def test_transcript_write_failure_still_writes_revert_and_report(harness):
    """The transcript write raising (here: IsADirectoryError, because a
    directory squats on transcript.txt) must not skip revert.sh or the
    report."""
    (harness.run_dir / "transcript.txt").mkdir()

    rc = runner.main()

    assert rc == 0
    _assert_finalised(harness)
    import json
    meta = json.loads((harness.run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "wrapup-error"
    assert any("finalising anyway" in m for m in harness.logs)
