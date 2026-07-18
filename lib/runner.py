"""runner.py — drives a REAL interactive Claude Code session in tmux to spend the
leftover subscription quota on local housekeeping. Called by run.sh.

Flow: lock -> run dir + mission -> launch tmux -> deliver mission -> supervise
(idle / budget / wall-clock) -> capture -> revert.sh -> report -> notify.
"""
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from shlex import quote as shlex_quote

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as cfgmod      # noqa: E402
import state                 # noqa: E402
import usage_api             # noqa: E402
import revert as revertmod   # noqa: E402
import report as reportmod   # noqa: E402
import digest as digestmod   # noqa: E402

TMUX = "moonlighter"
PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"
# Dedicated cwd so the night session's transcripts land in ONE known project dir
# (deterministic history-exclusion + token accounting). The session still operates
# on absolute paths under $HOME — cwd only sets where the transcript is stored.
SESSION_CWD = pathlib.Path.home() / ".moonlighter" / "session"
# Claude Code encodes a session's cwd into its projects-dir name by replacing every
# non-alphanumeric character with '-'. Derive ours the same way so the transcript dir
# is found on ANY machine/user (no hardcoded home path).
OWN_PROJECT_DIR = PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", str(SESSION_CWD))
# Claude-injected env vars to scrub so the nested session is a normal top-level
# session that writes its transcript (a child session suppresses it).
SCRUB_ENV = ["CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
             "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT",
             "AI_AGENT"]
POLL = 5
IDLE_CONFIRMATIONS = 6           # ~30s of unchanged screen = turn complete
BUDGET_CHECK_SEC = 300           # re-poll usage every ~5 min (tighter for the 5h target)


def _sh(*args, **kw):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, **kw)


def _capture():
    r = _sh("tmux", "capture-pane", "-pt", TMUX)
    return r.stdout if r.returncode == 0 else ""


def _session_alive():
    return subprocess.run(["tmux", "has-session", "-t", TMUX],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _all_transcripts():
    if not PROJECTS_DIR.exists():
        return set()
    return {str(p) for p in PROJECTS_DIR.rglob("*.jsonl")}


def _session_transcripts(since_ts):
    """Transcripts in Moonlighter's own project dir touched at/after since_ts."""
    if not OWN_PROJECT_DIR.exists():
        return []
    out = []
    for p in OWN_PROJECT_DIR.glob("*.jsonl"):
        try:
            if p.stat().st_mtime >= since_ts - 5:
                out.append(p)
        except OSError:
            continue
    return out


def _sum_tokens(paths):
    """Sum genuinely-new tokens (input + output + cache creation). Deliberately
    EXCLUDES cache_read_input_tokens — those are cheap re-reads of already-cached
    context and re-counting them per turn balloons the total to millions, which
    badly misrepresents 'tokens spent'."""
    total = 0
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    usage = (obj.get("message") or {}).get("usage") or obj.get("usage") or {}
                    for k in ("input_tokens", "output_tokens",
                              "cache_creation_input_tokens"):
                        total += int(usage.get(k) or 0)
        except OSError:
            continue
    return total


def build_mission(cfg, run_dir, dry_run, five_now, five_target, weekly_now,
                  weekly_cap, reserve, away_hours, wallclock_min, prior=""):
    wishlist = cfg.get("wishlist") or []
    wl = "\n".join(f"  - {w}" for w in wishlist) if wishlist else "  (none specified — use your judgment)"
    off = cfg.get("off_limits_resolved", [])
    offl = "\n".join(f"  - {p}" for p in off)
    night_model = cfg.get("night_model", "default")
    mlfs = HERE / "ml_fs.py"
    mltodo = HERE / "ml_todo.py"
    mode_block = (
        "## MODE: DRY RUN (observe) — TOUCH NOTHING\n"
        "This is a dry run. Do NOT modify, move, create, or delete ANY file outside this run dir.\n"
        "Investigate the user's home/workspace and PROPOSE what you would do. The only file you may\n"
        "write is `$ML_RUN_DIR/summary.md`.\n"
        "Be EXHAUSTIVE — you have a real investigation budget, so USE it: survey thoroughly and deeply\n"
        "across every area (home root, ~/code, Downloads, Desktop, caches, .config, large files,\n"
        "duplicates, stale backups, logs, node_modules/build cruft, old archives), quantify sizes,\n"
        "md5-verify duplicates, and build a comprehensive, prioritised audit. Keep digging into new\n"
        "areas until you approach the budget — do NOT stop after a quick skim of one or two folders.\n"
        if dry_run else
        "## MODE: FULL-AUTO — you may act, but only via the helper below\n"
        f"EVERY filesystem mutation MUST go through the helper so it is revertible:\n"
        f"  python3 {mlfs} write-begin <path>   # BEFORE editing a file in place\n"
        f"  python3 {mlfs} move <src> <dst>     # to move/rename\n"
        f"  python3 {mlfs} trash <path>         # to 'delete' (moves into the run trash)\n"
        f"  python3 {mlfs} created <path>        # AFTER creating a new file\n"
        f"  python3 {mlfs} note \"<text>\"        # to log a decision/non-fs action\n"
        "NEVER use mv/rm or write files in place without the matching helper call first.\n"
        "For any git repo: create & checkout a branch `moonlighter/" + run_dir.name + "` first,\n"
        "commit there, NEVER push, NEVER touch the working branch.\n"
    )
    director = (
        "## Work as a DIRECTOR\n"
        "Keep judgment, prioritisation, and anything risky at your own level. Delegate well-scoped\n"
        "mechanical chores (bulk file moves per a decided plan, renames, formatting, summarising\n"
        "file contents) to Sonnet sub-agents via the Agent tool with `model: \"sonnet\"`, each given a\n"
        "self-contained prompt with explicit file paths and acceptance criteria.\n"
        if night_model == "default" else
        "## Work directly\n"
        "You are running on Sonnet for a trivial night. Do the work yourself; keep it small.\n"
    )

    # ---- estate sections, built from the user's config (folders / vault / devices) ----
    roots = cfg.get("work_roots") or ["~"]
    roots_disp = ", ".join(f"`{r}`" for r in roots)
    vault = cfg.get("vault_path") or ""
    devices = cfg.get("devices") or {}
    audit_hosts = devices.get("audit") or []
    backup = devices.get("backup") or {"kind": "none"}
    branch = f"moonlighter/{run_dir.name}"

    remit = []
    remit.append(f"**Files in your work folders** ({roots_disp}) — loose-file tidying, duplicate "
                 "detection (md5-verify), stale caches/build output, dead artifacts.")
    remit.append(
        f"**Git repos inside those folders** — uncommitted work (stage + commit on a `{branch}` "
        "branch, NEVER push), `.gitignore` gaps, secrets accidentally tracked, broken/large files. "
        "COMMIT RULES: author as the user; **NEVER add `Co-Authored-By` or any AI-attribution "
        "trailer**. **Do NOT commit in client/deliverable repos** (anything for a client, or whose "
        "history must stay clean) — AUDIT those read-only and report. When unsure, audit-only.")
    if vault:
        remit.append(
            f"**Your notes vault** (`{vault}`) — vault maintenance: fix drift (stale statuses/"
            "checkboxes vs reality), broken/empty notes, 0-byte phantom stubs, orphan notes, "
            "missing links. Edit notes via the helper so it's revertible.")
    if os.path.exists("/mnt/c"):
        remit.append(
            "**The Windows side** (`/mnt/c`) — AUDIT ONLY (never modify Windows): Downloads/Desktop "
            "clutter, huge files, obvious junk → recommend, don't act.")
    if audit_hosts:
        hosts = "; ".join(f"{h.get('name', h.get('ssh_host'))} (`ssh {h.get('ssh_host')}`)"
                          for h in audit_hosts)
        remit.append(
            f"**Remote devices** — {hosts} — READ-ONLY audit over SSH (`df -h; docker ps; "
            "systemctl --failed; journalctl -p err -n 50` etc.). Report disk pressure, failed "
            "services, errors, drift. Propose fixes; do NOT change them.")
    remit.append(
        "**Security posture** — world-readable secrets, loose file perms, exposed ports, stale "
        "creds, out-of-date packages. Report findings; only fix trivially-safe LOCAL perms.")
    remit.append("**Live sites / services** — if quick & read-only, sanity-check they respond. Report only.")
    remit_block = "\n".join(f"{i+1}. {r}" for i, r in enumerate(remit))

    if backup.get("kind") == "ssh":
        dest = backup.get("dest_path") or "backups"
        backup_note = (f"An off-box backup destination IS configured: `ssh {backup.get('ssh_host')}:"
                       f"{dest}`. For at-risk unpushed/local-only work, you may propose (and on an "
                       "APPLY run, set up) a git-bundle backup there.")
    elif backup.get("kind") == "mount":
        backup_note = (f"An off-box backup destination IS configured: `{backup.get('dest_path')}` "
                       "(mounted). Propose/route backups of at-risk work there.")
    else:
        backup_note = ("NO off-box backup destination is configured — do NOT propose pushing "
                       "backups to a remote. If data-loss risk is high, suggest the user add one.")

    act_targets = roots_disp
    audit_targets = []
    if os.path.exists("/mnt/c"):
        audit_targets.append("the Windows side (`/mnt/c`)")
    if audit_hosts:
        audit_targets.append("the remote devices (read-only SSH)")
    audit_targets.append("live sites/services, and anything needing installs or system/root changes")
    audit_line = ", ".join(audit_targets)

    return f"""# Moonlighter mission — {run_dir.name}

You are Moonlighter: Claude working autonomously in the user's idle hours, improving the
user's WHOLE digital estate. You draw the user's leftover subscription quota — use it well.
Think like the "Full estate audit" a senior engineer would run overnight: sweep broadly,
fix what's safe, and surface a rich set of findings, recommendations, and ideas.

{mode_block}
{director}
{("## ALREADY COVERED TONIGHT — do NOT repeat (this is a later run of the night)" + chr(10) + prior + chr(10) + "Earlier runs already swept the estate and reported the above. Your value is in NEW ground or DOING the proposed work — re-reporting the same findings is wasted effort." + chr(10)) if prior else ""}
## YOUR REMIT — your whole estate (be ambitious; there is lots to do)
Sweep these areas (spawn parallel Sonnet sub-agents, one per area, to cover them concurrently):
{remit_block}

Off-box backup: {backup_note}

## BRAINSTORM & RECOMMEND (spend real effort here — the user explicitly wants this)
Beyond chores, generate genuine value as PROPOSALS in the report:
- **New ideas** — projects, automations, workflow improvements you'd suggest given what you see.
- **Software recommendations** — tools that would help, with why and how they'd fit.
- **Security fixes** — concrete, prioritised, with the exact remediation.
- **Quick wins** — small high-leverage things the user could do.
Be specific and opinionated, not generic. This section is as valuable as the cleanup.

## Spare-capacity budget — USE IT, do not quit early
Moonlighter spends the capacity the user is NOT using. The 5-hour rate-limit window resets
every 5 hours, so any idle 5-hour window is wasted forever — your job is to FILL it.
**KEEP WORKING** through useful, safe tasks until ONE of these ceilings is hit — do NOT stop
after a single task or one quick pass; after each task pick up the next most useful one:
- the **5-hour window reaches ~{five_target:.0f}%** (it is at {five_now:.0f}% right now), OR
- weekly usage reaches **{weekly_cap:.0f}%** (it is at {weekly_now:.0f}% now — the {reserve:.0f}% reserve
  is always left for the user), OR
- the wall-clock cap (**{wallclock_min} minutes**).
Always leave each individual task in a clean, finished state. Do NOT "save" capacity by stopping
early — unused 5-hour capacity is lost. Only write the final summary when a ceiling above is
reached, you are told to stop, or you have genuinely exhausted all useful, safe work.

## The user's standing wishlist
{wl}

## OFF-LIMITS — never read, touch, or even open
{offl}
  - ...and ANY credential, secret, key, token, password store, or .env file. Skip silently.

## HARD RULES (non-negotiable)
- Nothing outward-facing, EVER: no git push, no deploys, no emails/messages, no calendar events,
  no publishing API calls, no installs that phone home. Unattended ⇒ no irreversible outward acts.
- ACT only on LOCAL, reversible things inside your work folders ({act_targets}): files there, git
  staging/commits on the `{branch}` branch (NEVER push){', and vault note edits' if vault else ''} —
  all via the helper.
- AUDIT ONLY, never modify: {audit_line} — surface as proposals.
- No real deletes: "delete" means `ml_fs.py trash` (revertible). Snapshot before editing any file.
- Log every mutating action to the manifest via the helper BEFORE doing it.
- Anything risky, ambiguous, outward-facing, or remote: do NOT act — record it as a proposal.
- Secrets / credentials / keys / .env: never read or open. Skip silently.

## When you finish (or are told to stop)
FIRST register every OUTSTANDING actionable item — anything you did NOT complete that the user
might want done (a proposal, something you couldn't do unattended, something needing the user).
Register each as ONE atomic, self-contained task via the helper (do NOT hand-write JSON):
  python3 {mltodo} add --title "<short imperative>" --detail "<why/what, 1-2 sentences>" \\
      [--cmd "<a command>" ...] --category security|backup|cleanup|hygiene|idea \\
      --risk low|medium|high [--needs-sudo] [--needs-push --repo <name>]
RULES for todos (this is what the user ticks to approve — keep it clean):
- ONE concrete action per call (not a section header, not a status line, not a list of five things).
- NEVER register something you already DID — that is tracked for revert automatically. Outstanding
  work only. This is why the user's approve-list stays short and 100% real.
- Set --needs-sudo / --needs-push HONESTLY so the apply step uses the right permission path.
- No todo at all is correct if there is genuinely nothing left to do.

THEN write `$ML_RUN_DIR/summary.md` (the run dir is {run_dir}) as the human-readable narrative:
- Line 1: a single-sentence headline.
- `## What I did` — grouped, each with the WHY (cleanup, vault maintenance, repo work).
- `## Estate audit` — per-area findings (home/code, repos, vault, Windows, Pis, security, sites).
- `## Recommendations & ideas` — your brainstorm: new ideas, software recs, security fixes, quick wins.
- `## Proposals I did not act on` — risky/remote/outward things left for you, with the exact steps.
  (Every item here should ALSO have a `ml_todo.py add` entry — the summary is prose, the todos are
  the machine-readable approve-list.)
Then stop and wait at the prompt. Do not start new work after writing the summary.
"""


def build_scheduled_mission(cfg, run_dir, scheduled_text, dry_run, five_now, five_target, weekly_now,
                            weekly_cap, reserve, wallclock_min):
    """Compose a one-off scheduled brief inside Moonlighter's normal safety rails."""
    off = cfg.get("off_limits_resolved", [])
    offl = "\n".join(f"  - {p}" for p in off)
    mlfs = HERE / "ml_fs.py"
    mltodo = HERE / "ml_todo.py"
    branch = f"moonlighter/{run_dir.name}"
    mode_block = (
        "## MODE: DRY RUN (observe) — TOUCH NOTHING\n"
        "This is a dry run. Do NOT modify, move, create, or delete ANY file outside this run dir.\n"
        "Investigate the scheduled task and PROPOSE what you would do. The only file you may\n"
        "write is `$ML_RUN_DIR/summary.md`.\n"
        if dry_run else
        "## MODE: FULL-AUTO — you may act, but only via the helper below\n"
        f"EVERY filesystem mutation MUST go through the helper so it is revertible:\n"
        f"  python3 {mlfs} write-begin <path>   # BEFORE editing a file in place\n"
        f"  python3 {mlfs} move <src> <dst>     # to move/rename\n"
        f"  python3 {mlfs} trash <path>         # to 'delete' (moves into the run trash)\n"
        f"  python3 {mlfs} created <path>        # AFTER creating a new file\n"
        f"  python3 {mlfs} note \"<text>\"        # to log a decision/non-fs action\n"
        "NEVER use mv/rm or write files in place without the matching helper call first.\n"
        f"For any git repo: create & checkout a branch `{branch}` first, commit there, "
        "NEVER push, NEVER touch the working branch.\n"
    )
    return f"""# Moonlighter scheduled task — {run_dir.name}

You are Moonlighter: Claude working autonomously in the user's idle hours. This is a
one-off scheduled task, not the broad nightly estate audit. Do ONLY the scheduled task
below, while obeying every normal Moonlighter safety rule.

{mode_block}
## SCHEDULED TASK — user-authored brief
{scheduled_text.strip()}

## Spare-capacity budget
Stop cleanly when whichever of these ceilings is hit first:
- the 5-hour window reaches ~{five_target:.0f}% (it is at {five_now:.0f}% right now), OR
- weekly usage reaches {weekly_cap:.0f}% (it is at {weekly_now:.0f}% now — the {reserve:.0f}% reserve is left for the user), OR
- the wall-clock cap ({wallclock_min} minutes).

## OFF-LIMITS — never read, touch, or even open
{offl}
  - ...and ANY credential, secret, key, token, password store, or .env file. Skip silently.

## HARD RULES (non-negotiable)
- Nothing outward-facing, EVER: no git push, no deploys, no emails/messages, no calendar events,
  no publishing API calls, no installs that phone home. Unattended ⇒ no irreversible outward acts.
- Stay within the scheduled task scope and selected Work root. Treat paths outside that validated
  Work root as audit-only, even if the user-authored brief explicitly names them.
- In full-auto, every local filesystem mutation must stay inside the validated Work root and must
  go through the revertible helper above.
- In observe mode, touch nothing outside this run dir even if the scheduled brief asks for changes.
- No real deletes: "delete" means `ml_fs.py trash` (revertible). Snapshot before editing any file.
- Log every mutating action to the manifest via the helper BEFORE doing it.
- Anything risky, ambiguous, outward-facing, remote, or off-scope: do NOT act — record it as a proposal.
- Secrets / credentials / keys / .env: never read or open. Skip silently.

## When you finish (or are told to stop)
FIRST register every OUTSTANDING actionable item — anything you did NOT complete that the user
might want done — via the helper (do NOT hand-write JSON):
  python3 {mltodo} add --title "<short imperative>" --detail "<why/what, 1-2 sentences>" \
      [--cmd "<a command>" ...] --category security|backup|cleanup|hygiene|idea \
      --risk low|medium|high [--needs-sudo] [--needs-push --repo <name>]

THEN write `$ML_RUN_DIR/summary.md` (the run dir is {run_dir}) as the human-readable narrative:
- Line 1: a single-sentence headline.
- `## What I did` — what you did and why.
- `## Skipped / proposals` — anything you did not act on, with the reason.
Then stop and wait at the prompt. Do not start new work after writing the summary.
"""


def build_apply_mission(cfg, run_dir, tasks):
    """Focused mission: do ONLY the user-approved items from the night report."""
    mlfs = HERE / "ml_fs.py"
    mlask = HERE / "ml_ask.py"
    mltodo = HERE / "ml_todo.py"
    off = "\n".join(f"  - {p}" for p in cfg.get("off_limits_resolved", []))
    tasklist = "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))
    return f"""# Moonlighter — APPLY approved items ({run_dir.name})

The user reviewed the night report and **approved the specific items below**. Do ONLY
these — nothing else, no broad sweep. Work as a director; delegate mechanical parts to
Sonnet sub-agents if helpful.

## FIRST — ask clarifying questions (the user is reachable)
Before acting, read the approved items and decide if anything is ambiguous, risky, or has
options (which file to keep, how aggressive, a path to confirm). If so, ASK the user — they
can answer live from the panel:
  python3 {mlask} "your clear, specific question"
It prints the user's answer (or a 'no answer' note after ~4 min — then use best judgment and
note the assumption). Ask up front, batch related questions, and ask again mid-task if needed.
Don't ask about trivially-safe items — just do those.

## How to act (every filesystem mutation MUST be revertible)
  python3 {mlfs} write-begin <path>   # BEFORE editing a file in place
  python3 {mlfs} move <src> <dst>     # move/rename
  python3 {mlfs} trash <path>         # 'delete' = move to run trash
  python3 {mlfs} created <path>        # after creating a file
  python3 {mlfs} note "<text>"        # log a decision / non-fs action (e.g. a chmod, with its revert)
For a shell command an item gives you (e.g. a cache clean), run it, then `note` what you ran.

## RULES — APPROVAL IS PERMISSION. Complete the approved items; don't make the user approve again.
- The user EXPLICITLY ticked each item below. That is your consent — DO it. Do NOT refuse an
  approved item on "I wouldn't do this unattended" grounds; that caution is for the autonomous
  nightly sweep, NOT for things the user hand-approved.
- This INCLUDES things the nightly run would skip: editing dotfiles/config the user approved
  (e.g. moving secrets out of `~/.bashrc` into a chmod-600 sourced file), scheduling cron in the
  USER's own crontab, setting up off-box backup, installing USER-LEVEL tools
  (`pip install --user`, `pipx`, `npm -g` if the prefix is writable, `cargo install`).
- Make every change via the helper so it stays revertible (write-begin before edits; `note` the
  exact revert for chmod/cron/crontab changes). Keep reversibility — but DO the work.
- NEED INFO to complete an item (a path, a destination like the backup target, which of two
  options)? ASK via ml_ask and wait — do NOT skip for lack of info.
- **sudo: do the user-level part, DEFER the root-only part — NEVER ask for or handle the password.**
  Moonlighter must never touch the user's sudo password (it would end up in this run's transcript).
  So if an approved item needs root (apt install, chmod on a root-owned file, a system service):
  first do everything you CAN at user level (`pip install --user`/`pipx`/`npm -g`/`cargo install`,
  user-owned files). For the genuinely root-only remainder, do NOT run sudo and do NOT prompt for a
  password — instead re-register it as a carry-forward todo with the EXACT command:
    `python3 {mltodo} add --title "<do X> (needs your sudo)" --detail "user-level part done; this
        step needs root" --cmd "sudo <exact command>" --category <cat> --needs-sudo`
  and note it in the summary under "needs your sudo". The user runs that one command themselves.
  (`{mlask}` is ONLY for non-secret clarifications like "keep file A or B" — never a password.)
- **An approved item's OWN required push IS authorised.** Some approved items inherently need an
  outward git action — e.g. shrinking a repo's `.git` (LFS migrate / filter-repo) is pointless
  without `git push --force`, and "make repo X private / untrack a file" needs the GitHub API.
  Approval covers that specific push/visibility change ON THE REPO THE ITEM NAMES. Take a safety
  bundle first (`git bundle create … --all`), `note` it, then push. This is NOT a policy exception
  to grant yourself broadly — only the push the approved item cannot be completed without.
- ONLY skip if GENUINELY impossible, and never on vague "unattended" caution:
    (a) needs root → user-level part done, root-only part deferred as a `--needs-sudo` carry-forward
        todo with the exact `sudo …` command (above); never prompt for the password;
    (b) blocked by a transient lock / another running process → retry once, else note it;
    (c) the item is not actually a task (a status line / something already done) → note that and move on.
- STILL off-limits even when approved (these are never what an item "needs"):
    * DEPLOYING to hosting/production — Cloudflare Pages/Workers, Vercel, Netlify, `wrangler deploy`,
      `gh workflow run` deploy pipelines, pushing to a *live-site* branch. (A normal `git push` to a
      code repo's remote for the approved item is fine; shipping to a live website is not.)
    * SENDING to other people/services — emails, WhatsApp/Slack/Discord messages, calendar invites,
      outreach. Internal notifications to YOURSELF (desktop toast) are fine.
    * the literal secret STORES below; anything NOT in the approved list.
- OFF-LIMITS paths (never touch):
{off}

## APPROVED ITEMS — do each, in order
{tasklist}

## When done
1. For any approved item you genuinely COULD NOT finish (sudo refused, transient lock, truly
   blocked), re-register it so it carries forward and stays on the approve-list — do NOT hand-write
   JSON:
     python3 {mltodo} add --title "<short imperative>" --detail "<what's left + why blocked>" \\
         [--cmd "<command>" ...] --category <cat> --risk <low|medium|high> [--needs-sudo] [--needs-push --repo <name>]
   Do NOT register items you completed (those are tracked for revert). If you finished everything,
   register nothing.
2. Write `$ML_RUN_DIR/summary.md`: line 1 = one-sentence headline; then `## Done` (per item, what
   you did + the WHY) and `## Skipped` (any you didn't, with the reason). Then stop.
"""


def _read_budget_env(cfg):
    bucket = os.environ.get("ML_ACTIVE_BUCKET", "seven_day")
    away = os.environ.get("ML_AWAY_HOURS")
    away = float(away) if away else None
    wallclock = os.environ.get("ML_WALLCLOCK_MIN")
    wallclock = int(wallclock) if wallclock else None
    five_target = os.environ.get("ML_FIVE_TARGET")
    five_target = float(five_target) if five_target else None
    return bucket, away, wallclock, five_target


def _read_mission_file_env():
    mission_file = os.environ.get("ML_MISSION_FILE")
    if not mission_file:
        return None
    return pathlib.Path(mission_file).read_text(encoding="utf-8")


def _supervise(cfg, run_dir, summary_path, hard_deadline, bucket, five_target, weekly_cap):
    """Wait loop that supervises the live tmux session until it should stop.

    Returns the stop_reason string. Extracted from main() so it can be driven
    directly in tests without a real tmux session.
    """
    stop_reason = "completed"
    prev_pane = None
    idle = 0
    last_budget_check = time.time()

    while True:
        if not _session_alive():
            stop_reason = "session ended"
            break
        if summary_path.exists() and summary_path.stat().st_size > 0:
            # session signalled completion; give it a moment then finish
            time.sleep(5)
            stop_reason = "completed"
            break

        # If the agent is waiting on a clarifying question, it's legitimately paused —
        # don't count idle / nudge / kill until the user answers (or ml_ask times out).
        if (run_dir / "ask.json").exists():
            idle = 0
            prev_pane = None
        else:
            pane = _capture()
            if pane == prev_pane:
                idle += 1
            else:
                idle = 0
                prev_pane = pane
            if idle >= IDLE_CONFIRMATIONS:
                if idle == IDLE_CONFIRMATIONS:
                    subprocess.run(["tmux", "send-keys", "-t", TMUX,
                                    "If you are done, write $ML_RUN_DIR/summary.md now and stop.",
                                    "Enter"])
                if idle >= IDLE_CONFIRMATIONS * 3:
                    stop_reason = "idle"
                    break

        # Switched off from the panel/CLI — check every iteration (not
        # gated behind BUDGET_CHECK_SEC) so "off" takes effect promptly.
        if cfg["kill_switch_path"].exists():
            stop_reason = "switched off from panel"
            _graceful_stop("Switched off from the panel")
            break

        now = time.time()
        if datetime.datetime.now() >= hard_deadline:
            stop_reason = "wall-clock cap"
            _graceful_stop("Wall-clock cap reached")
            break
        if now - last_budget_check >= BUDGET_CHECK_SEC:
            last_budget_check = now
            try:
                un = usage_api.get_usage(force=True)  # real reading for the safety stop
                five = float((un.get("five_hour") or {}).get("utilization") or 0.0)
                cur = float((un.get(bucket) or {}).get("utilization") or 0.0)
                # Spare-capacity stops: 5h window filled to target, or weekly reserve reached.
                if five >= five_target:
                    stop_reason = f"5h window filled to target ({five:.0f}% ≥ {five_target:.0f}%)"
                    _graceful_stop("Budget reached")
                    break
                if cur >= weekly_cap:
                    stop_reason = f"weekly reserve reached ({cur:.0f}% ≥ {weekly_cap:.0f}%)"
                    _graceful_stop("Budget reached")
                    break
            except Exception:
                pass
        time.sleep(POLL)

    return stop_reason


def main():
    cfg = cfgmod.load()
    state.ensure_dirs()
    # Switched off wins over every launch path. The supervisor also checks this, but
    # only once the tmux session exists and the mission has been sent — by then the
    # agent can already have spent quota and acted. Refuse before launching anything.
    # This is the only gate `run.sh` passes through: `moonlight start` and /api/start
    # check the kill switch themselves, but /api/apply and one-off env-override
    # launchers invoke run.sh directly and would otherwise bypass OFF entirely.
    if cfg["kill_switch_path"].exists():
        print("Moonlighter is switched off. Turn it on from the panel "
              "or run `moonlight resume` first.")
        state.gate_log("runner: refused to launch — switched off (kill switch present)")
        return 1
    if not shutil.which("tmux"):
        print("Moonlighter's autonomous engine needs `tmux` (a real, capturable Claude session).\n"
              "  • Linux/macOS:  install tmux (apt/brew install tmux), then retry.\n"
              "  • Windows:      run Moonlighter inside WSL2 — `wsl --install` in PowerShell,\n"
              "                  then install Claude Code + tmux in the Ubuntu shell and run there.\n"
              "See the README / CLAUDE.md 'Windows' section; your AI setup agent can do this for you.")
        return 1
    if _session_alive():
        print("A Moonlighter session is already running. Aborting.")
        return 1

    dry_run = cfg.get("mode", "observe") != "full-auto"
    bucket, away_hours, wallclock_override, five_target_override = _read_budget_env(cfg)
    wallclock_min = int(cfg.get("max_wallclock_min", 360))
    if wallclock_override is not None:
        wallclock_min = wallclock_override
    night_model = os.environ.get("ML_NIGHT_MODEL") or cfg.get("night_model", "default")
    # The quota bucket must match the model actually launched: a Sonnet session draws the
    # Sonnet weekly pool, anything else the general one. Derive it authoritatively from the
    # EFFECTIVE night_model (which honours an ML_NIGHT_MODEL override) rather than trusting the
    # ML_ACTIVE_BUCKET that a gate/launcher computed from cfg before it saw the override — an
    # explicit seven_day must not mask a Sonnet run. Match ANY Sonnet model, not just the bare
    # "sonnet" keyword: the arbitrary-model passthrough below can launch an explicit Sonnet
    # model id, which still draws the Sonnet pool. Mirrors gate.active_bucket_name().
    bucket = "seven_day_sonnet" if "sonnet" in (night_model or "").lower() else "seven_day"
    five_target = float(cfg.get("five_hour_target_pct", 80))
    if five_target_override is not None:
        five_target = five_target_override
    reserve = float(os.environ.get("ML_RESERVE") or cfg.get("weekly_reserve_pct", 10))
    weekly_cap = 100.0 - reserve

    # usage before
    try:
        u0 = usage_api.get_usage(force=True)
    except Exception as exc:
        state.gate_log(f"runner: usage read failed at start: {exc}")
        u0 = {}
    util0 = float((u0.get(bucket) or {}).get("utilization") or 0.0)
    sonnet0 = float((u0.get("seven_day_sonnet") or {}).get("utilization") or 0.0)
    seven0 = float((u0.get("seven_day") or {}).get("utilization") or 0.0)
    five0 = float((u0.get("five_hour") or {}).get("utilization") or 0.0)

    # Determine whether this run will actually spend BEFORE the launch guard: applying
    # approved items always acts, regardless of the config mode.
    mission_override = _read_mission_file_env()
    apply_file = os.environ.get("ML_APPLY_TASKS")
    apply_mode = bool(apply_file and pathlib.Path(apply_file).exists())
    if apply_mode:
        dry_run = False  # applying approved items always acts

    # Refuse before launching if the weekly bucket is already at/over the effective cap. The
    # in-run supervisor only checks this after BUDGET_CHECK_SEC (~5 min), so a run started with
    # no room — e.g. an ML_RESERVE stricter than the launcher's preflight used — would spend
    # quota the override said was unavailable. Observe runs still start a real Claude survey
    # session and spend quota, so the cap applies to EVERY launch, dry-run or not.
    if u0 and util0 >= weekly_cap:
        state.gate_log(f"runner: refusing to launch — {bucket} at {util0:.0f}% already "
                       f"≥ weekly cap {weekly_cap:.0f}% (reserve {reserve:.0f}%)")
        return 0

    rid, run_dir = state.new_run_dir()
    started = datetime.datetime.now()
    started_ts = started.timestamp()

    if apply_mode:
        tasks = [t for t in pathlib.Path(apply_file).read_text(encoding="utf-8").split("\n\x1e") if t.strip()]
        mission = build_apply_mission(cfg, run_dir, tasks)
    elif mission_override is not None:
        mission = build_scheduled_mission(cfg, run_dir, mission_override, dry_run, five0,
                                          five_target, util0, weekly_cap, reserve,
                                          wallclock_min)
    else:
        try:
            prior = digestmod.prior_brief()   # what earlier runs tonight already covered
        except Exception:
            prior = ""
        mission = build_mission(cfg, run_dir, dry_run, five0, five_target, util0,
                                weekly_cap, reserve, away_hours, wallclock_min, prior=prior)
    (run_dir / "mission.md").write_text(mission, encoding="utf-8")
    (run_dir / "manifest.jsonl").touch()

    run_meta = {
        "id": rid, "date_human": started.strftime("%B %d"),
        "started": started.isoformat(), "mode": cfg.get("mode"),
        "dry_run": dry_run, "status": "running",
        "five_target_pct": five_target, "weekly_cap_pct": weekly_cap,
        "active_bucket": bucket, "util_before": util0,
        "five_before": five0,
        "seven_day_before": seven0, "seven_day_sonnet_before": sonnet0,
        "night_model": night_model, "manual": away_hours is not None,
        "apply": apply_mode,
    }
    (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    state.gate_log(f"runner: launching {rid} (dry_run={dry_run}, fill 5h to {five_target:.0f}%, "
                   f"weekly cap {weekly_cap:.0f}%)")

    # --- launch tmux interactive session ---
    # Launch through a login shell (full PATH under cron) with Claude-injected env
    # scrubbed (so it's a normal top-level session that writes its transcript), in a
    # dedicated cwd (so that transcript lands in OWN_PROJECT_DIR).
    SESSION_CWD.mkdir(parents=True, exist_ok=True)
    scrub = "env " + " ".join(f"-u {v}" for v in SCRUB_ENV)
    claude_inner = f"{scrub} claude --dangerously-skip-permissions"
    if night_model == "sonnet":
        claude_inner += " --model sonnet"
    elif night_model == "fable":
        claude_inner += " --model claude-fable-5"
    elif night_model not in ("default", ""):
        # Arbitrary model passthrough (explicit model id).
        claude_inner += f" --model {shlex_quote(night_model)}"
    claude_cmd = f"exec bash -lc {shlex_quote(claude_inner)}"
    subprocess.run(["tmux", "kill-session", "-t", TMUX],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", TMUX, "-x", "220", "-y", "50",
         "-c", str(SESSION_CWD),
         "-e", f"ML_RUN_DIR={run_dir}", claude_cmd],
        check=True,
    )

    # wait for TUI ready
    deadline = time.time() + 90
    ready = False
    while time.time() < deadline:
        pane = _capture()
        if ">" in pane or "Welcome" in pane or "│" in pane:
            ready = True
            break
        time.sleep(POLL)
    if not ready:
        state.gate_log(f"runner: TUI never became ready for {rid}")

    # deliver mission
    subprocess.run(["tmux", "send-keys", "-t", TMUX,
                    f"Read {run_dir/'mission.md'} and execute it.", "Enter"])
    time.sleep(2)
    # second Enter in case the first only submitted the text into the box
    subprocess.run(["tmux", "send-keys", "-t", TMUX, "Enter"])

    # --- supervise ---
    hard_deadline = started + datetime.timedelta(minutes=wallclock_min)
    summary_path = run_dir / "summary.md"
    stop_reason = _supervise(cfg, run_dir, summary_path, hard_deadline, bucket,
                              five_target, weekly_cap)

    # --- wrap up ---
    # Everything up to the `finally` below is best-effort bookkeeping: a failed
    # pane capture, transcript write, token accounting or calibration append
    # must NEVER prevent finalisation — revert.sh and the run report are the
    # user's only handles on what the run did, so they are ALWAYS generated.
    finished = datetime.datetime.now()
    util_delta = 0.0
    tokens_spent = 0
    try:
        pane_final = _capture()
        (run_dir / "transcript.txt").write_text(pane_final, encoding="utf-8")

        try:
            u1 = usage_api.get_usage(force=True)
        except Exception:
            u1 = {}
        util1 = float((u1.get(bucket) or {}).get("utilization") or util0)
        seven1 = float((u1.get("seven_day") or {}).get("utilization") or seven0)
        sonnet1 = float((u1.get("seven_day_sonnet") or {}).get("utilization") or sonnet0)
        five1 = float((u1.get("five_hour") or {}).get("utilization") or five0)
        session_tx = _session_transcripts(started_ts)
        tokens_spent = _sum_tokens(session_tx)

        # let session settle before the finally-block kill
        time.sleep(3)

        util_delta = max(util1 - util0, 0.0)
        run_meta.update({
            "status": "observed" if dry_run else "clean",
            "finished": finished.isoformat(),
            "duration_min": round((finished - started).total_seconds() / 60.0, 1),
            "stop_reason": stop_reason,
            "util_after": util1, "util_delta": round(util_delta, 2),
            "five_after": five1, "five_delta": round(max(five1 - five0, 0.0), 2),
            "seven_day_after": seven1, "seven_day_sonnet_after": sonnet1,
            "tokens_spent": tokens_spent,
            "spend_pct": round(util_delta, 2),
            "tokens": tokens_spent,
        })

        # headline from summary.md line 1
        headline = "Dry run — proposed actions, touched nothing" if dry_run else "Run complete"
        if summary_path.exists():
            first = summary_path.read_text(encoding="utf-8").strip().splitlines()
            if first:
                headline = first[0].lstrip("# ").strip() or headline
        run_meta["headline"] = headline
        (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")

        # calibration (only meaningful when something was actually spent)
        if util_delta > 0 and tokens_spent > 0:
            state.append_calibration({
                "run": rid, "primary_bucket": bucket,
                "tokens_spent": tokens_spent, "util_delta": util_delta,
                "seven_day_before": seven0, "seven_day_after": seven1,
                "seven_day_sonnet_before": sonnet0, "seven_day_sonnet_after": sonnet1,
            })
    except Exception as exc:
        state.gate_log(f"runner: wrap-up bookkeeping failed for {rid}: {exc!r} — finalising anyway")
        run_meta.update({
            "status": "wrapup-error",
            "finished": finished.isoformat(),
            "duration_min": round((finished - started).total_seconds() / 60.0, 1),
            "stop_reason": stop_reason,
            "wrapup_error": repr(exc),
        })
        try:
            (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
        except Exception:
            pass
    finally:
        # kill the session even when bookkeeping blew up
        subprocess.run(["tmux", "kill-session", "-t", TMUX],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # revert.sh + report + notify — each attempted independently so a
        # failure in one can never suppress the other
        finalisation_failures = []
        try:
            revertmod.write_revert_script(run_dir)
        except Exception as exc:
            state.gate_log(f"runner: write_revert_script failed for {rid}: {exc!r}")
            finalisation_failures.append(f"write_revert_script: {exc!r}")
        if finalisation_failures:
            # The morning report renders from run_meta — annotate BEFORE
            # writing it, or it claims a clean, fully-revertible run while
            # revert.sh is missing.
            run_meta["status"] = "finalisation-error"
            run_meta["finalisation_errors"] = list(finalisation_failures)
        try:
            reportmod.write_report(cfg, run_dir, run_meta)
        except Exception as exc:
            state.gate_log(f"runner: write_report failed for {rid}: {exc!r}")
            finalisation_failures.append(f"write_report: {exc!r}")

    if finalisation_failures:
        # A run without its revert.sh or morning report is NOT a clean run:
        # record what is missing in run.json (best-effort) and fail the
        # process so the gate log and the panel show a failed run instead of
        # a silently incomplete "clean" one.
        run_meta["status"] = "finalisation-error"
        run_meta["finalisation_errors"] = finalisation_failures
        try:
            (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
        except Exception:
            pass
        state.gate_log(f"runner: {rid} finished with finalisation errors — "
                       + "; ".join(finalisation_failures))
        return 1

    state.gate_log(f"runner: {rid} finished — {stop_reason}; spent {util_delta:.2f}% / {tokens_spent} tok")
    return 0


def _graceful_stop(why="Budget reached"):
    subprocess.run(["tmux", "send-keys", "-t", TMUX,
                    f"{why} — stop now, leave everything in a clean state, "
                    "and write $ML_RUN_DIR/summary.md.", "Enter"])
    time.sleep(120)


if __name__ == "__main__":
    sys.exit(main())
