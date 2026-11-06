"""Cross-origin requests against the panel.

The panel binds to the LAN, and any web page its owner opens can make the browser
POST to it: a form or sendBeacon needs no CORS approval, and a text/plain body is
parsed as JSON here. A browser's POST always carries Origin, so the panel refuses
one that is not its own page. Runs the REAL PanelHandler on an ephemeral port.
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
ROUTES = ["/api/start", "/api/pause", "/api/resume", "/api/schedule", "/api/schedule/delete",
          "/api/approve", "/api/mode", "/api/settings", "/api/apply", "/api/answer",
          "/api/revert", "/api/setup"]


@pytest.fixture
def panel(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    cfg = dict(panelserver.CFG)
    cfg["kill_switch_path"] = tmp_path / "pause"
    cfg["pin"] = TEST_PIN
    cfg["setup_complete"] = True
    monkeypatch.setattr(panelserver, "CFG", cfg)
    monkeypatch.setattr(panelserver.state, "RUNS_DIR", runs)
    # A failing gate must not reach the live install: its state dir and its runs.
    monkeypatch.setattr(panelserver.state, "APPROVED_FLAG", tmp_path / "approved")
    monkeypatch.setattr(panelserver.cfgmod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(panelserver.cfgmod, "LOCAL_PATH", tmp_path / "config.local.yaml")
    monkeypatch.setattr(panelserver.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("a test tried to start a process"))
    httpd = http.server.HTTPServer(("127.0.0.1", 0), panelserver.PanelHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1], tmp_path
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def post(port, path, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=json.dumps(body),
                 headers={"Content-Type": "text/plain", **(headers or {})})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


@pytest.mark.parametrize("route", ROUTES)
def test_every_post_route_refuses_a_foreign_origin(panel, route):
    # A wrong PIN, so with no gate a PIN-checked handler still does nothing; the
    # error tells the gate's 403 from the PIN's.
    port, _ = panel
    status, raw = post(port, route, {"pin": "000000"}, {"Origin": "https://evil.example"})
    assert status == 403
    assert json.loads(raw)["error"] == "cross-origin request refused"


def test_forged_pause_changes_nothing(panel):
    # /api/pause takes no PIN by design, so only the Origin check stands here.
    port, tmp = panel
    post(port, "/api/pause", {}, {"Origin": "https://evil.example"})
    assert not (tmp / "pause").exists()


@pytest.mark.parametrize("origin", [
    "https://evil.example", "http://evil.example:8377", "http://127.0.0.1", "null", "http://[bad",
])
def test_origin_must_be_the_panels_own(panel, origin):
    port, tmp = panel
    status, _ = post(port, "/api/pause", {}, {"Origin": origin})
    assert status == 403
    assert not (tmp / "pause").exists()


def test_own_page_and_a_script_with_no_origin_still_work(panel):
    port, tmp = panel
    status, _ = post(port, "/api/pause", {}, {"Origin": f"http://127.0.0.1:{port}"})
    assert status == 200
    assert (tmp / "pause").exists()
    (tmp / "pause").unlink()
    status, _ = post(port, "/api/pause", {})
    assert status == 200
    assert (tmp / "pause").exists()


@pytest.mark.parametrize("origin,host,want", [
    ("http://127.0.0.1:8377", "127.0.0.1:8377", True),
    ("http://localhost:8377", "localhost:8377", True),
    ("http://[::1]:8377", "[::1]:8377", True),
    ("http://192.168.0.5:8377", "192.168.0.5:8377", True),
    ("http://100.64.0.1:8377", "100.64.0.1:8377", True),  # phone over Tailscale
    ("http://box:8377", "box:8377", True),
    ("http://box.example-tailnet.ts.net:8377", "box.example-tailnet.ts.net:8377", True),
    ("http://box.local:8377", "box.local:8377", True),
    ("http://BOX:8377", "box:8377", True),
    ("http://box:8378", "box:8377", False),  # another port is another site
    ("https://evil.example", "box:8377", False),
    ("http://evil.example:8377", "evil.example:8377", False),        # DNS rebinding
    ("http://127.0.0.1.evil.example:8377", "127.0.0.1.evil.example:8377", False),
    ("null", "box:8377", False),
    ("", "box:8377", False),
])
def test_same_origin(origin, host, want):
    assert panelserver._same_origin(origin, host) is want


# ---- /api/answer: the channel a run's agent reads, so it takes the PIN too ----

def test_answer_needs_the_pin(panel):
    port, tmp = panel
    status, _ = post(port, "/api/answer", {"run": "r1", "answer": "rm -rf"})
    assert status == 403
    assert not (tmp / "runs" / "r1" / "answer.txt").exists()
    status, _ = post(port, "/api/answer", {"run": "r1", "answer": "rm -rf", "pin": "000000"})
    assert status == 403
    assert not (tmp / "runs" / "r1" / "answer.txt").exists()


def test_answer_with_the_pin_reaches_the_agent(panel):
    port, tmp = panel
    status, _ = post(port, "/api/answer", {"run": "r1", "answer": "yes", "pin": TEST_PIN})
    assert status == 200
    assert (tmp / "runs" / "r1" / "answer.txt").read_text() == "yes"


def test_the_answer_box_sends_the_pin():
    src = (PANEL / "server.py").read_text()
    start = src.index("el('ask-send')")
    handler = src[start:src.index("// Mobile start", start)]
    assert "pinPrompt()" in handler
    assert "answer, pin" in handler
