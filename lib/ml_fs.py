"""ml_fs.py — the ONLY sanctioned way the night session mutates the filesystem.

Every mutating action snapshots + logs to the run manifest BEFORE it happens, so
the run is byte-for-byte revertible. The mission's Hard Rules forbid the session
from using mv/rm/direct writes outside the run dir — all of it routes here.

Usage (run by the night session):
    python3 ml_fs.py snapshot <path>           # copy file into snapshot/ + log
    python3 ml_fs.py move <src> <dst>          # snapshot src, move, log
    python3 ml_fs.py trash <path>              # "delete" = move into run trash/
    python3 ml_fs.py write-begin <path>        # snapshot before an in-place edit
    python3 ml_fs.py created <path>            # declare a newly-created file
    python3 ml_fs.py note "<text>"             # log a non-fs action / decision

The run dir is taken from $ML_RUN_DIR.
"""
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import sys

# off-limits guard (defence in depth — the mission also forbids these)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import config as cfgmod  # noqa: E402


def _run_dir():
    rd = os.environ.get("ML_RUN_DIR")
    if not rd:
        print("ERROR: ML_RUN_DIR not set", file=sys.stderr)
        sys.exit(2)
    return pathlib.Path(rd)


def _manifest_append(rec):
    rd = _run_dir()
    rec["ts"] = datetime.datetime.now().isoformat()
    with open(rd / "manifest.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _sha(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _abspath(path):
    """Absolute + normalized path WITHOUT following symlinks.

    Unlike ``Path.resolve()`` (which dereferences symlinks to their target),
    this keeps the literal path the session named. ml_fs must act on that path
    itself — moving/trashing/snapshotting a symlink must affect the symlink,
    never the file it points at (audit finding #1, a real data-loss vector).
    """
    return pathlib.Path(os.path.abspath(path))


def _snap_dest(rd, path):
    """Mirror the absolute path under snapshot/ to avoid name collisions."""
    p = _abspath(path)
    rel = str(p).lstrip("/")
    dest = rd / "snapshot" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _guard(path):
    if cfgmod.is_off_limits(path):
        print(f"REFUSED: {path} is off-limits (secrets/credentials).", file=sys.stderr)
        sys.exit(3)


def do_snapshot(path):
    _guard(path)
    rd = _run_dir()
    p = _abspath(path)
    if not os.path.lexists(p):  # lexists: true even for a dangling symlink
        print(f"NOTE: {p} does not exist — nothing to snapshot")
        return
    dest = _snap_dest(rd, p)
    if not os.path.lexists(dest):
        # follow_symlinks=False: a symlink is snapshotted AS a symlink (the link
        # itself), never copied through to the file it points at.
        shutil.copy2(p, dest, follow_symlinks=False)
    if p.is_symlink():
        _manifest_append({"op": "snapshot", "path": str(p),
                          "symlink_target": os.readlink(p)})
    else:
        _manifest_append({"op": "snapshot", "path": str(p), "sha": _sha(p)})
    print(f"snapshotted {p}")


def do_move(src, dst):
    _guard(src)
    _guard(dst)
    p = _abspath(src)
    d = _abspath(dst)
    # Snapshot the source literally. shutil.move recreates a symlink as a
    # symlink, so removing the old .resolve() is what actually fixes the target
    # mutation — the snapshot is a belt-and-braces backup for files/links.
    if p.is_symlink() or (os.path.lexists(p) and p.is_file()):
        do_snapshot(str(p))
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), str(d))
    _manifest_append({"op": "move", "src": str(p), "dst": str(d)})
    print(f"moved {p} -> {d}")


def do_trash(path):
    _guard(path)
    rd = _run_dir()
    p = _abspath(path)
    if not os.path.lexists(p):
        print(f"NOTE: {p} does not exist — nothing to trash")
        return
    rel = str(p).lstrip("/")
    dest = rd / "trash" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Snapshot files and symlinks; for a real directory the move into trash/ IS
    # the full, revertible copy (shutil.copy2 throws IsADirectoryError on a dir).
    # is_symlink() is checked first so a symlink-to-a-dir is snapshotted as a
    # link, not treated as its target directory.
    if p.is_symlink() or p.is_file():
        do_snapshot(str(p))
    shutil.move(str(p), str(dest))
    _manifest_append({"op": "trash", "path": str(p), "trash": str(dest)})
    print(f"trashed (revertible) {p}")


def do_write_begin(path):
    _guard(path)
    do_snapshot(path)  # snapshot then the session edits in place
    print(f"ok to edit {path} (snapshot taken)")


def do_created(path):
    _guard(path)
    p = _abspath(path)
    if p.is_symlink():
        _manifest_append({"op": "created", "path": str(p),
                          "symlink_target": os.readlink(p)})
    else:
        _manifest_append({"op": "created", "path": str(p)})
    print(f"recorded created {p}")


def do_note(text):
    _manifest_append({"op": "note", "text": text})
    print("noted")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    try:
        if cmd == "snapshot":
            do_snapshot(argv[2])
        elif cmd == "move":
            do_move(argv[2], argv[3])
        elif cmd == "trash":
            do_trash(argv[2])
        elif cmd == "write-begin":
            do_write_begin(argv[2])
        elif cmd == "created":
            do_created(argv[2])
        elif cmd == "note":
            do_note(argv[2])
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 1
    except IndexError:
        print(f"missing argument for '{cmd}'", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
