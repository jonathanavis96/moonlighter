# Setting up Moonlighter — instructions for the AI agent

You are an AI coding agent (e.g. Claude Code) helping a user install and run **Moonlighter** on
**their own machine**. The user likely pasted this repo's URL and asked you to set it up. Work
through the steps below, adapting to their OS. Be concise with the user, verify each step rather
than assuming, and never fabricate success — if something fails, say so and fix it.

**What Moonlighter is** (so you can explain it): it runs Claude Code autonomously during the
user's idle hours to tidy/audit/improve their machine, using only spare subscription capacity,
fully reversibly, with a morning report. The README has the full pitch and the safety model.

## Ground rules

- **This is the user's machine.** Don't touch anything outside the repo and `~/.moonlighter/`
  without telling them. Installing system packages (tmux, WSL) is fine *with a heads-up*.
- **Don't invent config.** The setup **wizard** (a GUI) collects the user's budget, folders,
  devices, and PIN. Your job is to get the panel running and hand them the wizard — not to guess
  their folders or set a PIN for them.
- **Verify, don't assume.** Run the check commands. Report real output.

## Step 1 — Detect the OS and platform

```bash
uname -a 2>/dev/null; echo "---"; cat /proc/version 2>/dev/null
```

- **Linux** (not WSL) → continue at Step 2.
- **macOS** (`uname` = Darwin) → continue at Step 2.
- **WSL2** (`/proc/version` contains `microsoft`) → continue at Step 2; this IS the supported
  Windows path.
- **Native Windows** (you're in PowerShell/cmd, no WSL) → go to **Step 1a** first.

### Step 1a — Native Windows: move into WSL2

Moonlighter's autonomous engine needs a PTY-backed `claude` session, which on Windows means WSL2.
This is the supported, reliable path — set it up:

1. Check for WSL: `wsl --status`. If absent, install it (admin PowerShell): `wsl --install`
   (installs Ubuntu; a reboot may be needed). Tell the user what's happening.
2. Once in the Ubuntu/WSL shell, install Claude Code there and log in (`claude` → `/login`), then
   clone this repo *inside* WSL (under the Linux home, e.g. `~/moonlighter`, **not** `/mnt/c`).
3. Continue Step 2 inside WSL.

> If the user flatly cannot use WSL: the **panel and setup wizard still run** under native Windows
> Python (`python panel\server.py`), so they can configure and read reports — but scheduled
> autonomous runs won't work until a native engine exists. Be honest about this; don't pretend
> the night runs will fire. Offer WSL again.

## Step 2 — Prerequisites

Check and install what's missing (use the user's package manager; ask before installing):

```bash
python3 --version          # need 3.9+
command -v tmux            # required for the autonomous engine (apt/brew install tmux)
command -v claude          # Claude Code CLI must be installed AND logged in
python3 -c "import yaml" 2>/dev/null && echo "pyyaml ok" || echo "need pyyaml"
```

- **tmux** missing → `sudo apt install tmux` (Debian/Ubuntu) or `brew install tmux` (macOS).
- **pyyaml** missing → `pip install pyyaml` (or `sudo apt install python3-yaml`). It's the only
  Python dependency; everything else is stdlib.
- **claude** missing or not logged in → have the user install Claude Code and run `claude` once to
  log in. Confirm with: `claude --version`. (Moonlighter draws their normal subscription quota.)

## Step 3 — Make the launcher runnable

```bash
chmod +x moonlight run.sh
./moonlight status      # should print state without error
```

Optionally symlink it onto their PATH so they can run `moonlight` from anywhere:
`ln -s "$(pwd)/moonlight" ~/.local/bin/moonlight` (ensure `~/.local/bin` is on PATH).

## Step 4 — Phone / remote access (optional, ask)

By default the panel binds `127.0.0.1` (this machine only). If the user wants to reach it from
their phone, the safe option is **Tailscale** (a private mesh VPN, nothing public):

- Install Tailscale on this machine and the phone, same account.
- In the setup wizard (next step) or `config.yaml`, set `bind_host: 0.0.0.0`.
- They open `http://<this-machine's-tailscale-ip>:8377` on the phone.
- **WSL caveat:** WSL is NAT'd behind Windows, so also add a Windows port-proxy (admin
  PowerShell): `netsh interface portproxy add v4tov4 listenport=8377 connectaddress=<WSL-eth0-ip>`
  (get the IP with `ip addr show eth0`). The tailnet and WSL IPs can change.

Don't expose the panel to the public internet. The PIN protects actions, but keep it on a private
network.

## Step 5 — Start the panel and run the wizard

```bash
./moonlight ui          # starts the control panel (foreground) — or run it backgrounded
```

Tell the user to open **http://127.0.0.1:8377** (or their Tailscale URL). Because `setup_complete`
is false on a fresh install, it redirects to the **setup wizard** at `/setup`. Walk them through
it if they want, but let *them* make the choices:

1. **Budget & mode** — how much spare capacity to use; full-auto vs review-only.
2. **Folders** — a visual browser to pick what it may work in, and what's off-limits.
3. **Vault** — optional notes folder.
4. **Devices** — optional backup destination + read-only audit hosts (most people: none).
5. **Notifications** — desktop toast / vault log / ntfy.
6. **PIN** — they set it.
7. **Review & finish** — writes `~/.moonlighter/config.local.yaml`.

After finishing, the panel is live. Suggest they **leave it in `observe` (dry-run) mode for the
first night** so they can read a report before trusting it to act, then flip to `full-auto`.

## Step 6 — Scheduling (so it runs on its own)

For autonomous overnight runs, schedule **the gate** — `lib/gate.py`, not `moonlight start`. The
gate is the thing that decides whether to run, based on the idle window, recent activity and
budget; it returns without launching unless the verdict is GO. `moonlight start` is the *manual*
"I'm away, go now" path and deliberately **skips** the idle-window and activity checks, so cronning
it would spend quota every 30 minutes even while the user is at the keyboard.

```bash
# check every 30 min, around the clock
crontab -l 2>/dev/null | { cat; echo "*/30 * * * * cd $(pwd) && python3 lib/gate.py >/dev/null 2>&1"; } | crontab -
```

Run it on **every** hour, not just overnight: the gate also samples usage on each tick, and that
history is what the idle-window discovery and the weekly forecast are built from. Restricting it
to a fixed night window starves both — the gate works out the user's idle hours on its own.

On WSL, ensure the distro runs at boot (or that the user opens a WSL shell), since WSL cron only
runs while the distro is up.

> **Verify cron is actually running** before relying on it (`systemctl status cron` or
> `service cron status`; start/enable if needed).

## Step 7 — Smoke test

```bash
./moonlight start       # kick a run now; it respects the budget
./moonlight attach      # watch the live session (Ctrl-b then d to detach)
```

Then open the panel → the run appears with a live feed, and a report is written when it finishes.
Confirm the morning report renders and the approve/undo checklists work. You're done — summarise
for the user what you set up, the panel URL, the mode it's in, and how to reconfigure (the
**⚙ Reconfigure** link in the panel).

## Troubleshooting

- **`tmux` not found** → install it; the engine requires it. (Native Windows → use WSL.)
- **Panel won't load / port in use** → another process on 8377; change `ui_port` in `config.yaml`.
- **Wizard's folder browser is empty / 403** → it's PIN-gated once configured; on first run it's
  open. After setup, the panel prompts for the PIN.
- **No transcript / token count stuck at 0** → the session must be a *top-level* Claude session;
  Moonlighter already scrubs the `CLAUDECODE*`/`AI_AGENT` env vars for this. Confirm `claude` is
  logged in and runs interactively in a plain shell.
- **Runs never fire on schedule** → cron isn't running, or the window doesn't match when the
  machine is on/idle. Check `./moonlight status` and the cron service.
