"""gate.py — decides IF and HOW BIG tonight's run is, and produces the status
contract consumed by the panel and CLI.

Run by cron every 30 min during the candidate window:  python3 lib/gate.py
Importable: compute_status(cfg, ...) for the panel/CLI (never launches).
"""
import datetime
import json
import os
import subprocess
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as cfgmod      # noqa: E402
import state                 # noqa: E402
import history               # noqa: E402
import budget as budgetmod   # noqa: E402
import usage_api             # noqa: E402
import graph as graphmod     # noqa: E402
import schedule              # noqa: E402

TMUX_SESSION = "moonlighter"


def _now_hms():
    return datetime.datetime.now().strftime("%H:%M:%S")


def active_bucket_name(cfg):
    return "seven_day_sonnet" if cfg.get("night_model") == "sonnet" else "seven_day"


def resolve_window(cfg):
    w = cfg.get("window", "auto")
    if isinstance(w, list) and w:
        return [int(h) for h in w]
    return history.idle_hours()  # auto


def run_in_flight():
    try:
        r = subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def gather_usage():
    try:
        return usage_api.get_usage(), None
    except Exception as exc:
        return None, str(exc)


def _extract_activity(pane):
    """Pull the agent's narration (● lines) + current spinner status from a pane."""
    narration, spinner = [], ""
    for ln in pane.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("●"):
            txt = s.lstrip("● ").strip()
            if txt:
                narration.append(txt)
        elif ("…" in s or "tokens)" in s) and "esc to interrupt" not in s and "ctrl+" not in s:
            spinner = s  # e.g. "✻ Sketching… (37s · ↓ 2.1k tokens)"
    out = narration[-7:]
    if spinner:
        out.append(spinner)
    return out


def get_active_run():
    """If a run is in flight, return {id, started, dry_run, budget_pct, activity[]}."""
    if not run_in_flight():
        return None
    run = None
    for r in state.list_runs(8):
        if r.get("status") == "running":
            run = r
            break
    if run is None:
        run = {"id": None, "started": None, "dry_run": True, "budget_pct": None}
    try:
        out = subprocess.run(["tmux", "capture-pane", "-pt", TMUX_SESSION, "-S", "-100"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=5).stdout
    except Exception:
        out = ""
    ask = None
    if run.get("id"):
        af = state.RUNS_DIR / run["id"] / "ask.json"
        if af.exists():
            try:
                ask = json.loads(af.read_text())
            except Exception:
                ask = None
    return {
        "id": run.get("id"),
        "started": run.get("started"),
        "dry_run": run.get("dry_run", True),
        "budget_pct": run.get("budget_pct"),
        "activity": _extract_activity(out),
        "ask": ask,
    }


def _bucket(usage, name):
    return (usage or {}).get(name) or {}


def compute_status(cfg=None, manual_away_hours=None):
    """Build the full status contract. Never launches anything."""
    cfg = cfg or cfgmod.load()
    state.ensure_dirs()
    manual = manual_away_hours is not None
    t = _now_hms()
    checks = []
    usage, uerr = gather_usage()
    window = resolve_window(cfg)
    abname = active_bucket_name(cfg)
    is_first_run = len(state.calibration_records()) == 0

    # --- hard skips ---
    kill = cfg["kill_switch_path"].exists()
    inflight = run_in_flight()

    # --- poll usage ---
    if usage is None:
        checks.append({"ts": t, "name": "poll usage", "verdict": "FAIL",
                       "why": uerr or "usage API unreachable", "value": ""})
    else:
        fh = _bucket(usage, "five_hour")
        wk = _bucket(usage, abname)
        checks.append({"ts": t, "name": "poll usage", "verdict": "OK",
                       "why": "", "value": f"5h {fh.get('utilization')}% · weekly {wk.get('utilization')}%"})

    five_util = float(_bucket(usage, "five_hour").get("utilization") or 0.0) if usage else None
    five_max = float(cfg.get("five_hour_max_pct", 20))

    # --- activity check ---
    mins = history.minutes_since_last_activity()
    ra_hours = float(cfg.get("recent_activity_hours", 2))
    if not manual:
        if mins is not None and mins < ra_hours * 60:
            checks.append({"ts": t, "name": "activity check", "verdict": "FAIL",
                           "why": f"you were active {int(mins)} m ago", "value": ""})
        else:
            checks.append({"ts": t, "name": "activity check", "verdict": "OK",
                           "why": ("quiet" if mins is None else f"idle {int(mins)} m"),
                           "value": ""})
    else:
        checks.append({"ts": t, "name": "activity check", "verdict": "OK",
                       "why": f"manual start — away {manual_away_hours} h", "value": ""})

    # --- 5-hour ceiling ---
    if five_util is not None:
        if five_util > five_max:
            checks.append({"ts": t, "name": "window check", "verdict": "FAIL",
                           "why": f"{five_util:.0f}% > {five_max:.0f}% threshold", "value": ""})
        else:
            checks.append({"ts": t, "name": "window check", "verdict": "OK",
                           "why": f"{five_util:.0f}% ≤ {five_max:.0f}% threshold", "value": ""})

    # --- idle-window check (scheduled only) ---
    cur_hour = datetime.datetime.now().hour
    if not manual:
        if window and cur_hour in window:
            checks.append({"ts": t, "name": "idle-window", "verdict": "OK",
                           "why": f"{cur_hour:02d}:00 in window", "value": ""})
        else:
            checks.append({"ts": t, "name": "idle-window", "verdict": "HOLD",
                           "why": f"{cur_hour:02d}:00 outside {min(window):02d}-{max(window):02d}" if window else "no window",
                           "value": ""})

    # --- weekly budget ---
    bud = None
    if usage is not None:
        bud = budgetmod.compute(usage, cfg, abname)
        if bud["ok"]:
            checks.append({"ts": t, "name": "spare capacity", "verdict": "OK",
                           "why": f"fill 5h to {bud['five_target']:.0f}% (now {bud['five_now']:.0f}%) · "
                                  f"weekly room {bud['weekly_room']:.0f}%", "value": ""})
        else:
            if bud["five_room"] <= 0.5:
                reason = f"5h window already at {bud['five_now']:.0f}%"
            else:
                reason = f"weekly reserve reached ({bud['weekly_now']:.0f}% of {bud['weekly_cap']:.0f}%)"
            checks.append({"ts": t, "name": "spare capacity", "verdict": "FAIL",
                           "why": reason, "value": ""})

    # --- hard-skip overrides ---
    if kill:
        checks.append({"ts": t, "name": "kill switch", "verdict": "FAIL",
                       "why": "paused (kill switch present)", "value": ""})
    if inflight:
        checks.append({"ts": t, "name": "in-flight", "verdict": "FAIL",
                       "why": "a run is already active", "value": ""})

    # --- verdict ---
    has_fail = any(c["verdict"] == "FAIL" for c in checks)
    has_hold = any(c["verdict"] == "HOLD" for c in checks)
    if has_fail:
        verdict = "SKIP" if (kill or inflight or (bud and not bud["ok"])) else "HOLD"
    elif has_hold:
        verdict = "HOLD"
    else:
        verdict = "GO"
    checks.append({"ts": t, "name": "verdict", "verdict": verdict, "why": "", "value": ""})

    summary = _summary(verdict, checks, window, manual)

    # --- assemble contract ---
    def hum(name):
        b = _bucket(usage, name)
        return {
            "utilization": b.get("utilization"),
            "resets_at": b.get("resets_at"),
            "resets_in": budgetmod.human_delta(budgetmod.parse_iso(b.get("resets_at"))),
        }

    status = {
        "ts": state.now_iso(),
        "mode": cfg.get("mode", "observe"),
        "approved": state.APPROVED_FLAG.exists() or cfg.get("mode") == "full-auto",
        "paused": kill,
        "phone_tier": cfg.get("phone_tier", "lan"),
        "live": usage is not None,
        "usage_error": uerr,
        "usage": {
            "five_hour": hum("five_hour"),
            "seven_day": hum("seven_day"),
            "seven_day_sonnet": hum("seven_day_sonnet"),
            "active_bucket": abname,
            "five_hour_max_pct": five_max,
            "weekly_reserve_pct": float(cfg.get("weekly_reserve_pct", 10)),
        },
        "gate": {
            "checks": checks,
            "verdict": verdict,
            "summary": summary,
            "next_check": _next_check_str(),
            "budget": bud,
        },
        "window": window,
        "heatmap": history.heatmap_normalized(),
        "heatmap_raw": history.get_histogram(),
        "heatmap_now": history.now_cell(),
        "graph": graphmod.build(cfg, usage, bud) if usage else {"svg": "", "caption": ""},
        "runs": _runs_for_panel(),
        "night": _night_label(window),
        "active_run": get_active_run(),
    }
    state.write_status_cache(status)
    return status


def _summary(verdict, checks, window, manual):
    if verdict == "GO":
        return "All clear — starting a run now." if manual else "Window's quiet and you're away. Running tonight's mission."
    reasons = [c for c in checks if c["verdict"] in ("FAIL", "HOLD") and c["name"] != "verdict"]
    if any(c["name"] == "kill switch" for c in reasons):
        return "Paused. Resume from the panel or `moonlight resume` when you want me back."
    if any(c["name"] == "in-flight" for c in reasons):
        return "A run is already in flight — watch it with `tmux attach -t moonlighter`."
    if any(c["name"] == "spare capacity" for c in reasons):
        return "Weekly reserve reached — leaving the rest for you. Nothing more tonight."
    win = ""
    if window:
        win = f" Tonight around {min(window):02d}:00 looks clear."
    if any(c["name"] == "activity check" for c in reasons):
        return "Holding — you're still around." + win
    if any(c["name"] == "window check" for c in reasons):
        return "Holding — your 5-hour window is hot." + win
    if any(c["name"] == "idle-window" for c in reasons):
        return "Not your quiet hours yet." + win
    return "Holding for now." + win


def _next_check_str():
    now = datetime.datetime.now()
    nxt = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(minutes=30)
    return nxt.strftime("%H:%M")


def _night_label(window):
    if not window:
        return "next window — none set"
    return f"next window  {min(window):02d}:00 – {max(window)+1:02d}:00"


def _runs_for_panel(limit=8):
    out = []
    for r in state.list_runs(limit):
        tm = ""
        try:
            tm = datetime.datetime.fromisoformat(r.get("started", "")).strftime("%H:%M")
        except Exception:
            tm = ""
        out.append({
            "id": r.get("id"),
            "date": r.get("date_human", r.get("id", "")),
            "time": tm,
            "headline": r.get("headline", "—"),
            "spend_pct": r.get("spend_pct"),
            "five_delta": r.get("five_delta"),
            "tokens": r.get("tokens"),
            "dry_run": r.get("dry_run"),
            "apply": r.get("apply"),
            "status": r.get("status", "—"),
        })
    return out


def _process_scheduled(cfg):
    """Fire at most one due scheduled task.

    A scheduled task named a specific time, so unlike the nightly job it skips
    the idle-window and recent-activity checks below — it still refuses when
    Moonlighter is switched OFF, and the runner still enforces the wallclock/5h
    caps via the env overrides passed here.

    Returns True if a task was launched this tick, so main() can skip its own
    GO launch below and avoid firing two runs in the same tick (a race with
    run_in_flight(), which only sees a session once run.sh actually starts one).
    """
    fired = False
    for task in schedule.due():
        tid = task.get("id")

        if cfg["kill_switch_path"].exists():
            schedule.update(tid, status=schedule.STATUS_MISSED, note="switched off")
            continue

        if fired or run_in_flight():
            schedule.update(tid, status=schedule.STATUS_MISSED,
                             note="a run was already in flight")
            continue

        state.ensure_dirs()
        mission = schedule.build_mission(task)
        mission_file = state.STATE_DIR / f"scheduled-mission-{tid}.md"
        mission_file.write_text(mission, encoding="utf-8")

        env = dict(os.environ)
        env["ML_MISSION_FILE"] = str(mission_file)
        if task.get("wallclock_min"):
            env["ML_WALLCLOCK_MIN"] = str(task["wallclock_min"])
        if task.get("five_target"):
            env["ML_FIVE_TARGET"] = str(task["five_target"])

        run_sh = cfg["_project_dir"] / "run.sh"
        subprocess.Popen(["bash", str(run_sh)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        schedule.update(tid, status=schedule.STATUS_FIRED, fired_at=state.now_iso())
        state.gate_log(f"scheduled task {tid} fired -> {mission_file.name}")
        fired = True

    return fired


def main():
    """Cron entry. Compute status; launch the runner if the verdict is GO."""
    cfg = cfgmod.load()
    usage_now, _ = gather_usage()
    if usage_now is not None:
        state.append_usage_sample(usage_now)  # learn the weekly curve

    # Scheduled tasks are handled first and wrapped in isolation: a bug in the
    # scheduler must never take down the nightly gate below. A task fired here
    # already used this tick's single launch slot.
    scheduled_fired = False
    try:
        scheduled_fired = _process_scheduled(cfg)
    except Exception as exc:
        state.gate_log(f"scheduler error: {exc}")

    status = compute_status(cfg)
    verdict = status["gate"]["verdict"]
    bud = status["gate"]["budget"]
    line = (f"gate: {verdict}  5h={status['usage']['five_hour']['utilization']}% "
            f"wk={status['usage']['seven_day']['utilization']}% "
            f"five_room={bud['five_room'] if bud else '—'}%")
    state.gate_log(line)

    if verdict != "GO" or scheduled_fired:
        return 0

    # GO — launch the runner (run.sh decides dry-run vs act from config mode; the
    # runner reads the 5h target + weekly cap from config for its stop conditions).
    run_sh = cfg["_project_dir"] / "run.sh"
    env = dict(os.environ)
    env["ML_ACTIVE_BUCKET"] = bud["active_bucket"] if bud else "seven_day"
    state.gate_log(f"launching run.sh (mode={cfg.get('mode')}, fill 5h to "
                   f"{(cfg.get('five_hour_target_pct', 80))}%)")
    subprocess.Popen(["bash", str(run_sh)], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(compute_status(), indent=2, default=str))
    else:
        sys.exit(main())
