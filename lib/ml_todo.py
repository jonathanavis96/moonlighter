"""ml_todo.py — the night/apply agent registers an OUTSTANDING actionable item.

The agent calls this ONCE per concrete thing it is leaving for the user to approve
(a proposal, something it skipped, something that needs the user). It does NOT call
this for work it already completed — that is tracked in the manifest for revert.

Each call appends one validated JSON line to $ML_RUN_DIR/todo.jsonl. Because the
agent never hand-writes JSON, the night report's tick-to-DO list can't be corrupted
by malformed output, and already-done work is excluded by construction (the agent
simply doesn't register it) — so the user only ever ticks real, outstanding tasks.

Usage (run by the agent):
    python3 ml_todo.py add \
        --title "Shrink baobab-wines .git via Git LFS" \
        --detail "883 MB .git from committed wine PNGs + portfolio PDF." \
        --cmd "git lfs migrate import --include='*.png,*.pdf' --everything" \
        --cmd "git push --force --all" \
        --category cleanup --risk medium --needs-push --repo baobab-wines

Categories: security | backup | cleanup | hygiene | idea  (free text tolerated).
Flags: --needs-sudo, --needs-push (set --repo to name the repo the push targets).
"""
import argparse
import json
import os
import pathlib
import sys
import time

CATEGORIES = {"security", "backup", "cleanup", "hygiene", "idea"}


def main(argv=None):
    p = argparse.ArgumentParser(prog="ml_todo")
    sub = p.add_subparsers(dest="action")
    a = sub.add_parser("add", help="register one outstanding actionable item")
    a.add_argument("--title", required=True, help="short imperative task name")
    a.add_argument("--detail", default="", help="one or two sentences of why/what")
    a.add_argument("--cmd", action="append", default=[], help="a command (repeatable)")
    a.add_argument("--category", default="idea")
    a.add_argument("--risk", default="low", choices=["low", "medium", "high"])
    a.add_argument("--needs-sudo", action="store_true")
    a.add_argument("--needs-push", action="store_true")
    a.add_argument("--repo", default="", help="repo the push targets (if --needs-push)")
    args = p.parse_args(argv)

    rd = os.environ.get("ML_RUN_DIR")
    if not rd:
        print("(ml_todo: no ML_RUN_DIR — not recorded)")
        return 0
    if args.action != "add":
        p.print_help()
        return 2

    title = args.title.strip()
    if not title:
        print("(ml_todo: empty title — not recorded)")
        return 2
    cat = args.category.strip().lower()
    if cat not in CATEGORIES:
        cat = "idea"  # tolerate free text, bucket as idea
    rec = {
        "title": title,
        "detail": args.detail.strip(),
        "commands": [c for c in (c.strip() for c in args.cmd) if c],
        "category": cat,
        "risk": args.risk,
        "needs_sudo": bool(args.needs_sudo),
        "needs_push": bool(args.needs_push),
        "repo": args.repo.strip(),
        "ts": time.time(),
    }
    tf = pathlib.Path(rd) / "todo.jsonl"
    with tf.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"(todo registered: {title})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
