"""ntfy_bridge.py — phone -> Moonlighter command listener (phone_tier: ntfy).

Long-polls the ntfy command topic and treats PIN-stamped messages exactly like
the panel buttons. No inbound port is opened on this machine. Server -> phone
pushes ("run finished") are handled separately by lib/notify.py.

Recognised commands (PIN required):
    start 5h <PIN>     start a run, away-window 5h
    start <PIN>        start a run, default away-window
    pause <PIN>        kill switch on
    resume <PIN>       kill switch off
    status <PIN>       push current gate verdict back to the phone

Run as a daemon (cron @reboot):  python3 lib/ntfy_bridge.py
"""
import json
import time
import types
import urllib.request
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as cfgmod   # noqa: E402
import state              # noqa: E402
import gate as gatemod    # noqa: E402
import cli                # noqa: E402
import notify as notifymod  # noqa: E402


def _ack(cfg, msg):
    notifymod.ntfy_push(cfg, msg, title="Moonlighter")
    state.gate_log(f"ntfy: {msg}")


def handle(cfg, message):
    parts = (message or "").strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    pin = cfg.get("pin")
    # PIN must be present as the LAST token
    if not parts or parts[-1] != str(pin):
        _ack(cfg, "✗ command ignored: missing/incorrect PIN")
        return
    if cmd == "start":
        hours = 5.0
        for tok in parts[1:-1]:
            t = tok.lower().rstrip("h")
            try:
                hours = float(t)
            except ValueError:
                pass
        args = types.SimpleNamespace(hours=hours, budget=None)
        rc = cli.cmd_start(args)
        _ack(cfg, f"▶ start away {hours:.0f}h — {'launched' if rc == 0 else 'refused (see log)'}")
    elif cmd == "pause":
        cli.cmd_pause(types.SimpleNamespace())
        _ack(cfg, "⏸ paused")
    elif cmd == "resume":
        cli.cmd_resume(types.SimpleNamespace())
        _ack(cfg, "✓ resumed")
    elif cmd == "status":
        s = gatemod.compute_status(cfg)
        _ack(cfg, f"{s['gate']['verdict']} — {s['gate']['summary']}")
    else:
        _ack(cfg, f"✗ unknown command: {cmd}")


def listen():
    cfg = cfgmod.load()
    nt = cfg.get("ntfy") or {}
    base = nt.get("base_url", "https://ntfy.sh").rstrip("/")
    topic = nt.get("command_topic")
    if not topic:
        print("No ntfy command_topic configured — nothing to listen for.")
        return
    url = f"{base}/{topic}/json?since=now"
    state.gate_log("ntfy: bridge started")
    while True:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=320) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("event") != "message":
                        continue  # skip keepalive/open events
                    handle(cfgmod.load(), obj.get("message", ""))
        except Exception as exc:
            state.gate_log(f"ntfy: reconnect after error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    listen()
