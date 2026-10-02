"""weekly_end_pcts reads a hand-appendable JSONL log; a malformed or naive
timestamp line must be skipped, never crash the gate."""
import json
import pathlib
import sys

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import state  # noqa: E402


def test_naive_and_malformed_resets_are_skipped(tmp_path, monkeypatch):
    log = tmp_path / "usage_log.jsonl"
    lines = [
        {"seven_day": {"utilization": 40, "resets_at": "2020-01-01T00:00:00+00:00"}},
        {"seven_day": {"utilization": 50, "resets_at": "2020-01-08T00:00:00"}},  # naive
        {"seven_day": {"utilization": 60, "resets_at": 12345}},                  # not a string
        {"seven_day": {"utilization": "x", "resets_at": "2020-01-01T00:00:00+00:00"}},
        [1, 2, 3],                                                                # not a dict
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    monkeypatch.setattr(state, "USAGE_LOG", log)
    out = state.weekly_end_pcts()
    assert 40.0 in out and 50.0 in out
