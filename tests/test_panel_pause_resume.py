"""Regression tests for the panel's /api/pause and /api/resume PIN behaviour.

Deliberate asymmetry: /api/pause drops its PIN check (off is fail-safe, and the
worst case of an unwanted pause is that no run happens). /api/resume keeps its
PIN check (on spends quota, stays gated).

Scope of the relaxation: the panel's own UI is the only caller of these endpoints.
lib/ntfy_bridge.py enforces its own PIN independently (rejecting any message whose
last token isn't the PIN) and dispatches by calling cli.cmd_pause() directly, so
phone `pause <PIN>` is unaffected by the endpoint's PIN check either way.

Spins up the REAL PanelHandler on an ephemeral 127.0.0.1 port in a background
thread — never the live install (PID from `config.ui_port`, currently 8377) and
never a hand-rolled stub of the HTTP layer, so this exercises the actual routing,
JSON body handling and PIN comparison in panel/server.py.
"""
import http.client
import http.server
import json
import pathlib
import sys
import threading

import pytest

PANEL = pathlib.Path(__file__).resolve().parents[1] / "panel"
sys.path.insert(0, str(PANEL))
import server as panelserver  # noqa: E402

TEST_PIN = "445566"


@pytest.fixture
def running_panel(tmp_path, monkeypatch):
    """A real HTTP server backed by PanelHandler, with CFG swapped to an isolated
    kill-switch path + a test-only PIN, torn down after the test."""
    test_cfg = dict(panelserver.CFG)
    test_cfg["kill_switch_path"] = tmp_path / "pause"
    test_cfg["pin"] = TEST_PIN
    monkeypatch.setattr(panelserver, "CFG", test_cfg)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), panelserver.PanelHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port, test_cfg["kill_switch_path"]
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


def test_pause_succeeds_with_no_pin(running_panel):
    port, kill_path = running_panel
    assert not kill_path.exists()

    status, data = _post(port, "/api/pause", {})

    assert status == 200
    assert data["ok"] is True
    assert kill_path.exists(), "pause must write the kill-switch file even with no pin"


def test_pause_ignores_a_wrong_pin_too(running_panel):
    """Pause has no PIN gate at all now — a wrong pin in the body must not matter."""
    port, kill_path = running_panel

    status, data = _post(port, "/api/pause", {"pin": "000000"})

    assert status == 200
    assert data["ok"] is True
    assert kill_path.exists()


def test_resume_403s_on_bad_pin_and_leaves_kill_switch_present(running_panel):
    port, kill_path = running_panel
    kill_path.parent.mkdir(parents=True, exist_ok=True)
    kill_path.write_text("x", encoding="utf-8")

    status, data = _post(port, "/api/resume", {"pin": "000000"})

    assert status == 403
    assert data["ok"] is False
    assert kill_path.exists(), "a bad pin must not resume"


def test_resume_succeeds_on_correct_pin_and_removes_kill_switch(running_panel):
    port, kill_path = running_panel
    kill_path.parent.mkdir(parents=True, exist_ok=True)
    kill_path.write_text("x", encoding="utf-8")

    status, data = _post(port, "/api/resume", {"pin": TEST_PIN})

    assert status == 200
    assert data["ok"] is True
    assert not kill_path.exists()


def test_resume_with_no_pin_is_rejected(running_panel):
    port, kill_path = running_panel
    kill_path.parent.mkdir(parents=True, exist_ok=True)
    kill_path.write_text("x", encoding="utf-8")

    status, data = _post(port, "/api/resume", {})

    assert status == 403
    assert kill_path.exists()
