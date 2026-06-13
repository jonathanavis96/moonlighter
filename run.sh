#!/usr/bin/env bash
# run.sh — Moonlighter runner entry point. Drives a REAL interactive Claude Code
# session in tmux (never headless), so it draws the user's normal subscription
# quota. Invoked by the gate (cron) or `moonlight start`. Delegates to runner.py.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/lib/runner.py"
