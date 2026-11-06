"""The generated revert.sh runs real commands over file names and manifest text an
agent wrote, so nothing from the manifest may be read by the shell as code.

Each test runs the script in bash against real files under tmp_path."""
import json
import pathlib
import shlex
import subprocess
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


def write_manifest(run_dir, *recs):
    (run_dir / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))


def run_script(run_dir):
    script = revert.write_revert_script(run_dir)
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=20)


NAMES = ["cost $HOME.txt", "price $NOPE.txt", 'quo"te.txt', "back`touch PWNED`tick.txt",
         "$(touch PWNED).txt", "it's.txt", "sp ace.txt"]


@pytest.mark.parametrize("name", NAMES)
def test_created_file_with_a_shell_name(run_dir, tmp_path, name):
    f = tmp_path / name
    f.write_text("x")
    write_manifest(run_dir, {"op": "created", "path": str(f)})
    r = subprocess.run(["bash", str(revert.write_revert_script(run_dir))],
                       capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not f.exists()
    assert not (tmp_path / "PWNED").exists()
    assert f"removed created {f}" in r.stdout    # the name as it is, unexpanded
    assert "Revert complete." in r.stdout


@pytest.mark.parametrize("name", NAMES)
def test_move_with_a_shell_name(run_dir, tmp_path, name):
    src, dst = tmp_path / ("orig " + name), tmp_path / name
    dst.write_text("x")
    write_manifest(run_dir, {"op": "move", "src": str(src), "dst": str(dst)})
    r = subprocess.run(["bash", str(revert.write_revert_script(run_dir))],
                       capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert src.exists() and not dst.exists()
    assert not (tmp_path / "PWNED").exists()
    assert "Revert complete." in r.stdout


@pytest.mark.parametrize("name", NAMES)
def test_trash_with_a_shell_name(run_dir, tmp_path, name):
    p, tr = tmp_path / name, tmp_path / ("trash " + name)
    tr.write_text("x")
    write_manifest(run_dir, {"op": "trash", "path": str(p), "trash": str(tr)})
    r = subprocess.run(["bash", str(revert.write_revert_script(run_dir))],
                       capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert p.exists() and not tr.exists()
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize("name", NAMES)
def test_snapshot_with_a_shell_name(run_dir, tmp_path, name):
    p = tmp_path / name
    p.write_text("changed")
    snap = revert._snap_path(run_dir, str(p))
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text("original")
    write_manifest(run_dir, {"op": "snapshot", "path": str(p)})
    r = subprocess.run(["bash", str(revert.write_revert_script(run_dir))],
                       capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert p.read_text() == "original"
    assert not (tmp_path / "PWNED").exists()


def test_a_set_u_failure_does_not_stop_the_revert_halfway(run_dir, tmp_path):
    """The sweep's case: `$NOPE` unset under `set -u` ended the script, so the
    records after it (older operations) were never reverted."""
    older, newer = tmp_path / "older.txt", tmp_path / "price $NOPE.txt"
    older.write_text("x")
    newer.write_text("x")
    write_manifest(run_dir, {"op": "created", "path": str(older)},
                   {"op": "created", "path": str(newer)})
    r = run_script(run_dir)
    assert not newer.exists()
    assert not older.exists(), r.stderr
    assert "Revert complete." in r.stdout


# ---- "Revert: chmod ..." notes ----

@pytest.fixture
def victim(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    f.chmod(0o600)
    return f


def modes(path):
    return oct(path.stat().st_mode & 0o777)


@pytest.mark.parametrize("cmd", [
    "chmod 644 {f}; touch {pwn}",
    "chmod 644 {f} && touch {pwn}",
    "chmod 644 {f} | touch {pwn}",
    "chmod 644 {f}\ntouch {pwn}",
    "chmod 644 $(touch {pwn}){f}",
    "chmod 644 `touch {pwn}`",
    "chmod $(touch {pwn}) {f}",
    "chmod 644 {f} > {pwn}",
])
def test_a_note_cannot_run_anything_but_chmod(run_dir, tmp_path, victim, cmd):
    pwn = tmp_path / "PWNED"
    write_manifest(run_dir, {"op": "note", "text": "Fixed. Revert: " + cmd.format(f=victim, pwn=pwn)})
    r = run_script(run_dir)
    assert not pwn.exists()
    assert "Revert complete." in r.stdout
    ok, _msg = revert._revert_one(run_dir, json.loads((run_dir / "manifest.jsonl").read_text()))
    assert not pwn.exists()
    assert ok is False


@pytest.mark.parametrize("mode", ["644", "0644", "u+rw,go+r", "a=r"])
def test_a_plain_chmod_note_still_replays_in_both_paths(run_dir, tmp_path, victim, mode):
    rec = {"op": "note", "text": f"Tightened it. Revert: chmod {mode} {victim}"}
    want = {"644": "0o644", "0644": "0o644", "u+rw,go+r": "0o644", "a=r": "0o444"}[mode]
    write_manifest(run_dir, rec)
    r = run_script(run_dir)
    assert r.returncode == 0, r.stderr
    assert modes(victim) == want
    victim.chmod(0o600)
    ok, msg = revert._revert_one(run_dir, rec)
    assert ok, msg
    assert modes(victim) == want


def test_a_note_with_a_shell_name_chmods_that_file(run_dir, tmp_path):
    f = tmp_path / "cost $HOME's.txt"
    f.write_text("x")
    f.chmod(0o600)
    rec = {"op": "note", "text": f"Revert: chmod 644 {shlex.quote(str(f))}"}
    write_manifest(run_dir, rec)
    r = run_script(run_dir)
    assert r.returncode == 0, r.stderr
    assert modes(f) == "0o644"
    assert "ran: chmod 644" in r.stdout
    assert "cost $HOME" in r.stdout    # unexpanded


def test_a_home_path_in_a_note_is_expanded(run_dir, tmp_path, monkeypatch, victim):
    monkeypatch.setenv("HOME", str(tmp_path))
    rec = {"op": "note", "text": "Revert: chmod 644 ~/file.txt"}
    ok, msg = revert._revert_one(run_dir, rec)
    assert ok, msg
    assert modes(victim) == "0o644"


@pytest.mark.parametrize("text", [
    "Revert: chmod 775 cortex.bash; chmod 644 layout.sql arena_ask.bak",   # real note
    "Revert: chmod o+w on each of the 17 paths listed in this run's findings.",  # real note
    "Revert: chmod 644.",                                                  # real note
    "Revert: chmod 644 relative.txt",
    "Revert: chmod -R 777 /tmp/x",
    "Revert: chmod --reference=/etc/passwd /tmp/x",
    "Revert: chmod 644 /tmp/unbalanced'quote",
])
def test_a_note_that_is_not_one_plain_chmod_is_refused_out_loud(run_dir, text):
    write_manifest(run_dir, {"op": "note", "text": text})
    script = revert.build_revert_script(run_dir)
    assert "NOT replayed" in script
    assert "\nchmod" not in script
    ok, msg = revert._revert_one(run_dir, {"op": "note", "text": text})
    assert ok is False
    assert "NOT replayed" in msg


@pytest.mark.parametrize("path", ["~/.ssh/id_rsa", "~/.claude/.credentials.json"])
def test_an_off_limits_chmod_note_is_refused_in_both_paths(run_dir, monkeypatch, path):
    calls = []
    monkeypatch.setattr(revert.subprocess, "call", lambda *a, **k: calls.append(a) or 0)
    rec = {"op": "note", "text": f"Revert: chmod 644 {path}"}
    write_manifest(run_dir, rec)
    script = revert.build_revert_script(run_dir)
    assert "NOT replayed" in script
    assert "\nchmod" not in script
    ok, msg = revert._revert_one(run_dir, rec)
    assert ok is False
    assert "off-limits" in msg
    assert calls == []


def test_a_failed_chmod_is_a_failure_not_a_success(run_dir, tmp_path):
    ok, msg = revert._revert_one(run_dir, {
        "op": "note", "text": f"Revert: chmod 644 {tmp_path}/no-such-file"})
    assert ok is False


def test_a_note_with_no_chmod_is_still_nothing_to_revert(run_dir):
    ok, msg = revert._revert_one(run_dir, {"op": "note", "text": "Revert: restore from snapshot."})
    assert ok
    assert msg == "note (nothing to revert)"
