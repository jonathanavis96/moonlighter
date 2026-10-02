"""State and cache files must be replaced atomically: a failure mid-write leaves the
previous file intact and no temp file behind."""
import json
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import atomic  # noqa: E402
import cli  # noqa: E402
import history  # noqa: E402
import state  # noqa: E402
import usage_api  # noqa: E402


def _boom(*a, **k):
    raise OSError("disk died mid-write")


def _only(d, name):
    return sorted(p.name for p in d.iterdir()) == [name]


def test_atomic_helper_failure_keeps_old(tmp_path, monkeypatch):
    f = tmp_path / "x.json"
    f.write_text("old")
    monkeypatch.setattr(atomic.os, "fsync", _boom)
    with pytest.raises(OSError):
        atomic.write_text(f, "new")
    assert f.read_text() == "old" and _only(tmp_path, "x.json")


def test_atomic_helper_replaces(tmp_path):
    f = tmp_path / "x.json"
    atomic.write_text(f, "a")
    atomic.write_text(f, "b")
    assert f.read_text() == "b" and _only(tmp_path, "x.json")


def test_status_cache_atomic(tmp_path, monkeypatch):
    cache = tmp_path / "last_status.json"
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(state, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(state, "STATUS_CACHE", cache)
    cache.write_text(json.dumps({"ok": 1}))
    monkeypatch.setattr(atomic.os, "fsync", _boom)
    with pytest.raises(OSError):
        state.write_status_cache({"ok": 2})
    assert json.loads(cache.read_text()) == {"ok": 1}
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_usage_last_good_atomic(tmp_path, monkeypatch):
    f = tmp_path / "usage_last_good.json"
    f.write_text('{"ts": 1, "data": "old"}')
    monkeypatch.setattr(usage_api, "_LAST_GOOD", f)
    monkeypatch.setattr(atomic.os, "fsync", _boom)
    usage_api._save_last_good({"new": 1})  # swallows the error by design
    assert json.loads(f.read_text())["data"] == "old" and _only(tmp_path, f.name)


def test_usage_backoff_atomic(tmp_path, monkeypatch):
    f = tmp_path / "usage_backoff.json"
    f.write_text('{"ts": 1, "retry_after": 0}')
    monkeypatch.setattr(usage_api, "_ATTEMPT_FILE", f)
    monkeypatch.setattr(atomic.os, "fsync", _boom)
    usage_api._record_attempt(30.0)
    assert json.loads(f.read_text())["ts"] == 1 and _only(tmp_path, f.name)


def test_histogram_cache_atomic(tmp_path, monkeypatch):
    f = tmp_path / "histogram.json"
    f.write_text('"old"')
    monkeypatch.setattr(history, "HIST_CACHE", f)
    monkeypatch.setattr(history, "build_histogram", lambda weeks: {"new": 1})
    monkeypatch.setattr(history, "CACHE_TTL_SEC", -1)
    monkeypatch.setattr(atomic.os, "fsync", _boom)
    history.get_histogram()
    assert f.read_text() == '"old"' and _only(tmp_path, f.name)


def test_run_json_gc_mark_atomic(tmp_path, monkeypatch, capsys):
    meta_f = tmp_path / "run.json"
    meta_f.write_text('{"old": true}')
    (tmp_path / "revert.sh").write_text("x")
    monkeypatch.setattr(atomic.os, "fsync", _boom)
    cli._mark_revert_purged(tmp_path, {"old": True}, meta_f)
    assert json.loads(meta_f.read_text()) == {"old": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run.json"]
