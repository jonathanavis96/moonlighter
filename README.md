<div align="center">

# ◖ Moonlighter

**Claude, working the night shift.**

Moonlighter runs Claude Code autonomously in the hours you're *not* using it — tidying,
auditing, and improving your machine while you sleep — using only the subscription capacity
you'd otherwise waste, and leaving a morning report of everything it did and everything it suggests.

</div>

---

## Why

Your Claude usage limit refills on a rolling window whether you use it or not. Overnight, that
capacity is just **burned**. Moonlighter spends *only the slack* — it fills the idle window and
stops well before it could touch what you've reserved for your own working hours.

While it's at it, it does the boring, valuable housekeeping you never get to:

- 🧹 **Tidies** — loose files, duplicates, stale caches and build output, dead artifacts
- 🔧 **Repo hygiene** — uncommitted work, `.gitignore` gaps, accidentally-tracked secrets, bloat
- 🔒 **Security sweep** — world-readable secrets, loose permissions, exposed ports, stale packages
- 📝 **Notes upkeep** — optional Obsidian/markdown vault maintenance (broken links, stale statuses)
- 🖥️ **Device health** — optional read-only audit of other machines you can SSH to
- 💡 **Ideas & proposals** — opinionated suggestions, tooling recommendations, quick wins

Every morning you get one clean report: what it **did** (one-click undo on anything), and a
checklist of bigger things it **proposes** — tick what you want, and it does exactly those.

## Safe by design

- **Everything is reversible.** Every change is snapshotted; per-item and whole-run undo.
- **Never outward-facing.** No `git push`, no deploys, no emails/messages — unattended runs only
  do local, reversible work. (Things you explicitly approve can do the one push/sudo they need.)
- **Your secrets are untouchable.** `~/.ssh`, `~/.aws`, GPG, credential stores and `.env` files
  are hard-blocked in code, no matter what you configure.
- **It can't blow your budget.** You set a weekly reserve; it always stops short of it.
- **PIN-gated.** Every action needs your PIN, even though the panel is only on your private network.

---

## Quick start

> **Prerequisites:** [Claude Code](https://claude.com/claude-code) installed and logged in
> (a Pro/Max subscription), **Python 3.9+**, and **tmux**.
> **On Windows:** run Moonlighter inside **WSL2** (see [Platform support](#platform-support)) —
> or just let your AI agent set it up for you (below).

```bash
git clone https://github.com/<you>/moonlighter.git
cd moonlighter
./moonlight ui          # starts the control panel
```

Open the panel at **http://127.0.0.1:8377** — on first run it walks you through a short
**setup wizard**: budget, the folders it may work in (a visual folder picker), optional
vault/devices, notifications, and your PIN. That's it.

Flip it from *observe* (dry-run) to *full-auto* whenever you're ready, and it'll start filling
idle windows. Watch a run live, read the morning report, and approve or undo from the panel.

### …or let your AI set it up

This repo ships with a setup playbook for AI agents. In **Claude Code**, just point it here:

```
Set up Moonlighter for me from https://github.com/<you>/moonlighter — follow its CLAUDE.md.
```

The agent reads [`CLAUDE.md`](CLAUDE.md), detects your OS, installs what's missing (including
WSL2 on Windows), starts the panel, and hands you the setup wizard. Paste the link, answer a
couple of questions, done.

---

## Platform support

| Platform | Status | Notes |
|---|---|---|
| **Linux** | ✅ Full | Native. |
| **macOS** | ✅ Full | `brew install tmux`. |
| **Windows (via WSL2)** | ✅ Full | The supported Windows path — Claude Code runs great in WSL2; the AI setup agent can install it for you with one command. |
| **Windows (native, no WSL)** | 🧪 Experimental | The panel/wizard are pure-Python and run anywhere, but the autonomous engine needs a PTY-backed `claude` session (tmux). A native backend (Task Scheduler + PowerShell) is on the roadmap; for now the AI setup agent will steer you to WSL2. |

## How it works

- A small **gate** decides when to run (idle window, recent activity, your budget) and launches a
  **real, interactive `claude` session** inside tmux — so spend draws your normal subscription
  quota, not a separate credit pool. It supervises the session live and stops at your ceiling.
- The session works to a **mission** built from *your* config (your folders, your devices) — so
  it's never hardcoded to one machine. Every file change goes through a reversible helper that
  records a manifest + snapshot.
- When done it writes a **report** and registers any outstanding work as structured **to-do
  items**. The panel shows the morning digest; you tick items to *do* or *undo*.
- A **control panel** (stdlib Python, no dependencies) gives you the dashboard, the live run feed,
  the report, the approve/undo checklists, and the setup wizard.

## Configuration

You normally never hand-edit config — the wizard writes your choices to
`~/.moonlighter/config.local.yaml`, which layers over the documented defaults in
[`config.yaml`](config.yaml). Re-run setup any time from the panel (**⚙ Reconfigure**).

State (reports, run snapshots, your config overlay, usage log) lives in `~/.moonlighter/` —
outside the repo, so updating Moonlighter never touches your data.

## CLI

```bash
./moonlight ui        # start the control panel
./moonlight status    # current state + budget
./moonlight start     # run now (respects your budget)
./moonlight mode full-auto | observe
./moonlight log       # recent activity
./moonlight attach    # watch the live tmux session
```

## License

[MIT](LICENSE). Provided as-is — you are responsible for what you let it run on your machine.
Read the safety model above; start in *observe* mode until you trust it.
