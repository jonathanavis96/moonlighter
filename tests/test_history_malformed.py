"""A transcript line with a non-string timestamp (or a non-object line) must be
skipped, not abort the whole histogram build."""
import datetime
import json
import pathlib
import sys

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import history  # noqa: E402


def test_bad_timestamp_lines_are_skipped(tmp_path):
    good = datetime.datetime.now().astimezone().isoformat()
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join([
        json.dumps({"timestamp": 12345}),
        json.dumps(["timestamp"]),
        json.dumps({"timestamp": good}),
    ]) + "\n")
    cutoff = datetime.datetime.now() - datetime.timedelta(days=1)
    assert len(list(history._parse_timestamps(f, cutoff))) == 1
