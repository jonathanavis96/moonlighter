"""history.py — learn the user's activity pattern from Claude Code transcripts.

Two signals:
  * recent_activity_within(hours)  — cheap stat()-based "is the user around now?"
  * hour_of_week histogram         — parsed message timestamps, cached daily,
                                     used to auto-discover the idle window.
"""
import datetime
import json
import pathlib

PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"
HIST_CACHE = pathlib.Path.home() / ".moonlighter" / "histogram.json"
OWN_TRANSCRIPTS = pathlib.Path.home() / ".moonlighter" / "own_transcripts.txt"
CACHE_TTL_SEC = 24 * 3600


def _own_set():
    """Moonlighter's own night-session transcripts — excluded so it never learns
    that 'the user' was active during its own runs."""
    if not OWN_TRANSCRIPTS.exists():
        return set()
    return {l.strip() for l in OWN_TRANSCRIPTS.read_text().splitlines() if l.strip()}


# The night session runs in a dedicated cwd (~/.moonlighter/session), so all of
# Moonlighter's OWN transcripts land in this one project dir — exclude it wholesale
# from activity learning so Moonlighter never mis-reads its own runs as "user active".
OWN_PROJECT_MARKER = "moonlighter-session"


def _transcripts():
    if not PROJECTS_DIR.exists():
        return []
    own = _own_set()
    return [p for p in PROJECTS_DIR.rglob("*.jsonl")
            if OWN_PROJECT_MARKER not in str(p) and str(p) not in own]


def recent_activity_within(hours):
    """True if any transcript was modified within the last `hours`. stat()-only."""
    cutoff = datetime.datetime.now().timestamp() - hours * 3600
    for f in _transcripts():
        try:
            if f.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def minutes_since_last_activity():
    latest = 0.0
    for f in _transcripts():
        try:
            latest = max(latest, f.stat().st_mtime)
        except OSError:
            continue
    if latest == 0.0:
        return None
    return (datetime.datetime.now().timestamp() - latest) / 60.0


def _parse_timestamps(path, cutoff_dt):
    """Yield local datetimes of message lines newer than cutoff_dt."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                idx = line.find('"timestamp"')
                if idx == -1:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = obj.get("timestamp")
                if not ts:
                    continue
                try:
                    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                dt = dt.astimezone()  # to local tz
                if dt.replace(tzinfo=None) >= cutoff_dt:
                    yield dt
    except OSError:
        return


def build_histogram(weeks=4):
    """7x24 grid (day-of-week 0=Mon .. 6=Sun) of activity-minute counts.

    Counts distinct (hour, 10-min-bucket) message timestamps so a long session
    spanning several hours marks all those hours, not just its end.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(weeks=weeks)
    grid = [[0] * 24 for _ in range(7)]
    seen = set()  # (file, dow, hour, 10min) dedup
    for f in _transcripts():
        try:
            if datetime.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                continue
        except OSError:
            continue
        for dt in _parse_timestamps(f, cutoff):
            key = (f.name, dt.weekday(), dt.hour, dt.minute // 10)
            if key in seen:
                continue
            seen.add(key)
            grid[dt.weekday()][dt.hour] += 1
    return grid


def get_histogram(weeks=4):
    """Cached histogram (recomputed at most once / 24h)."""
    try:
        if HIST_CACHE.exists():
            age = datetime.datetime.now().timestamp() - HIST_CACHE.stat().st_mtime
            if age < CACHE_TTL_SEC:
                return json.loads(HIST_CACHE.read_text())
    except Exception:
        pass
    grid = build_histogram(weeks)
    try:
        HIST_CACHE.parent.mkdir(parents=True, exist_ok=True)
        HIST_CACHE.write_text(json.dumps(grid))
    except Exception:
        pass
    return grid


def hour_of_day_totals(grid=None):
    grid = grid or get_histogram()
    totals = [0] * 24
    for dow in range(7):
        for h in range(24):
            totals[h] += grid[dow][h]
    return totals


def idle_hours(grid=None, frac=0.15):
    """Hours whose total activity is below `frac` of the peak hour => candidate window."""
    totals = hour_of_day_totals(grid)
    peak = max(totals) or 1
    thresh = peak * frac
    return [h for h in range(24) if totals[h] <= thresh]


def heatmap_normalized(grid=None):
    """7x24 grid bucketed to 0..3 for the panel heatmap.

    Uses QUANTILES of the non-zero cells, not max-normalization: a single heavy
    hour (e.g. a 142-message 02:00) would otherwise dominate and wash every other
    cell down to the faintest level. Quantile thresholds keep the map legible.
    """
    grid = grid or get_histogram()
    nz = sorted(v for row in grid for v in row if v > 0)
    if not nz:
        return [[0] * 24 for _ in range(7)]

    def q(p):
        return nz[min(len(nz) - 1, int(p * (len(nz) - 1)))]

    t1, t2, t3 = q(0.35), q(0.70), q(0.90)
    out = []
    for dow in range(7):
        row = []
        for h in range(24):
            v = grid[dow][h]
            if v == 0:
                row.append(0)
            elif v <= t1:
                row.append(1)
            elif v <= t2:
                row.append(2)
            else:
                row.append(3)
        out.append(row)
    return out


def now_cell():
    """(dow, hour) of the current local time — for the panel's 'now' marker."""
    import datetime
    n = datetime.datetime.now()
    return [n.weekday(), n.hour]


if __name__ == "__main__":
    g = get_histogram()
    totals = hour_of_day_totals(g)
    idle = idle_hours(g)
    print("hour-of-day activity:")
    peak = max(totals) or 1
    for h in range(24):
        bar = "#" * int(40 * totals[h] / peak)
        mark = " <idle>" if h in idle else ""
        print(f"  {h:02d}:00 {totals[h]:4d} {bar}{mark}")
    print("idle window (candidate hours):", idle)
