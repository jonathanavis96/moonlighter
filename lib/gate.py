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
    # Match ANY Sonnet model (bare "sonnet" keyword or an explicit Sonnet model id) from the
    # EFFECTIVE model: an ML_NIGHT_MODEL override (cron / moonlight start / panel) wins over
    # cfg, mirroring runner.main(), so the gate budgets against the same weekly pool the run
    # will actually draw. A Sonnet session draws the Sonnet pool.
    model = os.environ.get("ML_NIGHT_MODEL") or cfg.get("night_model") or ""
    return "seven_day_sonnet" if "sonnet" in model.lower() else "seven_day"


def _apply_reserve_override(cfg):
    """Return a cfg COPY with a stricter ML_RESERVE applied, mirroring runner.main().

    runner.main() refuses to launch when the weekly bucket is over the effective (possibly
    ML_RESERVE-stricter) cap, so every launch-decision preflight — the scheduled-task check,
    and compute_status() behind `moonlight start` / `/api/start` / the nightly gate — must
    budget with the SAME reserve. Otherwise a launcher reports GO / spawns run.sh only for the
    runner to exit without creating a run, repeating on each cron tick. Always a copy, so
    callers can further mutate it without touching the shared cfg.
    """
    out = dict(cfg)
    override = os.environ.get("ML_RESERVE")
    if override:
        try:
            out["weekly_reserve_pct"] = float(override)
        except ValueError:
            pass
    return out


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


def _usage_freshness(usage):
    """
    How old the usage numbers actually are, for the panel to display.

    Returns {"as_of": "HH:MM"|None, "age_sec": int|None, "stale": bool, "missing": bool}.
    `stale` True means the reading did NOT come from a fresh fetch — it is a cached
    value being served because the API is unavailable (typically HTTP 429). The panel
    must show this; otherwise a frozen gauge is indistinguishable from a live one,
    which is exactly how the 2026-07-17 "stuck at 0%/28%" bug hid for ~33 minutes.
    `missing` True means there is NO reading at all (fresh install whose first fetch
    failed, or an expired cache) — a different state from "cached": labelling it
    "cached, as of ?" would send troubleshooting down the wrong path.
    """
    if usage is None:
        return {"as_of": None, "age_sec": None, "stale": False, "missing": True}
    try:
        info = usage_api.last_serve_info()
        ts, age = info.get("fetched_at") or 0, info.get("age")
        return {
            "as_of": datetime.datetime.fromtimestamp(ts).strftime("%H:%M") if ts else None,
            "age_sec": int(age) if age is not None and age != float("inf") else None,
            "stale": bool(info.get("stale", True)),
            "missing": False,
        }
    except Exception:
        # Never let a display concern break the gate's decision path.
        return {"as_of": None, "age_sec": None, "stale": False, "missing": False}


def _display_freshness(usage, disp_ts, disp_data):
    """
    Freshness of the value the panel actually SHOWS, which may be a cached reading older
    than STALE_GRACE (decision-grade `usage` has already gone None by then). The gate will
    not act on it, but the panel should keep showing it, dated, rather than blank.

    `missing` here tracks the usage contract's `has_data`, not `_usage_freshness`'s own
    True-whenever-usage-is-None rule: once a cached reading exists to show, this is no
    longer "missing" even though decision-grade `usage` already is.
    """
    if usage is not None:
        return _usage_freshness(usage)
    if disp_data is not None and disp_ts:
        try:
            age = datetime.datetime.now().timestamp() - disp_ts
            return {
                "as_of": datetime.datetime.fromtimestamp(disp_ts).strftime("%H:%M"),
                "age_sec": int(age),
                "stale": True,
                "missing": False,
            }
        except Exception:
            pass
    return {"as_of": None, "age_sec": None, "stale": True, "missing": True}


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
    # Decision-grade `usage` is None once the reading is older than STALE_GRACE, so the gate
    # never launches on a stale number. For DISPLAY, fall back to the last known reading of
    # any age so the panel keeps showing it (dated) instead of blanking when the API is down
    # for a long spell. This value MUST NOT feed any check below — only the contract's gauges.
    disp_ts, disp_data = 0.0, usage
    if usage is None:
        disp_ts, disp_data = usage_api.last_known()
    window = resolve_window(cfg)
    abname = active_bucket_name(cfg)
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
        bud = budgetmod.compute(usage, _apply_reserve_override(cfg), abname)
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
        b = _bucket(disp_data, name)
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
            # Whether the gauges above carry a number at all (fresh OR cached). The panel
            # shows them whenever this is true; it only falls back to "no live data" when
            # there has never been a reading to show.
            "has_data": disp_data is not None,
            # Staleness of the numbers above. `live` only says we have DECISION-GRADE data —
            # on a 429 the shown value can be a cached reading (any age) and is otherwise
            # indistinguishable from a fresh one. A gauge that shows a frozen number as
            # current is worse than one that admits it is frozen.
            **_display_freshness(usage, disp_ts, disp_data),
        },
        "gate": {
            "checks": checks,
            "verdict": verdict,
            "summary": summary,
            "next_check": _next_check_str(),
            "budget": bud,
        },
        "window": window,
        "heatmap_raw": (_hm := history.get_histogram()),
        "heatmap": history.heatmap_normalized(grid=_hm),
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

        if fired:
            break

        if run_in_flight():
            schedule.update(tid, status=schedule.STATUS_MISSED,
                             note="a run was already in flight")
            continue

        usage, uerr = gather_usage()
        if usage is None:
            schedule.update(tid, status=schedule.STATUS_MISSED,
                            note=uerr or "usage API unreachable")
            continue

        abname = active_bucket_name(cfg)
        # Honour a stricter ML_RESERVE the same way runner.main() does, so a task is not
        # marked FIRED here only for run.sh to refuse it on the tighter cap and leave no run
        # behind (the task would then silently disappear).
        budget_cfg = _apply_reserve_override(cfg)
        if task.get("five_target"):
            budget_cfg["five_hour_target_pct"] = task["five_target"]
        bud = budgetmod.compute(usage, budget_cfg, abname)
        if not bud["ok"]:
            if bud["five_room"] <= 0.5:
                note = f"5h window already at {bud['five_now']:.0f}%"
            else:
                note = f"weekly reserve reached ({bud['weekly_now']:.0f}% of {bud['weekly_cap']:.0f}%)"
            schedule.update(tid, status=schedule.STATUS_MISSED, note=note)
            continue

        claimed = schedule.claim(tid)
        if claimed is None:
            continue
        task = claimed

        state.ensure_dirs()
        mission = schedule.build_mission(task)
        mission_file = state.STATE_DIR / f"scheduled-mission-{tid}.md"
        mission_file.write_text(mission, encoding="utf-8")

        env = dict(os.environ)
        env["ML_MISSION_FILE"] = str(mission_file)
        env["ML_ACTIVE_BUCKET"] = abname
        # The validated Work root the task was created against (`_validate_schedule()`
        # in panel/server.py requires + resolves it before the task is ever queued).
        # runner.py's build_scheduled_mission() needs this OUT OF BAND, structurally —
        # not just embedded as prose inside the mission text — so it can state the
        # concrete path in the brief and refuse to launch if it's ever missing. Left
        # unset (not "") when the task has no folder so runner.py's falsy check catches
        # legacy/corrupt records the same way as an outright missing value.
        folder = (task.get("folder") or "").strip()
        if folder:
            env["ML_WORK_ROOT"] = folder
        if task.get("wallclock_min"):
            env["ML_WALLCLOCK_MIN"] = str(task["wallclock_min"])
        if task.get("five_target"):
            env["ML_FIVE_TARGET"] = str(task["five_target"])

        run_sh = cfg["_project_dir"] / "run.sh"
        try:
            subprocess.Popen(["bash", str(run_sh)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            schedule.transition(tid, schedule.STATUS_LAUNCHING,
                                status=schedule.STATUS_MISSED,
                                note=f"launch failed: {exc}")
            continue
        schedule.transition(tid, schedule.STATUS_LAUNCHING,
                            status=schedule.STATUS_FIRED, fired_at=state.now_iso())
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
