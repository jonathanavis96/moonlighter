"""revert.py — build and run a byte-for-byte revert from a run's manifest."""
import json
import pathlib
import re
import shlex
import subprocess
import sys

STATE_RUNS = pathlib.Path.home() / ".moonlighter" / "runs"


def _read_manifest(run_dir):
    recs, _ = _read_manifest_with_torn(run_dir)
    return recs


def _read_manifest_with_torn(run_dir):
    """Return (records, torn_line_count).

    A torn/corrupt line (e.g. an interleaved concurrent append) is unparseable
    JSON. It must NOT vanish silently — that would under-revert the run with no
    signal. We count it and warn loudly on stderr so the caller can flag the
    revert as incomplete (audit findings #2/#5).
    """
    mf = run_dir / "manifest.jsonl"
    if not mf.exists():
        return [], 0
    recs = []
    torn = 0
    for lineno, line in enumerate(mf.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            torn += 1
            print(
                f"WARNING: manifest.jsonl line {lineno} is unparseable "
                f"(torn/corrupt) and will NOT be reverted: {line[:120]!r}",
                file=sys.stderr,
            )
    if torn:
        print(
            f"WARNING: {torn} manifest line(s) could not be parsed for run "
            f"{run_dir.name}; this revert is INCOMPLETE.",
            file=sys.stderr,
        )
    return recs, torn


def _snap_path(run_dir, abs_path):
    return run_dir / "snapshot" / str(pathlib.Path(abs_path)).lstrip("/")


def build_revert_script(run_dir):
    """Emit revert.sh content. Operations are reversed in manifest order."""
    recs, torn = _read_manifest_with_torn(run_dir)
    lines = [
        "#!/usr/bin/env bash",
        "# Auto-generated revert for Moonlighter run " + run_dir.name,
        "# Restores snapshots, un-moves moves, un-trashes deletes, removes created files.",
        "# Git branches (moonlighter/*) are left for manual inspection.",
        "set -uo pipefail",
        'echo "Reverting run ' + run_dir.name + '..."',
        "",
    ]
    if torn:
        lines.append(
            f'echo "WARNING: {torn} manifest line(s) were corrupt/torn and could '
            f'NOT be reverted — this revert is INCOMPLETE." >&2'
        )
        lines.append("")
    for rec in reversed(recs):
        op = rec.get("op")
        if op == "created":
            p = rec["path"]
            lines.append(f'[ -e {shlex.quote(p)} ] && rm -f {shlex.quote(p)} && echo "  removed created {p}"')
        elif op == "move":
            src, dst = rec["src"], rec["dst"]
            lines.append(f'if [ -e {shlex.quote(dst)} ]; then mkdir -p "$(dirname {shlex.quote(src)})"; mv -f {shlex.quote(dst)} {shlex.quote(src)} && echo "  un-moved -> {src}"; fi')
        elif op == "trash":
            p, tr = rec["path"], rec["trash"]
            lines.append(f'if [ -e {shlex.quote(tr)} ]; then mkdir -p "$(dirname {shlex.quote(p)})"; mv -f {shlex.quote(tr)} {shlex.quote(p)} && echo "  un-trashed -> {p}"; fi')
        elif op == "snapshot":
            p = rec["path"]
            snap = _snap_path(run_dir, p)
            # -P: never dereference; -p: preserve mode/times. Restores a
            # snapshotted symlink as a symlink, a regular file as its bytes.
            lines.append(f'if [ -e {shlex.quote(str(snap))} ] || [ -L {shlex.quote(str(snap))} ]; then mkdir -p "$(dirname {shlex.quote(p)})"; cp -Pp {shlex.quote(str(snap))} {shlex.quote(p)} && echo "  restored {p}"; fi')
        elif op == "note":
            # perm-fix notes carry an explicit "Revert: <cmd>" — honour it here
            # too, not only in the per-item panel path (_revert_one).
            m = re.search(r"Revert:\s*(chmod\s+\S+\s+\S.*)$", rec.get("text", ""))
            if m:
                cmd = m.group(1)
                lines.append(f'{cmd} && echo "  ran: {cmd}"')
    lines += ["", 'echo "Revert complete."', ""]
    return "\n".join(lines)


def write_revert_script(run_dir):
    content = build_revert_script(run_dir)
    dest = run_dir / "revert.sh"
    dest.write_text(content, encoding="utf-8")
    dest.chmod(0o755)
    return dest


def run_revert(run_id):
    run_dir = STATE_RUNS / run_id
    if not run_dir.exists():
        print(f"No such run: {run_id}", file=sys.stderr)
        return 1
    script = run_dir / "revert.sh"
    if not script.exists():
        script = write_revert_script(run_dir)
    return subprocess.call(["bash", str(script)])


# --- per-item revert (for the night-digest tick-to-revert checklist) ---

import os    # noqa: E402
import shutil  # noqa: E402


def _revert_one(run_dir, rec):
    """Reverse a single manifest record. Returns (ok, message)."""
    op = rec.get("op")
    try:
        if op == "created":
            p = pathlib.Path(rec["path"])
            if p.exists():
                p.unlink()
            return True, f"removed created {p}"
        if op == "move":
            src, dst = pathlib.Path(rec["src"]), pathlib.Path(rec["dst"])
            if dst.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
            return True, f"un-moved -> {src}"
        if op == "trash":
            p, tr = pathlib.Path(rec["path"]), pathlib.Path(rec["trash"])
            if tr.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tr), str(p))
            return True, f"un-trashed -> {p}"
        if op == "snapshot":
            p = rec["path"]
            snap = _snap_path(run_dir, p)
            if snap.exists() or snap.is_symlink():
                pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
                if snap.is_symlink():
                    if pathlib.Path(p).is_symlink() or pathlib.Path(p).exists():
                        pathlib.Path(p).unlink()
                    os.symlink(os.readlink(snap), p)
                else:
                    shutil.copy2(snap, p)
            return True, f"restored {p}"
        if op == "note":
            # perm-fix notes carry an explicit "Revert: <cmd>" — honour it.
            m = re.search(r"Revert:\s*(chmod\s+\S+\s+\S.*)$", rec.get("text", ""))
            if m:
                subprocess.call(["bash", "-c", m.group(1)])
                return True, f"ran: {m.group(1)}"
            return True, "note (nothing to revert)"
    except Exception as exc:
        return False, f"{op} failed: {exc}"
    return True, f"{op} (skipped)"


def revert_items(run_id, indices):
    """Revert specific manifest entries (by 0-based index) of a run, newest-first."""
    run_dir = STATE_RUNS / run_id
    recs = _read_manifest(run_dir)
    sel = sorted((i for i in indices if 0 <= i < len(recs)), reverse=True)
    done, errors = [], []
    for i in sel:
        ok, msg = _revert_one(run_dir, recs[i])
        (done if ok else errors).append(f"[{i}] {msg}")
    return {"ok": not errors, "reverted": done, "errors": errors}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: revert.py <run-id>")
        sys.exit(1)
    sys.exit(run_revert(sys.argv[1]))
