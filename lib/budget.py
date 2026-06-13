"""budget.py — SPARE-CAPACITY model.

The point of Moonlighter is to use the capacity you're NOT using. The 5-hour
window resets every 5 hours, so any idle 5-hour window is wasted capacity. So a
run simply FILLS the current 5-hour window up to `five_hour_target_pct` (e.g. 80%)
— bounded by the hard rule that weekly utilization never crosses
`100 - weekly_reserve_pct` (the slice always left for the user).

No forecast guesswork, no per-night micro-slices. Two knobs the user understands:
  five_hour_target_pct  — how full to drive each idle 5-hour window
  weekly_reserve_pct    — how much of the week to always leave for the user
"""
import datetime


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def human_delta(target_dt, now=None):
    if target_dt is None:
        return "—"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=datetime.timezone.utc)
    secs = (target_dt - now).total_seconds()
    if secs <= 0:
        return "now"
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    if h >= 24:
        return f"{h // 24} d {h % 24} h"
    return f"{h} h {m} m"


def compute(usage, cfg, active_bucket_name, now=None):
    """Spare-capacity budget. Returns the run's stop targets + whether to run."""
    reserve = float(cfg.get("weekly_reserve_pct", 10))
    five_target = float(cfg.get("five_hour_target_pct", 80))
    weekly_cap = 100.0 - reserve

    active = usage.get(active_bucket_name) or {}
    weekly_now = float(active.get("utilization") or 0.0)
    five = usage.get("five_hour") or {}
    five_now = float(five.get("utilization") or 0.0)

    five_room = max(0.0, five_target - five_now)        # 5h capacity left to fill
    weekly_room = max(0.0, weekly_cap - weekly_now)     # weekly headroom before reserve

    # Worth running if there's both 5h room to fill AND weekly headroom left.
    ok = five_room > 0.5 and weekly_room > 0.5

    return {
        "active_bucket": active_bucket_name,
        "five_now": round(five_now, 1),
        "five_target": five_target,
        "five_room": round(five_room, 1),
        "weekly_now": round(weekly_now, 1),
        "weekly_cap": weekly_cap,
        "weekly_room": round(weekly_room, 1),
        "reserve_pct": reserve,
        "ok": ok,
        "five_resets_at": five.get("resets_at"),
        "resets_at": active.get("resets_at"),
        # back-compat display field: "tonight" spend is bounded by weekly headroom
        "tonight_pct": round(min(weekly_room, 100.0), 1),
    }
