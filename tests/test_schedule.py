"""Tests for lib/schedule.py — the one-off scheduled-task queue.

Everything here is read from cron (`lib/gate.py`'s */30 tick), so `load()` must
never raise: a missing or corrupt file behaves as an empty list. These tests
redirect `schedule._path()` into tmp_path via monkeypatch, so nothing here
touches the real `~/.moonlighter/scheduled.json`.
"""
import datetime
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import schedule  # noqa: E402


@pytest.fixture
def sched_path(tmp_path, monkeypatch):
    p = tmp_path / "scheduled.json"
    monkeypatch.setattr(schedule, "_path", lambda: p)
    return p


def _task(run_at, status=schedule.STATUS_PENDING, **extra):
    t = {
        "id": schedule.new_id(run_at),
        "created": "2026-07-16T21:40:00+02:00",
        "run_at": run_at.isoformat(),
        "prompt": "Fix security patching",
        "folder": "/home/user/code/example",
        "docs": ["/home/user/notes/brief.md"],
        "wallclock_min": 300,
        "five_target": 80,
        "status": status,
        "fired_at": None,
        "run_id": None,
        "note": None,
    }
    t.update(extra)
    return t


def test_save_load_round_trip(sched_path):
    now = datetime.datetime.now().astimezone()
    task = _task(now)
    schedule.save([task])

    loaded = schedule.load()

    assert loaded == [task]


def test_load_missing_file_returns_empty_list(sched_path):
    assert not sched_path.exists()
    assert schedule.load() == []


def test_load_corrupt_file_returns_empty_list_and_does_not_raise(sched_path):
    sched_path.parent.mkdir(parents=True, exist_ok=True)
    sched_path.write_text("not json{", encoding="utf-8")

    assert schedule.load() == []


def test_due_returns_only_past_dated_pending_tasks_oldest_first(sched_path):
    now = datetime.datetime.now().astimezone()
    older = _task(now - datetime.timedelta(hours=2))
    newer_past = _task(now - datetime.timedelta(minutes=5))
    future = _task(now + datetime.timedelta(hours=1))
    fired = _task(now - datetime.timedelta(hours=3), status=schedule.STATUS_FIRED)
    schedule.save([newer_past, older, future, fired])

    result = schedule.due(now=now)

    assert result == [older, newer_past]


def test_due_skips_unparseable_run_at(sched_path):
    now = datetime.datetime.now().astimezone()
    bad = _task(now - datetime.timedelta(hours=1))
    bad["run_at"] = "not-a-date"
    good = _task(now - datetime.timedelta(minutes=1))
    schedule.save([bad, good])

    result = schedule.due(now=now)

    assert result == [good]


def test_pending_returns_only_future_tasks(sched_path):
    now = datetime.datetime.now().astimezone()
    past = _task(now - datetime.timedelta(minutes=5))
    future = _task(now + datetime.timedelta(hours=1))
    cancelled_future = _task(now + datetime.timedelta(hours=2), status=schedule.STATUS_CANCELLED)
    schedule.save([past, future, cancelled_future])

    result = schedule.pending(now=now)

    assert result == [future]


def test_cancel_works_on_pending_task(sched_path):
    now = datetime.datetime.now().astimezone()
    task = _task(now + datetime.timedelta(hours=1))
    schedule.save([task])

    result = schedule.cancel(task["id"])

    assert result["status"] == schedule.STATUS_CANCELLED
    assert schedule.get(task["id"])["status"] == schedule.STATUS_CANCELLED


def test_cancel_returns_none_for_already_fired_task(sched_path):
    now = datetime.datetime.now().astimezone()
    task = _task(now - datetime.timedelta(hours=1), status=schedule.STATUS_FIRED)
    schedule.save([task])

    result = schedule.cancel(task["id"])

    assert result is None
    assert schedule.get(task["id"])["status"] == schedule.STATUS_FIRED


def test_build_mission_contains_prompt_folder_and_doc_paths():
    task = {
        "prompt": "Work through this repo and fix security patching",
        "folder": "/home/user/code/example",
        "docs": ["/home/user/notes/brief.md", "/home/user/notes/second.md"],
        "wallclock_min": 300,
        "five_target": 80,
    }

    mission = schedule.build_mission(task)

    assert "Work through this repo and fix security patching" in mission
    assert "/home/user/code/example" in mission
    assert "/home/user/notes/brief.md" in mission
    assert "/home/user/notes/second.md" in mission


def test_build_mission_keeps_full_auto_changes_inside_work_root():
    task = {
        "prompt": "Tidy this and also rewrite /tmp/other-project.",
        "folder": "/tmp/allowed-project",
        "docs": [],
    }

    mission = schedule.build_mission(task)

    assert "Stay inside this Work root" in mission
    assert "anything elsewhere is audit-only" in mission
    assert "unless the task" not in mission
