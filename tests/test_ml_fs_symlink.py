"""Regression tests for ml_fs symlink handling (audit finding #1, CRITICAL).

ml_fs must act on the LITERAL path the session names — never resolve a symlink
and mutate the file it points at. Reproduces the two data-loss cases the audit
demonstrated: renaming a symlink renamed its target instead; trashing one of two
symlinks that shared a target deleted the shared file.
"""
import os
import pathlib
import sys

import pytest

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import ml_fs  # noqa: E402


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    rd = tmp_path / "run"
    (rd / "snapshot").mkdir(parents=True)
    (rd / "trash").mkdir(parents=True)
    monkeypatch.setenv("ML_RUN_DIR", str(rd))
    return rd


def test_move_symlink_moves_the_link_not_its_target(run_dir, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    target = work / "real_target.txt"
    target.write_text("precious data")
    link = work / "the_link.txt"
    link.symlink_to(target)

    dst = work / "renamed_link.txt"
    ml_fs.do_move(str(link), str(dst))

    # The real file must be untouched where it was, with its content intact.
    assert target.exists(), "moving the symlink must NOT move/rename its target"
    assert target.read_text() == "precious data"
    # The symlink itself moved to the new name.
    assert dst.is_symlink()
    assert not os.path.lexists(link)


def test_trash_symlink_preserves_a_shared_target(run_dir, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    shared = work / "shared.txt"
    shared.write_text("everyone needs this")
    link_a = work / "link_a.txt"
    link_b = work / "link_b.txt"
    link_a.symlink_to(shared)
    link_b.symlink_to(shared)

    ml_fs.do_trash(str(link_b))

    # Trashing one symlink must not destroy the shared real file.
    assert shared.exists(), "trashing a symlink must not delete its target"
    assert shared.read_text() == "everyone needs this"
    # link_a still resolves; link_b (the one we trashed) is gone from its spot.
    assert link_a.is_symlink() and link_a.resolve() == shared.resolve()
    assert not os.path.lexists(link_b)


def test_snapshot_symlink_does_not_copy_through_to_target(run_dir, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    target = work / "data.txt"
    target.write_text("target content")
    link = work / "link.txt"
    link.symlink_to(target)

    ml_fs.do_snapshot(str(link))

    snap = run_dir / "snapshot" / str(link).lstrip("/")
    # The snapshot of a symlink is stored AS a symlink, not the target's bytes.
    assert os.path.islink(snap), "symlink snapshot must not dereference the target"
    # Manifest records the link target rather than a sha of the (followed) file.
    manifest = (run_dir / "manifest.jsonl").read_text()
    assert "symlink_target" in manifest
