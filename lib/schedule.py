"""schedule.py — one-off scheduled tasks: a user-written mission, at a time they
picked, with a budget they set.

Storage is a single JSON file (`~/.moonlighter/scheduled.json`) read by the gate on
its existing */30 cron tick. No crontab mutation, survives reboot, self-cleans.
Firing granularity is therefore :00/:30 — 04:00 or 04:30, never 04:07.

Everything here is read from cron, so loading NEVER raises: a missing or corrupt
file yields an empty list. A scheduler that crashes the gate would take the whole
nightly system down with it.
"""
import datetime
import contextlib
import fcntl
import json
import pathlib
import secrets
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state  # noqa: E402

STATUS_PENDING = "pending"
STATUS_LAUNCHING = "launching"
STATUS_FIRED = "fired"
STATUS_MISSED = "missed"
STATUS_CANCELLED = "cancelled"


def _path():
    return state.STATE_DIR / "scheduled.json"


@contextlib.contextmanager
def _lock():
    """Serialize queue mutations across the panel and cron gate."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(".json.lock")
    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _load_unlocked():
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt file: behave as empty rather than taking the gate down. The
        # file is rewritten on the next save.
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, dict)]


def load():
    """Every task, newest first. Never raises — cron depends on this."""
    return _load_unlocked()


def _save_unlocked(tasks):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=p.parent,
        prefix=f".{p.name}.",
        suffix=".tmp",
        delete=False,
    ) as fh:
        json.dump(tasks, fh, indent=2)
        tmp = pathlib.Path(fh.name)
    tmp.replace(p)


def save(tasks):
    """Write atomically — the gate may read this file mid-write."""
    with _lock():
        _save_unlocked(tasks)


def new_id(run_at):
    """Readable + unique: the time it fires, plus entropy for collisions."""
    return f"{run_at.strftime('%Y%m%d-%H%M')}-{secrets.token_hex(2)}"


def add(task):
    with _lock():
        tasks = _load_unlocked()
        tasks.insert(0, task)
        _save_unlocked(tasks)
    return task


def get(task_id):
    for t in load():
        if t.get("id") == task_id:
            return t
    return None


def update(task_id, **fields):
    with _lock():
        tasks = _load_unlocked()
        hit = None
        for t in tasks:
            if t.get("id") == task_id:
                t.update(fields)
                hit = t
                break
        if hit is not None:
            _save_unlocked(tasks)
        return hit


def transition(task_id, from_status, **fields):
    """Conditionally update a task only if it is still in from_status."""
    with _lock():
        tasks = _load_unlocked()
        hit = None
        for t in tasks:
            if t.get("id") == task_id:
                if t.get("status") != from_status:
                    return None
                t.update(fields)
                hit = t
                break
        if hit is not None:
            _save_unlocked(tasks)
        return hit


def claim(task_id):
    """Claim a pending task for launch; returns None if another process won."""
    return transition(task_id, STATUS_PENDING, status=STATUS_LAUNCHING)


def cancel(task_id):
    """Only a pending task can be cancelled — never rewrite history."""
    return transition(task_id, STATUS_PENDING, status=STATUS_CANCELLED)


def due(now=None):
    """Pending tasks whose run_at has passed. Oldest first, so a backlog fires in
    the order it was scheduled.

    An unparseable run_at is skipped rather than treated as due — firing an
    autonomous run off a malformed date is the worst possible reading.
    """
    now = now or datetime.datetime.now().astimezone()
    out = []
    for t in load():
        if t.get("status") != STATUS_PENDING:
            continue
        try:
            when = datetime.datetime.fromisoformat(t["run_at"])
        except Exception:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when <= now:
            out.append((when, t))
    out.sort(key=lambda pair: pair[0])
    return [t for _, t in out]


def pending(now=None):
    """Pending tasks not yet due — what the panel shows as upcoming."""
    now = now or datetime.datetime.now().astimezone()
    out = []
    for t in load():
        if t.get("status") != STATUS_PENDING:
            continue
        try:
            when = datetime.datetime.fromisoformat(t["run_at"])
        except Exception:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when > now:
            out.append(t)
    return out


def build_mission(task):
    """The mission text handed to the run via ML_MISSION_FILE.

    The user's prompt is reproduced verbatim and leads the document — it is the
    actual instruction; everything else is framing. The chosen folder becomes the
    work root and attached docs are referenced by path (the run reads the live
    file), per the design.

    Deliberately does NOT restate the safety rules: the runner composes those
    around the mission, and off_limits is enforced by ml_fs regardless of what
    any mission says.
    """
    lines = ["# Scheduled task", ""]
    lines.append(task.get("prompt", "").strip())
    lines.append("")

    folder = (task.get("folder") or "").strip()
    if folder:
        lines += ["## Work root", "",
                  f"Do this work in `{folder}`. Stay inside this Work root for any "
                  "full-auto filesystem changes; anything elsewhere is audit-only.", ""]

    docs = [d for d in (task.get("docs") or []) if str(d).strip()]
    if docs:
        lines += ["## Reference documents", "",
                  "Read these before starting — they are the brief:", ""]
        lines += [f"- `{d}`" for d in docs]
        lines.append("")

    budget = []
    if task.get("wallclock_min"):
        budget.append(f"stop after ~{int(task['wallclock_min'])} minutes")
    if task.get("five_target"):
        budget.append(f"stop when the 5-hour window reaches {int(task['five_target'])}%")
    if budget:
        lines += ["## Budget", "",
                  "You will be stopped automatically at whichever comes first: "
                  + ", ".join(budget) + ". Work in priority order so the most "
                  "valuable work lands first, and leave things in a clean state.",
                  ""]

    return "\n".join(lines)
