"""cli.py — `moonlight` command. Everything the GUI does, scriptable."""
import argparse
import datetime
import json
import os
import pathlib
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
    return p


def main():
    args = build_parser().parse_args()
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
