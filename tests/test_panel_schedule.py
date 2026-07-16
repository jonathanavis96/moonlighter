"""Regression tests for the panel's /api/schedule and /api/schedule/delete endpoints.

Both are PIN-gated (scheduling spends quota later, same rationale as Start now).
Spins up the REAL PanelHandler on an ephemeral 127.0.0.1 port in a background
thread — same pattern as test_panel_pause_resume.py, never the live install
(PID from `config.ui_port`, currently 8377) and never a hand-rolled stub of the
HTTP layer, so this exercises the actual routing, JSON body handling, PIN
comparison and validation in panel/server.py.

The schedule store is redirected into tmp_path (monkeypatching schedule._path,
same isolation `test_gate_schedule.py` uses) so nothing here touches the real
~/.moonlighter/scheduled.json.
"""
import datetime
import http.client
import http.server
import json
import pathlib
import sys
import threading
import types

import pytest

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda *a, **k: {}, safe_dump=lambda *a, **k: ""))

PANEL = pathlib.Path(__file__).resolve().parents[1] / "panel"
LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(PANEL))
import server as panelserver  # noqa: E402
import schedule as schedulemod  # noqa: E402

TEST_PIN = "778899"


@pytest.fixture
def running_panel(tmp_path, monkeypatch):
    """A real HTTP server backed by PanelHandler, with CFG swapped to an isolated
    kill-switch path + test PIN + no off-limits roots, and the schedule store
    redirected into tmp_path. Torn down after the test."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    test_cfg = dict(panelserver.CFG)
    test_cfg["kill_switch_path"] = tmp_path / "pause"
    test_cfg["pin"] = TEST_PIN
    test_cfg["off_limits_resolved"] = []
    test_cfg["work_roots_resolved"] = [str(work_dir)]
    monkeypatch.setattr(panelserver, "CFG", test_cfg)

    sched_path = tmp_path / "scheduled.json"
    monkeypatch.setattr(schedulemod, "_path", lambda: sched_path)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), panelserver.PanelHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port, work_dir
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def _post(port, path, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _future_iso(hours=2):
    return (datetime.datetime.now().astimezone()
            + datetime.timedelta(hours=hours)).isoformat()


def _past_iso(hours=2):
    return (datetime.datetime.now().astimezone()
            - datetime.timedelta(hours=hours)).isoformat()


def test_schedule_create_403s_without_pin(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir), "run_at": _future_iso(),
    })

    assert status == 403
    assert data["ok"] is False
    assert schedulemod.load() == []


def test_schedule_create_rejects_past_run_at(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir), "run_at": _past_iso(),
        "pin": TEST_PIN,
    })

    assert status == 400
    assert data["ok"] is False
    assert "future" in data["error"].lower()
    assert schedulemod.load() == []


def test_schedule_create_rejects_empty_prompt(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "   ", "folder": str(work_dir), "run_at": _future_iso(),
        "pin": TEST_PIN,
    })

    assert status == 400
    assert data["ok"] is False
    assert "prompt" in data["error"].lower()


def test_schedule_create_rejects_five_target_zero(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir), "run_at": _future_iso(),
        "pin": TEST_PIN, "five_target": 0,
    })

    assert status == 400
    assert data["ok"] is False


def test_schedule_create_rejects_five_target_over_100(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir), "run_at": _future_iso(),
        "pin": TEST_PIN, "five_target": 101,
    })

    assert status == 400
    assert data["ok"] is False


def test_schedule_create_rejects_nonexistent_folder(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir / "does-not-exist"),
        "run_at": _future_iso(), "pin": TEST_PIN,
    })

    assert status == 400
    assert data["ok"] is False
    assert "folder" in data["error"].lower()
    assert schedulemod.load() == []


def test_schedule_create_rejects_folder_outside_work_roots(running_panel, tmp_path):
    port, _work_dir = running_panel
    outside = tmp_path / "outside"
    outside.mkdir()

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(outside),
        "run_at": _future_iso(), "pin": TEST_PIN,
    })

    assert status == 400
    assert data["ok"] is False
    assert "work roots" in data["error"].lower()
    assert schedulemod.load() == []


def test_schedule_create_rejects_off_limits_document(running_panel):
    port, work_dir = running_panel
    secret_dir = work_dir / "secret"
    secret_dir.mkdir()
    secret_doc = secret_dir / "brief.txt"
    secret_doc.write_text("do not read me", encoding="utf-8")
    panelserver.CFG["off_limits_resolved"] = [str(secret_dir)]

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir), "run_at": _future_iso(),
        "docs": [str(secret_doc)], "pin": TEST_PIN,
    })

    assert status == 400
    assert data["ok"] is False
    assert "off-limits" in data["error"].lower()
    assert schedulemod.load() == []

def test_schedule_create_success_persists_pending_task(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir), "run_at": _future_iso(),
        "docs": [], "pin": TEST_PIN,
    })

    assert status == 200
    assert data["ok"] is True

    tasks = schedulemod.load()
    assert len(tasks) == 1
    assert tasks[0]["status"] == schedulemod.STATUS_PENDING
    assert tasks[0]["prompt"] == "do the thing"
    assert tasks[0]["folder"] == str(work_dir)


def test_schedule_delete_cancels_a_pending_task(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir), "run_at": _future_iso(),
        "pin": TEST_PIN,
    })
    assert status == 200
    task_id = data["task"]["id"]

    status, data = _post(port, "/api/schedule/delete", {"id": task_id, "pin": TEST_PIN})

    assert status == 200
    assert data["ok"] is True
    task = schedulemod.get(task_id)
    assert task["status"] == schedulemod.STATUS_CANCELLED


def test_schedule_delete_403s_without_pin_and_leaves_task_pending(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule", {
        "prompt": "do the thing", "folder": str(work_dir), "run_at": _future_iso(),
        "pin": TEST_PIN,
    })
    task_id = data["task"]["id"]

    status, data = _post(port, "/api/schedule/delete", {"id": task_id})

    assert status == 403
    assert data["ok"] is False
    task = schedulemod.get(task_id)
    assert task["status"] == schedulemod.STATUS_PENDING


def test_schedule_delete_rejects_unknown_id(running_panel):
    port, work_dir = running_panel

    status, data = _post(port, "/api/schedule/delete",
                          {"id": "does-not-exist", "pin": TEST_PIN})

    assert status == 400
    assert data["ok"] is False
