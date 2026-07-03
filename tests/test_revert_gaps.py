"""Regression tests for whole-run revert gaps (audit findings #2 and #5).

#2: build_revert_script silently drops `note`-recorded chmod reverts, so
    `moonlight revert <id>` leaves permission changes in place.
#5/#3: a torn/corrupt manifest line is silently dropped, under-reverting with
    no warning — the revert must at least announce it is INCOMPLETE.
"""
import json
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import revert  # noqa: E402


@pytest.fixture
def run_dir(tmp_path):
    rd = tmp_path / "run"
    (rd / "snapshot").mkdir(parents=True)
    return rd


def test_revert_script_replays_chmod_note(run_dir):
    manifest = run_dir / "manifest.jsonl"
    manifest.write_text(
        json.dumps({"op": "note", "text": "Revert: chmod 644 /tmp/some_file"}) + "\n"
    )
    script = revert.build_revert_script(run_dir)
    assert "chmod 644 /tmp/some_file" in script, (
        "whole-run revert must replay note-recorded chmod reverts"
    )


def test_torn_manifest_line_makes_revert_announce_incomplete(run_dir, capsys):
    manifest = run_dir / "manifest.jsonl"
    good = json.dumps({"op": "created", "path": "/tmp/x"})
    torn = '{"op": "trash", "path": "/tmp/import'  # truncated / unparseable
    manifest.write_text(good + "\n" + torn + "\n")

    script = revert.build_revert_script(run_dir)

    # The generated revert must loudly flag that it is incomplete...
    assert "INCOMPLETE" in script
    # ...and reading the manifest must warn on stderr, not swallow silently.
    err = capsys.readouterr().err
    assert "unparseable" in err or "torn" in err.lower()
