"""The weekly-graph sampler reads the same hand-appendable usage log; a
malformed line must be skipped, not crash the panel graph."""
import json
import pathlib
import sys

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import graph  # noqa: E402

R = "2020-01-08T00:00:00+00:00"


def test_malformed_lines_are_skipped(tmp_path, monkeypatch):
    log = tmp_path / "usage_log.jsonl"
    lines = [
        {"ts": "2020-01-02T00:00:00+00:00", "seven_day": {"utilization": 10, "resets_at": R}},
        {"ts": "2020-01-03T00:00:00", "seven_day": {"utilization": 20, "resets_at": R}},  # naive
        {"ts": "2020-01-04T00:00:00+00:00", "seven_day": {"utilization": "x", "resets_at": R}},
        {"ts": "2020-01-04T00:00:00+00:00", "seven_day": [1]},
        [1, 2, 3],
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    monkeypatch.setattr(graph, "USAGE_LOG", log)
    assert [u for _, u in graph._this_week_samples(R)] == [10.0, 20.0]
