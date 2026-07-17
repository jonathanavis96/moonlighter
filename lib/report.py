"""report.py — write the morning report for a run and fire notifications."""
import datetime
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import notify as notifymod   # noqa: E402


def _manifest_counts(run_dir):
    mf = run_dir / "manifest.jsonl"
    counts = {}
    if mf.exists():
        for line in mf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                op = json.loads(line).get("op", "?")
            except Exception:
                op = "?"
            counts[op] = counts.get(op, 0) + 1
    return counts


def write_report(cfg, run_dir, meta):
    report_dir = cfg["report_dir_path"]
    report_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.datetime.now().strftime("%Y-%m-%d")
    rid = meta["id"]
    dest = report_dir / f"moonlighter-{date}-{rid}.md"

    summary_md = ""
    sp = run_dir / "summary.md"
    if sp.exists():
        summary_md = sp.read_text(encoding="utf-8").strip()

    counts = _manifest_counts(run_dir)
    counts_line = ", ".join(f"{v} {k}" for k, v in counts.items()) or "no file actions"

    dry = meta.get("dry_run")
    tokens = meta.get("tokens_spent", 0)
    tokh = f"{tokens/1000:.0f}k" if tokens < 1_000_000 else f"{tokens/1_000_000:.1f}M"

    # The Revert section must reflect reality: if revert.sh was not generated
    # (write_revert_script failed — see finalisation_errors in run.json), the
    # report must say so instead of pointing at a script that does not exist.
    revert_sh = run_dir / "revert.sh"
    if revert_sh.exists():
        revert_md = f"""This run is fully revertible:

```
moonlight revert {rid}
```

(or inspect/run `{revert_sh}`)"""
    else:
        errs = "; ".join(meta.get("finalisation_errors", [])) or "revert.sh was not generated"
        revert_md = (
            "⚠ **revert.sh could not be generated — this run is NOT one-command revertible.**\n"
            f"Failure: `{errs}`\n"
            f"Review the file actions listed above and undo manually if needed (run dir: `{run_dir}`)."
        )

    body = f"""# Moonlighter — {meta.get('date_human', date)}  ({'dry run' if dry else 'full-auto'})

**Run id:** `{rid}`  ·  **status:** {meta.get('status')}  ·  **stop:** {meta.get('stop_reason')}
**Duration:** {meta.get('duration_min')} min  ·  **File actions:** {counts_line}

## What happened
{summary_md or '_No summary was written by the session._'}

## Usage math
- Primary bucket: `{meta.get('active_bucket')}`
- Utilization before → after: **{meta.get('util_before')}% → {meta.get('util_after')}%**  (Δ {meta.get('util_delta')}%)
- seven_day: {meta.get('seven_day_before')}% → {meta.get('seven_day_after')}%
- seven_day_sonnet: {meta.get('seven_day_sonnet_before')}% → {meta.get('seven_day_sonnet_after')}%
- Tokens processed this run: **{tokh}**
- Tonight's budget ceiling was: {meta.get('budget_pct')}% (≈ {meta.get('budget_tokens')} tok)

## Revert
{revert_md}
"""
    dest.write_text(body, encoding="utf-8")

    # notify
    headline = meta.get("headline", "Moonlighter run finished")
    spend_line = f"{meta.get('util_delta')}% · {tokh} tok"
    fired = notifymod.report_ready(cfg, headline, report_path=dest, spend_line=spend_line)
    meta["report_path"] = str(dest)
    meta["notified"] = fired
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest


def write_skip_note(cfg, reason):
    """Brief note for a skipped night (the report channels stay quiet for skips
    unless configured; we just append to the vault changelog if enabled)."""
    nc = cfg.get("notify") or {}
    if nc.get("vault_log"):
        date = datetime.datetime.now().strftime("%a %d %b")
        notifymod.vault_append(cfg, f"- **{date}** — skipped ({reason})")
