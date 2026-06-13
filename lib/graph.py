"""graph.py — build the 'week ahead' SVG from REAL usage data.

Plots, across the current weekly window (reset-7d → reset):
  * "so far"        — your actual seven_day utilization samples this week
  * "your forecast" — projected to your typical week-end (shaded)
  * "with moonlighter" — forecast + planned nightly spend, landing near the target
  * target line at (100 - reserve)%
Also returns a one-line caption: are you tracking above or below your average?
"""
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import state    # noqa: E402
import history  # noqa: E402

USAGE_LOG = pathlib.Path.home() / ".moonlighter" / "usage_log.jsonl"

VIEW_W, VIEW_H = 420, 150
Y_TOP, Y_BOT = 12, 140  # 100% → Y_TOP, 0% → Y_BOT


def _parse(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _y(util):
    util = max(0.0, min(100.0, float(util)))
    return Y_BOT - (util / 100.0) * (Y_BOT - Y_TOP)


def _this_week_samples(reset_iso):
    """seven_day (time, util) samples whose resets_at matches the current week."""
    if not USAGE_LOG.exists():
        return []
    out = []
    for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        sd = rec.get("seven_day") or {}
        if sd.get("resets_at") != reset_iso:
            continue
        t = _parse(rec.get("ts"))
        u = sd.get("utilization")
        if t is not None and u is not None:
            out.append((t, float(u)))
    out.sort(key=lambda x: x[0])
    return out


def build(cfg, usage, budget):
    reserve = float(cfg.get("weekly_reserve_pct", 10))
    target = 100.0 - reserve
    sd = (usage or {}).get("seven_day") or {}
    reset = _parse(sd.get("resets_at"))
    cur_util = float(sd.get("utilization") or 0.0)
    if reset is None:
        return {"svg": "", "caption": ""}
    week_start = reset - datetime.timedelta(days=7)
    span = (reset - week_start).total_seconds() or 1
    now = datetime.datetime.now(datetime.timezone.utc)

    def x_of(t):
        return VIEW_W * max(0.0, min(1.0, (t - week_start).total_seconds() / span))

    now_x = x_of(now)

    # --- EXPECTED curve, shaped by the real activity histogram ---
    # Your activity (heatmap) tells us WHEN you typically use Claude. Accumulating
    # that across the reset-aligned week gives an expected-usage curve that rises on
    # active days/hours and flattens overnight — scaled to a typical week total.
    # As real usage samples come in, the solid line below overrides it.
    # Scale LEARNED from real data: the 75th-percentile of your observed completed
    # weekly peaks (from usage_log, sampled every gate run). Falls back to the config
    # estimate only until ~2 weeks of data exist, then self-corrects. The longer the
    # system runs, the more accurate this assumption becomes.
    default_scale = float((cfg.get("forecast") or {}).get("typical_week_pct", 70))
    weeks = state.weekly_end_pcts("seven_day")
    expected_scale = state.typical_week_end_pct("seven_day", default_scale)
    learned = len(weeks) >= 2
    grid = history.get_histogram()                       # 7x24 local activity
    gtot = sum(sum(r) for r in grid) or 1
    exp_pts, cum = [], 0.0
    cur = week_start
    while cur <= reset:
        loc = cur.astimezone()
        cum += grid[loc.weekday()][loc.hour] / gtot
        if cur.hour % 3 == 0:
            exp_pts.append((x_of(cur), _y(expected_scale * cum)))
        cur += datetime.timedelta(hours=1)
    expected = " ".join(f"{x:.1f},{y:.1f}" for x, y in exp_pts)

    # --- so-far polyline (actual samples) ---
    samples = _this_week_samples(sd.get("resets_at"))
    pts = [(x_of(week_start), _y(0))]
    for t, u in samples:
        pts.append((x_of(t), _y(u)))
    pts.append((now_x, _y(cur_util)))
    sofar = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    ty = _y(target)
    reset_local = reset.astimezone().strftime("%a %H:%M")

    svg = f"""<svg width="100%" height="150" viewBox="0 0 420 150" preserveAspectRatio="none">
  <line x1="0" y1="{ty:.1f}" x2="420" y2="{ty:.1f}" stroke="rgba(212,180,120,.45)" stroke-dasharray="3 5"/>
  <text x="4" y="{ty-5:.1f}" fill="#d4b478" font-size="9" font-family="IBM Plex Mono" letter-spacing="2">{target:.0f}% RESERVE LINE</text>
  <polyline points="{expected}" fill="none" stroke="#9db5e2" stroke-width="1.4" stroke-dasharray="4 4" opacity=".75"/>
  <polyline points="{sofar}" fill="none" stroke="#e8e2d4" stroke-width="2.2"/>
  <circle cx="{now_x:.1f}" cy="{_y(cur_util):.1f}" r="3" fill="#e8e2d4"/>
  <line x1="{now_x:.1f}" y1="0" x2="{now_x:.1f}" y2="150" stroke="rgba(232,226,212,.2)"/>
  <text x="{VIEW_W-74:.0f}" y="147" fill="#94a0b8" font-size="8.5" font-family="IBM Plex Mono">{reset_local}</text>
</svg>"""

    weekly_cap = float((budget or {}).get("weekly_cap") or target)
    five_now = float((budget or {}).get("five_now") or 0)
    five_target = float((budget or {}).get("five_target") or 80)
    basis = (f"based on your ~{len(weeks)}-week average (~{expected_scale:.0f}%)" if learned
             else f"a rough ~{expected_scale:.0f}% estimate until ~2 weeks of usage are logged, then it self-corrects")
    caption = (f"Solid = your actual use ({cur_util:.0f}% this week). Dashed = expected — shape from "
               f"your activity pattern, scale {basis}. Moonlighter fills idle 5-hour windows and "
               f"stops at the <b>{weekly_cap:.0f}%</b> reserve line.")

    return {"svg": svg, "caption": caption, "samples": len(samples), "learned": learned}
