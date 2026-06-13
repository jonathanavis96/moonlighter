"""state.py — Moonlighter state dir, run dirs, gate log, calibration ledger."""
import datetime
import json
import pathlib

STATE_DIR = pathlib.Path.home() / ".moonlighter"
RUNS_DIR = STATE_DIR / "runs"
GATE_LOG = STATE_DIR / "gate.log"
CALIBRATION = STATE_DIR / "calibration.jsonl"
STATUS_CACHE = STATE_DIR / "last_status.json"   # last computed status (for panel/CLI)
APPROVED_FLAG = STATE_DIR / "approved"          # presence => full-auto approved at least once
USAGE_LOG = STATE_DIR / "usage_log.jsonl"       # periodic usage samples (learns weekly curve)
OWN_TRANSCRIPTS = STATE_DIR / "own_transcripts.txt"  # Moonlighter's OWN session files (excluded from learning)


def ensure_dirs():
    for d in (STATE_DIR, RUNS_DIR, STATE_DIR / "reports"):
        d.mkdir(parents=True, exist_ok=True)


def now():
    return datetime.datetime.now()


def now_iso():
    return datetime.datetime.now().astimezone().isoformat()


def gate_log(line):
    """Append a timestamped line to the gate log (audit trail)."""
    ensure_dirs()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(GATE_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp}  {line}\n")


def read_gate_log(n=50):
    if not GATE_LOG.exists():
        return []
    lines = GATE_LOG.read_text(encoding="utf-8").splitlines()
    return lines[-n:]


def new_run_dir(when=None):
    when = when or datetime.datetime.now()
    rid = when.strftime("%Y%m%d-%H%M%S")
    d = RUNS_DIR / rid
    (d / "snapshot").mkdir(parents=True, exist_ok=True)
    (d / "trash").mkdir(parents=True, exist_ok=True)
    return rid, d


def list_runs(limit=50):
    if not RUNS_DIR.exists():
        return []
    out = []
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith(("apply-", "MOCK")):
            continue
        meta_f = d / "run.json"
        meta = {}
        if meta_f.exists():
            try:
                meta = json.loads(meta_f.read_text())
            except Exception:
                meta = {}
        meta.setdefault("id", d.name)
        out.append(meta)
        if len(out) >= limit:
            break
    return out


def calibration_records():
    if not CALIBRATION.exists():
        return []
    recs = []
    for line in CALIBRATION.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    return recs


def append_calibration(rec):
    ensure_dirs()
    with open(CALIBRATION, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def tokens_per_pct(bucket="seven_day", default=None):
    """Self-calibrating: running ratio of tokens spent per 1% of `bucket` util.

    Returns tokens-per-1%-utilization. First run (no data) returns `default`.
    """
    ratios = []
    for r in calibration_records():
        if r.get("primary_bucket") and r.get("primary_bucket") != bucket:
            continue
        spent = r.get("tokens_spent")
        d = r.get("util_delta")  # change in primary bucket util over the run
        if spent and d and d > 0:
            ratios.append(spent / d)
    if not ratios:
        return default
    return sum(ratios) / len(ratios)


def append_usage_sample(usage):
    """Record a usage snapshot (for learning the weekly consumption curve)."""
    ensure_dirs()
    rec = {"ts": now_iso()}
    for b in ("seven_day", "seven_day_sonnet", "five_hour"):
        v = (usage or {}).get(b) or {}
        rec[b] = {"utilization": v.get("utilization"), "resets_at": v.get("resets_at")}
    with open(USAGE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def weekly_end_pcts(bucket="seven_day"):
    """Per-completed-week peak utilization, grouped by resets_at.

    Utilization rises monotonically until the weekly reset, so the max sample in
    each reset-group approximates that week's end utilization.
    """
    if not USAGE_LOG.exists():
        return []
    groups = {}
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        b = rec.get(bucket) or {}
        util = b.get("utilization")
        resets = b.get("resets_at")
        if util is None or not resets:
            continue
        groups.setdefault(resets, []).append(float(util))
    out = []
    for resets, vals in groups.items():
        try:
            r = datetime.datetime.fromisoformat(resets.replace("Z", "+00:00"))
        except ValueError:
            continue
        if r < now_dt:  # completed week only
            out.append(max(vals))
    return sorted(out)


def typical_week_end_pct(bucket, bootstrap):
    """75th-percentile of observed completed-week peaks, or bootstrap default."""
    peaks = weekly_end_pcts(bucket)
    if len(peaks) < 2:
        return float(bootstrap)
    k = int(round(0.75 * (len(peaks) - 1)))
    return float(peaks[k])


def own_transcripts():
    """Absolute paths of Moonlighter's own night-session transcripts (set)."""
    if not OWN_TRANSCRIPTS.exists():
        return set()
    return {l.strip() for l in OWN_TRANSCRIPTS.read_text().splitlines() if l.strip()}


def add_own_transcripts(paths):
    ensure_dirs()
    existing = own_transcripts()
    new = [str(p) for p in paths if str(p) not in existing]
    if new:
        with open(OWN_TRANSCRIPTS, "a", encoding="utf-8") as fh:
            for p in new:
                fh.write(str(p) + "\n")
    return new


def write_status_cache(status):
    ensure_dirs()
    STATUS_CACHE.write_text(json.dumps(status, default=str), encoding="utf-8")


def read_status_cache():
    if not STATUS_CACHE.exists():
        return None
    try:
        return json.loads(STATUS_CACHE.read_text())
    except Exception:
        return None
