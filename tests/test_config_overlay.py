"""Mode and limits saved from the panel or the CLI must take effect. The wizard's
~/.moonlighter/config.local.yaml is merged OVER config.yaml, so a change written to
config.yaml alone was overridden: 'observe' did not stop a full-auto run. Runs the
REAL PanelHandler; every path it writes is redirected to tmp_path."""
import http.client
import http.server
import json
import pathlib
import sys
import threading
import types

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "panel"))
sys.path.insert(0, str(ROOT / "lib"))
import server as panelserver  # noqa: E402
import cli  # noqa: E402
import config as cfgmod  # noqa: E402
import state  # noqa: E402

TEST_PIN = "445566"


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """A wizard-written overlay that says full-auto, as a real install's does."""
    local = tmp_path / "config.local.yaml"
    local.write_text(yaml.safe_dump({
        "mode": "full-auto", "five_hour_target_pct": 70, "weekly_reserve_pct": 20,
        "pin": TEST_PIN, "setup_complete": True}))
    monkeypatch.setattr(cfgmod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "LOCAL_PATH", local)
    monkeypatch.setattr(state, "APPROVED_FLAG", tmp_path / "approved")
    monkeypatch.setattr(panelserver.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("a test tried to start a process"))
    return local


@pytest.fixture
def panel(overlay, monkeypatch):
    cfg = dict(panelserver.CFG)
    cfg["pin"] = TEST_PIN
    cfg["mode"] = "full-auto"
    monkeypatch.setattr(panelserver, "CFG", cfg)
    httpd = http.server.HTTPServer(("127.0.0.1", 0), panelserver.PanelHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def post(port, path, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=json.dumps(body))
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def test_panel_observe_beats_a_full_auto_overlay(panel):
    before = cfgmod.CONFIG_PATH.read_bytes()
    status, data = post(panel, "/api/mode", {"mode": "observe", "pin": TEST_PIN})
    assert (status, data["mode"]) == (200, "observe")
    assert cfgmod.load()["mode"] == "observe"            # what the gate and runner read
    assert panelserver.CFG["mode"] == "observe"
    assert cfgmod.CONFIG_PATH.read_bytes() == before     # the template stays clean


def test_panel_full_auto_sets_the_approved_flag(panel):
    post(panel, "/api/mode", {"mode": "observe", "pin": TEST_PIN})
    post(panel, "/api/mode", {"mode": "full-auto", "pin": TEST_PIN})
    assert cfgmod.load()["mode"] == "full-auto"
    assert state.APPROVED_FLAG.exists()


def test_panel_mode_keeps_the_rest_of_the_overlay(panel, overlay):
    post(panel, "/api/mode", {"mode": "observe", "pin": TEST_PIN})
    saved = yaml.safe_load(overlay.read_text())
    assert saved["pin"] == TEST_PIN
    assert saved["setup_complete"] is True


def test_panel_settings_beat_the_overlay(panel):
    before = cfgmod.CONFIG_PATH.read_bytes()
    status, data = post(panel, "/api/settings",
                        {"five_leave": 40, "weekly_reserve": 35, "pin": TEST_PIN})
    assert status == 200
    assert data["five_target"] == 60
    loaded = cfgmod.load()
    assert loaded["five_hour_target_pct"] == 60
    assert loaded["weekly_reserve_pct"] == 35
    assert cfgmod.CONFIG_PATH.read_bytes() == before


def test_panel_approve_reaches_the_overlay(panel, overlay, monkeypatch):
    cfgmod.save_local({"mode": "observe"})
    monkeypatch.setattr(panelserver.state, "list_runs", lambda n: [{"dry_run": True}])
    status, _ = post(panel, "/api/approve", {"pin": TEST_PIN})
    assert status == 200
    assert cfgmod.load()["mode"] == "full-auto"
    assert panelserver.CFG["mode"] == "full-auto"


def test_cli_mode_beats_a_full_auto_overlay(overlay):
    before = cfgmod.CONFIG_PATH.read_bytes()
    cli.cmd_mode(types.SimpleNamespace(mode="review"))
    assert cfgmod.load()["mode"] == "observe"
    assert cfgmod.CONFIG_PATH.read_bytes() == before


def test_cli_full_auto_sets_the_approved_flag(overlay):
    cli.cmd_mode(types.SimpleNamespace(mode="observe"))
    cli.cmd_mode(types.SimpleNamespace(mode="full-auto"))
    assert cfgmod.load()["mode"] == "full-auto"
    assert state.APPROVED_FLAG.exists()


def test_cli_approve_reaches_the_overlay(overlay, monkeypatch):
    cfgmod.save_local({"mode": "observe"})
    monkeypatch.setattr(cli.state, "list_runs", lambda *a: [{"dry_run": True}])
    before = cfgmod.CONFIG_PATH.read_bytes()
    assert cli.cmd_approve(types.SimpleNamespace(force=False)) == 0
    assert cfgmod.load()["mode"] == "full-auto"
    assert cfgmod.CONFIG_PATH.read_bytes() == before
