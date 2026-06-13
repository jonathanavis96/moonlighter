"""ml_ask.py — the night/apply agent asks the USER a clarifying question and waits.

Usage (run by the agent):
    python3 ml_ask.py "Which of these should I keep — A or B?"

NEVER use this for secrets/passwords (e.g. a sudo password): the answer is printed to the
agent's terminal and saved in the run transcript. Root-only work is DEFERRED, not prompted —
see build_apply_mission. This channel is for non-secret clarifications only ("keep A or B?").

Writes the question to $ML_RUN_DIR/ask.json; the panel surfaces it with an answer box.
Blocks (polling) until the user answers via the panel (→ $ML_RUN_DIR/answer.txt) or a
~4-minute timeout, then prints the answer (or a 'no answer' note) and exits 0. Capped
under the agent's Bash-tool timeout so the tool call returns cleanly either way.
"""
import json
import os
import pathlib
import sys
import time

TIMEOUT = 240   # seconds — under the agent Bash-tool ceiling


def main():
    rd = os.environ.get("ML_RUN_DIR")
    if not rd:
        print("(ml_ask: no ML_RUN_DIR — proceeding with best judgment)")
        return 0
    rd = pathlib.Path(rd)
    question = sys.argv[1] if len(sys.argv) > 1 else "Clarification needed?"
    ask = rd / "ask.json"
    ans = rd / "answer.txt"
    if ans.exists():
        try:
            ans.unlink()
        except OSError:
            pass
    aid = str(int(time.time() * 1000))
    ask.write_text(json.dumps({"id": aid, "question": question, "ts": time.time()}),
                   encoding="utf-8")
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if ans.exists():
            try:
                a = ans.read_text(encoding="utf-8").strip()
            except OSError:
                a = ""
            for f in (ans, ask):
                try:
                    f.unlink()
                except OSError:
                    pass
            print(a if a else "(user sent an empty answer)")
            return 0
        time.sleep(2)
    try:
        ask.unlink()
    except OSError:
        pass
    print("(no answer received within 4 min — use your best judgment and note the assumption "
          "in the summary; do not hang)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
