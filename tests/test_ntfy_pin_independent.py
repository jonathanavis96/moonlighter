"""The ntfy bridge must keep its own PIN gate, independent of the panel endpoints.

/api/pause deliberately has no PIN check (off is fail-safe). That relaxation must
stay confined to the panel's own UI. The bridge is a separate front door reachable
by anyone who learns the private ntfy topic, so it enforces the PIN itself and
dispatches straight to the CLI rather than through the HTTP API.

This is pinned by a test because the design doc originally asserted the opposite —
that the bridge shared /api/pause, and that dropping the PIN there exposed phone
pause. It does not. If someone later "simplifies" the bridge to POST /api/pause,
that false claim becomes true and phone pause silently loses its PIN.
"""
import pathlib
import sys




LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import ntfy_bridge  # noqa: E402


def test_bridge_rejects_a_command_without_the_pin(monkeypatch):
    """A pause command lacking the PIN must be refused by the bridge itself."""
    acks = []
    dispatched = []
    monkeypatch.setattr(ntfy_bridge, "_ack", lambda cfg, msg: acks.append(msg))
    monkeypatch.setattr(
        ntfy_bridge.cli, "cmd_pause", lambda *a, **k: dispatched.append("pause")
    )

    ntfy_bridge.handle({"pin": "999999"}, "pause")

    assert dispatched == [], "bridge must not dispatch pause without the PIN"
    assert acks and "PIN" in acks[0]


def test_bridge_rejects_a_wrong_pin(monkeypatch):
    acks = []
    dispatched = []
    monkeypatch.setattr(ntfy_bridge, "_ack", lambda cfg, msg: acks.append(msg))
    monkeypatch.setattr(
        ntfy_bridge.cli, "cmd_pause", lambda *a, **k: dispatched.append("pause")
    )

    ntfy_bridge.handle({"pin": "999999"}, "pause 000000")

    assert dispatched == [], "bridge must not dispatch pause on a wrong PIN"
    assert acks and "PIN" in acks[0]


def test_bridge_dispatches_pause_to_the_cli_not_the_http_endpoint(monkeypatch):
    """With the right PIN the bridge calls cli.cmd_pause directly.

    Dispatching via the CLI is what keeps phone pause PIN-gated even though
    /api/pause is not.
    """
    dispatched = []
    monkeypatch.setattr(ntfy_bridge, "_ack", lambda cfg, msg: None)
    monkeypatch.setattr(
        ntfy_bridge.cli, "cmd_pause", lambda *a, **k: dispatched.append("pause")
    )

    ntfy_bridge.handle({"pin": "999999"}, "pause 999999")

    assert dispatched == ["pause"]


def test_bridge_source_never_calls_the_pause_endpoint():
    """Guard the boundary itself: the bridge must not route through /api/pause.

    If it ever does, the panel's PIN-free pause becomes reachable from the ntfy
    topic — the exact exposure the design doc wrongly claimed already existed.
    """
    src = pathlib.Path(ntfy_bridge.__file__).read_text(encoding="utf-8")
    assert "/api/pause" not in src, (
        "ntfy bridge must not call /api/pause — that endpoint has no PIN gate; "
        "dispatch via cli.cmd_pause() so the bridge's own PIN check applies"
    )
