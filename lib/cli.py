"""cli.py — `moonlight` command. Everything the GUI does, scriptable."""
import argparse
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import webbrowser

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as cfgmod   # noqa: E402
import state              # noqa: E402
import gate as gatemod    # noqa: E402
import revert as revertmod  # noqa: E402

PROJECT = HERE.parent
TMUX = "moonlighter"
GOLD = "\033[38;5;179m"; DIM = "\033[38;5;245m"; OK = "\033[38;5;108m"
FAIL = "\033[38;5;174m"; HOLD = "\033[38;5;179m"; R = "\033[0m"


def _c(verdict):
    return {"OK": OK, "GO": OK, "FAIL": FAIL, "SKIP": FAIL, "HOLD": HOLD}.get(verdict, DIM)


def cmd_status(args):
    cfg = cfgmod.load()
    s = gatemod.compute_status(cfg)
    u = s["usage"]
    print(f"\n  {GOLD}Moonlighter{R} — mode {s['mode']}  ·  {s['night']}")
    print(f"  five-hour  {u['five_hour']['utilization']}%   (resets in {u['five_hour']['resets_in']}, "
          f"start ≤ {u['five_hour_max_pct']:.0f}%)")
    print(f"  weekly     {u['seven_day']['utilization']}%   (resets in {u['seven_day']['resets_in']}, "
          f"reserve {u['weekly_reserve_pct']:.0f}%)")
    print()
    for c in s["gate"]["checks"]:
        dots = "." * max(2, 22 - len(c["name"]))
        why = f"  {DIM}({c['why']}){R}" if c["why"] else ""
        print(f"  {c['ts']}  {c['name']} {DIM}{dots}{R} {_c(c['verdict'])}{c['verdict']:<5}{R}{why}")
    print(f"\n  {GOLD}{s['gate']['summary']}{R}\n")
    if not s["live"]:
        print(f"  {FAIL}No live usage data: {s['usage_error']}{R}\n")


def cmd_start(args):
    cfg = cfgmod.load()
    if cfg["kill_switch_path"].exists():
        print("Moonlighter is paused. Run `moonlight resume` first.")
        return 1
    if subprocess.run(["tmux", "has-session", "-t", TMUX],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        print("A run is already in flight. `moonlight attach` to watch it.")
        return 1
    # manual start: skip idle-window + 5h-ceiling pre-checks (the user said they're
    # away). The runner reads the 5h target + weekly cap from config and stops there.
    s = gatemod.compute_status(cfg, manual_away_hours=args.hours or 5)
    bud = s["gate"]["budget"]
    if bud is None:
        print("Cannot reach the usage API — refusing to start blind.")
        return 1
    if not bud["ok"]:
        print(f"Nothing to do: 5h at {bud['five_now']:.0f}% (target {bud['five_target']:.0f}%), "
              f"weekly {bud['weekly_now']:.0f}% (cap {bud['weekly_cap']:.0f}%).")
        return 1
    env = dict(os.environ)
    env["ML_ACTIVE_BUCKET"] = bud["active_bucket"]
    env["ML_AWAY_HOURS"] = str(args.hours or 5)
    mode = "dry run (observe)" if cfg.get("mode") != "full-auto" else "FULL-AUTO"
    print(f"\n  Starting Moonlighter — {mode}")
    print(f"  Fill 5h to {bud['five_target']:.0f}% (now {bud['five_now']:.0f}%) · "
          f"weekly cap {bud['weekly_cap']:.0f}% (now {bud['weekly_now']:.0f}%) · "
          f"wall-clock {cfg.get('max_wallclock_min')} min · away {args.hours or 5}h")
    print(f"  Watch it:  tmux attach -t {TMUX}    (Ctrl-b d to detach)\n")
    subprocess.Popen(["bash", str(PROJECT / "run.sh")], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return 0


def cmd_approve(args):
    cfg = cfgmod.load()
    runs = state.list_runs(50)
    did_dry = any(r.get("dry_run") for r in runs)
    if not did_dry and not args.force:
        print("No dry run has completed yet. Let the first observe-mode night run, review it,\n"
              "then `moonlight approve` (or `--force` to override).")
        return 1
    # flip config mode -> full-auto
    cfgpath = PROJECT / "config.yaml"
    txt = cfgpath.read_text(encoding="utf-8")
    txt = txt.replace("mode: observe", "mode: full-auto", 1)
    cfgpath.write_text(txt, encoding="utf-8")
    state.APPROVED_FLAG.write_text(state.now_iso(), encoding="utf-8")
    print("\n  ✔ Full-auto approved. Moonlighter will now ACT on its nightly runs.")
    print("  Exits you have at all times:")
    print("    moonlight pause        — stop it running (kill switch)")
    print("    moonlight revert <id>  — undo any run byte-for-byte")
    print("    moonlight log          — see every gate decision\n")
    return 0


def set_mode(mode):
    """Set operating mode in config.yaml. mode = 'full-auto' | 'observe'."""
    cfgpath = PROJECT / "config.yaml"
    txt = cfgpath.read_text(encoding="utf-8")
    import re as _re
    txt = _re.sub(r"^mode:\s*\S+", f"mode: {mode}", txt, count=1, flags=_re.M)
    cfgpath.write_text(txt, encoding="utf-8")
    if mode == "full-auto":
        state.APPROVED_FLAG.write_text(state.now_iso(), encoding="utf-8")


def cmd_mode(args):
    want = args.mode
    if want is None:
        print(f"mode: {cfgmod.load().get('mode')}")
        return 0
    want = "observe" if want in ("review", "observe") else "full-auto"
    set_mode(want)
    label = "FULL-AUTO (runs will act)" if want == "full-auto" else "REVIEW (dry runs only)"
    print(f"mode set to {label}")
    return 0


def cmd_revert(args):
    return revertmod.run_revert(args.run_id)


def cmd_pause(args):
    cfg = cfgmod.load()
    cfg["kill_switch_path"].parent.mkdir(parents=True, exist_ok=True)
    cfg["kill_switch_path"].write_text(state.now_iso(), encoding="utf-8")
    print("Paused. Moonlighter will not start any run until `moonlight resume`.")
    return 0


def cmd_resume(args):
    cfg = cfgmod.load()
    p = cfg["kill_switch_path"]
    if p.exists():
        p.unlink()
    print("Resumed. Moonlighter is armed again (subject to the gate).")
    return 0


def cmd_log(args):
    for line in state.read_gate_log(args.n):
        print(line)
    return 0


def cmd_ui(args):
    cfg = cfgmod.load()
    port = cfg.get("ui_port", 8377)
    # start server if not already up
    import urllib.request
    url = f"http://127.0.0.1:{port}/"
    try:
        urllib.request.urlopen(url, timeout=1)
        running = True
    except Exception:
        running = False
    if not running:
        subprocess.Popen(["python3", str(PROJECT / "panel" / "server.py")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        print(f"Started panel server on {url}")
        import time
        time.sleep(1.5)
    # open in the Windows browser on WSL
    opener = None
    for cand in ("wslview", "explorer.exe", "xdg-open"):
        if subprocess.run(["which", cand], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            opener = cand
            break
    if opener:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Panel: {url}")
    return 0


def cmd_attach(args):
    os.execvp("tmux", ["tmux", "attach", "-t", TMUX])


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _mark_revert_purged(run_dir, meta, meta_f):
    """Record that GC removed a run's revert data, so nothing later treats it as revertible.
    Deletes revert.sh (the report keys off its existence) and flags run.json; run_revert()
    checks the flag and refuses. Best-effort: if run.json can't be rewritten, say so."""
    (run_dir / "revert.sh").unlink(missing_ok=True)
    meta["revert_purged"] = True
    errs = list(meta.get("finalisation_errors") or [])
    errs.append(f"revert data purged by gc on {datetime.date.today():%Y-%m-%d}; "
                "run is no longer revertible")
    meta["finalisation_errors"] = errs
    try:
        meta_f.write_text(json.dumps(meta, indent=2))
    except OSError as e:
        print(f"  ! {run_dir.name}: purged data but could not update run.json ({e})",
              file=sys.stderr)


def cmd_gc(args):
    """Purge trash/ + snapshot/ for clean runs older than --days, keeping
    manifest.jsonl + run.json for audit/revert-listing. This is what stops
    ~/.moonlighter/runs from growing unbounded (each run keeps full revertible
    copies of everything it touched forever otherwise)."""
    days = args.days
    if days < 0:
        print("error: --days must be >= 0 (a negative value would put the cutoff in the "
              "future and make every run eligible)", file=sys.stderr)
        return 2
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    runs_dir = state.RUNS_DIR
    if not runs_dir.exists():
        print("No runs dir.")
        return 0
    freed = 0
    purged = 0
    kept = 0
    failed = 0
    for d in sorted(runs_dir.iterdir()):
        if not d.is_dir() or d.name.startswith(("apply-", "MOCK")):
            continue
        meta_f = d / "run.json"
        if not meta_f.exists():
            continue
        try:
            meta = json.loads(meta_f.read_text())
        except (OSError, ValueError) as e:
            print(f"  kept {d.name}: unreadable run.json ({e})")
            kept += 1
            continue
        if meta.get("status") != "clean":
            kept += 1
            continue
        # Fail closed on a missing/malformed timestamp: the directory mtime is not the run's
        # age (a later touch would reset it), and purging revert data whose real age is
        # unknown is exactly what we must not do.
        stamp = meta.get("finished") or meta.get("started") or ""
        try:
            when = datetime.datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            print(f"  kept {d.name}: missing or invalid run timestamp")
            kept += 1
            continue
        if when > cutoff:
            kept += 1
            continue
        existing = [t for t in (d / "trash", d / "snapshot") if t.exists()]
        if not existing:
            continue
        run_bytes = sum(_dir_bytes(t) for t in existing)
        if args.dry_run:
            print(f"  would purge {d.name}  ({run_bytes/1e9:.2f} GB)  [{when:%Y-%m-%d}]")
            freed += run_bytes
            purged += 1
            continue
        # Actually delete, counting only what really goes and reporting anything that doesn't.
        errors = []
        removed_bytes = 0
        removed_any = False
        for t in existing:
            tb = _dir_bytes(t)
            try:
                shutil.rmtree(t)
            except OSError as exc:
                errors.append((t, exc))
            if not t.exists():
                removed_bytes += tb
                removed_any = True
            elif not any(p == t for p, _ in errors):
                errors.append((t, "target still present after rmtree"))
        freed += removed_bytes
        # If ANY revert-data target was actually removed, the run can no longer be fully
        # reverted — mark it non-revertible even on a PARTIAL purge (one target gone, the
        # other failed) or a ZERO-BYTE one (an empty trashed dir is still a revert record),
        # so the report says so and `moonlight revert` refuses instead of running a revert.sh
        # that would silently skip the now-missing records. Key on removal, not bytes.
        if removed_any:
            _mark_revert_purged(d, meta, meta_f)
        if errors:
            failed += 1
            for p, exc in errors[:3]:
                print(f"  ! {d.name}: could not remove {p}: {exc}", file=sys.stderr)
            print(f"  partial {d.name}  freed {removed_bytes/1e9:.2f} GB (targets remain)")
            continue
        print(f"  purged {d.name}  freed {removed_bytes/1e9:.2f} GB  [{when:%Y-%m-%d}]")
        purged += 1
    verb = "would free" if args.dry_run else "freed"
    tail = f", {failed} failed" if failed else ""
    print(f"\n  {purged} run(s) {'eligible' if args.dry_run else 'purged'}, "
          f"{kept} kept (not clean / newer than {days}d){tail} — {verb} {freed/1e9:.2f} GB.")
    if args.dry_run:
        print("  (dry run — re-run with --apply to actually purge)")
    return 1 if failed else 0


def build_parser():
    p = argparse.ArgumentParser(prog="moonlight", description="Moonlighter control")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sp = sub.add_parser("start"); sp.add_argument("--hours", type=float, default=None)
    sp.add_argument("--budget", type=float, default=None); sp.set_defaults(fn=cmd_start)
    ap = sub.add_parser("approve"); ap.add_argument("--force", action="store_true")
    ap.set_defaults(fn=cmd_approve)
    mp = sub.add_parser("mode"); mp.add_argument("mode", nargs="?", choices=["full-auto", "review", "observe"])
    mp.set_defaults(fn=cmd_mode)
    rp = sub.add_parser("revert"); rp.add_argument("run_id"); rp.set_defaults(fn=cmd_revert)
    sub.add_parser("pause").set_defaults(fn=cmd_pause)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    lp = sub.add_parser("log"); lp.add_argument("-n", type=int, default=40); lp.set_defaults(fn=cmd_log)
    sub.add_parser("ui").set_defaults(fn=cmd_ui)
    sub.add_parser("attach").set_defaults(fn=cmd_attach)
    gp = sub.add_parser("gc", help="purge trash/+snapshot/ of clean runs older than --days (keeps manifest)")
    gp.add_argument("--days", type=int, default=14, help="keep revert data for runs newer than this many days (default 14)")
    gmx = gp.add_mutually_exclusive_group()
    gmx.add_argument("--apply", dest="dry_run", action="store_false", help="actually purge (default is dry-run)")
    gmx.add_argument("--dry-run", dest="dry_run", action="store_true", help="preview only (default)")
    gp.set_defaults(fn=cmd_gc, dry_run=True)
    return p


def main():
    args = build_parser().parse_args()
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
