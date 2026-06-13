"""digest.py — roll a night's runs into ONE end-result with a tickable checklist.

Done-items (for tick-to-revert) come straight from each run's manifest.
Proposals (for tick-to-do) come from a run's actions.json if present, else from
fenced ```bash blocks in the summary (best-effort for older runs).
"""
import json
import pathlib
import re
import datetime
import collections

RUNS = pathlib.Path.home() / ".moonlighter" / "runs"


def _runs():
    out = []
    for d in sorted(RUNS.iterdir()):
        if not d.is_dir() or d.name.startswith(("MOCK", "apply-")):
            continue
        mf = d / "run.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text())
        except Exception:
            continue
        m["_dir"] = d
        out.append(m)
    out.sort(key=lambda m: m.get("started", ""))
    return out


def _manifest(d):
    mf = d / "manifest.jsonl"
    recs = []
    if mf.exists():
        for line in mf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    recs.append({})
    return recs


def _hsize(path):
    try:
        return pathlib.Path(path).stat().st_size
    except Exception:
        return 0


def _done_items(recs):
    """Tickable revert-items from a manifest. Pairs each snapshot with its op so a
    moved/trashed file isn't listed twice; standalone snapshots = in-place edits."""
    # paths that have a move/trash (their snapshot is just the backup, not a separate edit)
    paired = set()
    for r in recs:
        if r.get("op") == "move":
            paired.add(r.get("src"))
        elif r.get("op") == "trash":
            paired.add(r.get("path"))
    items = []
    for i, r in enumerate(recs):
        op = r.get("op")
        if op == "move":
            items.append({"idx": i, "kind": "moved",
                          "desc": f"moved {r.get('src','')} → {r.get('dst','')}"})
        elif op == "trash":
            items.append({"idx": i, "kind": "trashed", "desc": f"trashed {r.get('path','')}"})
        elif op == "created":
            items.append({"idx": i, "kind": "created", "desc": f"created {r.get('path','')}"})
        elif op == "snapshot" and r.get("path") not in paired:
            items.append({"idx": i, "kind": "edited", "desc": f"edited {r.get('path','')}"})
        elif op == "note":
            txt = r.get("text", "")
            if "Revert:" in txt:  # perm-fix or similar, revertable
                items.append({"idx": i, "kind": "perms", "desc": txt.split(". Revert:")[0]})
    return items


def _proposals(m):
    """Structured proposals from actions.json, else ```bash blocks from the summary."""
    d = m["_dir"]
    aj = d / "actions.json"
    if aj.exists():
        try:
            data = json.loads(aj.read_text())
            return [{"run": m["id"], "title": p.get("title", ""),
                     "commands": p.get("commands", []), "why": p.get("why", "")}
                    for p in data.get("proposals", [])]
        except Exception:
            pass
    # fallback: fenced bash blocks under the Proposals/Recommendations sections
    out = []
    sm = (d / "summary.md")
    if not sm.exists():
        return out
    text = sm.read_text(encoding="utf-8")
    # only look after a "Proposals" or "Recommendations" heading
    cut = re.search(r"##+\s*(Proposals|Recommendations)", text, re.I)
    body = text[cut.start():] if cut else text
    heading = ""
    for block in re.finditer(r"(?:^|\n)(#{2,4}\s*[^\n]+|```bash\n(.*?)```)", body, re.S):
        chunk = block.group(1)
        if chunk.startswith("#"):
            heading = chunk.lstrip("# ").strip()
        else:
            cmds = [l for l in block.group(2).splitlines()
                    if l.strip() and not l.strip().startswith("#")]
            if cmds:
                out.append({"run": m["id"], "title": heading or "proposed commands",
                            "commands": cmds, "why": ""})
    return out


def _todos(m):
    """Structured OUTSTANDING todos a run registered via ml_todo.py (todo.jsonl).
    These are authoritative: the agent only writes items it did NOT do, so already-done
    work is excluded by construction — nothing here is ever a stale 'tick to do' entry."""
    tf = m["_dir"] / "todo.jsonl"
    out = []
    if not tf.exists():
        return out
    for line in tf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("title"):
            out.append(r)
    return out


# which panel bucket a structured todo category lands in
_CAT_BUCKET = {"security": "security", "idea": "ideas",
               "backup": "audit", "cleanup": "audit", "hygiene": "audit"}


def _todo_text(t):
    """Display/instruction text for a structured todo: title — detail (+ command hint)."""
    parts = [t.get("title", "").strip()]
    if t.get("detail"):
        parts.append("— " + t["detail"].strip())
    s = " ".join(p for p in parts if p)
    cmds = t.get("commands") or []
    if cmds:
        s += "  ⟨" + " ; ".join(cmds) + "⟩"
    return s


def _todo_task(t):
    """Full instruction handed to the apply agent: the text + explicit capability hints
    so it uses the right (approved) permission path instead of skipping."""
    s = _todo_text(t)
    hints = []
    if t.get("needs_sudo"):
        hints.append("NEEDS SUDO (ask for the password via ml_ask)")
    if t.get("needs_push"):
        repo = t.get("repo") or "the repo this item names"
        hints.append(f"NEEDS git push to {repo} (approved — bundle first, then push)")
    if t.get("risk") and t["risk"] != "low":
        hints.append(f"risk: {t['risk']}")
    if hints:
        s += "\n  [" + " · ".join(hints) + "]"
    return s


_SECTION_RE = re.compile(r'^(#{2,4})\s+(.*)$', re.M)


def _sections(text):
    """Split markdown into (title, body) by ## / ### / #### headings."""
    ms = list(_SECTION_RE.finditer(text))
    out = []
    for i, mt in enumerate(ms):
        start = mt.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append((mt.group(2).strip(), text[start:end].strip()))
    return out


def _bullets(body):
    """Top-level items only: a new item starts on a non-indented bullet/number;
    nested bullets and continuation lines fold INTO the current item (so a finding's
    sub-detail stays part of that finding, not separate items)."""
    items, cur = [], None
    for line in body.splitlines():
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if not s:
            continue
        if indent <= 1 and re.match(r'^([-*]|\d+\.)\s+', s):
            if cur:
                items.append(cur)
            cur = re.sub(r'^([-*]|\d+\.)\s+', '', s)
        elif s.startswith(('#', '```', '|')):
            if cur:
                items.append(cur)
                cur = None
        elif cur is not None:
            cur += ' ' + re.sub(r'^([-*]|\d+\.)\s+', '', s)
    if cur:
        items.append(cur)
    return items


def _sig(text):
    """Aggressive normalised signature for dedup — strip md, leading numbers, sizes,
    ALL punctuation, so the same finding worded slightly differently collapses."""
    t = text.lower()
    t = re.sub(r'^\d+\.\s*', '', t)
    t = re.sub(r'\d+(\.\d+)?\s*(gb|mb|kb|g|m|k)\b', '', t)
    t = re.sub(r'[^a-z0-9 ]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:55]


_GENERIC = ("security", "recommendation", "idea", "estate audit", "what i did",
            "proposal", "home", "git repo", "obsidian", "vault", "windows",
            "pi box", "live site", "disk", "dependency", "quick win", "software")


def _is_generic(title):
    """A container heading (use its bullets) vs a specific finding (use the title)."""
    t = title.lower().strip()
    if re.match(r'^\d+\.', t) or "⚠" in title or "`" in title:
        return False
    return any(t.startswith(g) for g in _GENERIC)


# Strip leading decoration so the filter sees the real first word: markdown emphasis,
# checkmark/warning emoji + any variation selector, and a leading "`path` — " prefix
# (vault-edit narration is written "`People.md` — added …").
_LEAD_DECOR = re.compile(r'^[\s*_>#✅☑✔✓✅️☐⚠️❌•\-]+')
_PATH_PREFIX = re.compile(r'^`[^`]+`\s*[—–:-]\s*')

# Past-tense / meta lead-ins that mark a line as NARRATION of work already done
# (or explicitly NOT done) — belongs in "what was done → tick to REVERT", never a DO-task.
_DONE_LEAD = re.compile(
    r'^(?:i\b|we\b|'
    r'trashed|deleted|removed|fixed|converted|added|rejected|declined|resolved|'
    r'demoted|archived|updated|replaced|cleared|purged|killed|moved|created|'
    r'ticked|wrote|renamed|de-?orphaned|merged|committed|skipped|restored|'
    r'left\b|deliberately|carried|verified|confirmed|rewrote|tuned|flagged|'
    r'\d+\s+(?:fixes|notes|repos?|files?|items?)\b)',
    re.I)

# Pure status / health / "all clear" findings — informational, not actionable.
_STATUS_RE = re.compile(
    r'(→\s*200|\b200\b(?:\s*\(|\.|\s|\*)|all (?:up|return|green)|both (?:healthy|green)|'
    r'are\s*\*\*healthy|\bno pressure\b|0 failed|no failed|—\s*clean\b|scan.*clean|'
    r'no remotely-exploitable|structurally healthy|0 (?:empty|zero-byte))',
    re.I)

# Section LABELS that leaked in as items: "A. Foo", "C. Git .git bloat …",
# "Windows (/mnt/c — audit only)", "… needing your judgment", "(NEW angle)".
_LABEL_RE = re.compile(
    r'^[A-G]\.\s|(audit only|needing your judgment|repo-bloat watch|'
    r'\(new angle\)|^windows\b.*audit|^home(-root)?\b.*(loose|audit)|'
    r'^repo hygiene|^inside-the-repo|^git hygiene|^largest tracked|'
    r'^data protection)',
    re.I)


def _is_actionable(text):
    """True if a finding is a thing the user could approve to DO — not narration of
    already-done work, not a bare status line, not a section label/header."""
    t = text.strip()
    if len(t) < 12:
        return False
    if _LABEL_RE.search(t):
        return False
    if _STATUS_RE.search(t):
        return False
    # strip leading emoji / emphasis / "`path` —" prefix, then test for done-narration
    core = _PATH_PREFIX.sub('', _LEAD_DECOR.sub('', t)).strip()
    core = _PATH_PREFIX.sub('', core).strip()  # handle "✅ `path` — verbed"
    if _DONE_LEAD.match(core):
        return False
    return True


def _collect_items(night, predicate, seen=None, order=None):
    """Extract individual ACTIONABLE findings from matching sections across runs.

    Findings are deduped (a thing 3 runs flagged appears once, with a 3× count) and
    kept at FULL length — the text is handed verbatim to the apply agent, so it must
    never be truncated mid-instruction. Pass a shared `seen`/`order` to dedup ACROSS
    categories too (so one finding can't show up under both Security and Ideas).
    """
    own = seen is None
    if own:
        seen, order = {}, []
    new_order = []
    for m in night:
        sm = m["_dir"] / "summary.md"
        if not sm.exists():
            continue
        try:
            tm = datetime.datetime.fromisoformat(m.get("started", "")).strftime("%H:%M")
        except Exception:
            tm = "—"
        for title, body in _sections(sm.read_text(encoding="utf-8")):
            if not predicate(title.lower()):
                continue
            # specific finding → the title IS the item; generic container → its bullets
            cands = _bullets(body) if _is_generic(title) else [title]
            for it in cands:
                if not _is_actionable(it):
                    continue
                sig = _sig(it)
                if len(sig) < 10:
                    continue
                if sig in seen:
                    if tm not in seen[sig]["runs"]:
                        seen[sig]["runs"].append(tm)
                    continue
                seen[sig] = {"text": it, "runs": [tm], "cat": id(predicate)}
                order.append(sig)
                new_order.append(sig)
    keys = order if own else new_order
    return [seen[s] for s in keys if seen[s].get("cat") == id(predicate)]


def build_night(date=None):
    """Aggregate the most recent night's runs (or a given YYYY-MM-DD)."""
    runs = _runs()
    if not runs:
        return {"date": None, "runs": [], "proposals": [], "summary": "No runs yet."}
    if date is None:
        date = runs[-1].get("started", "")[:10]
    night = [m for m in runs if m.get("started", "").startswith(date)]

    tot = collections.Counter()
    run_blocks, proposals = [], []
    for m in night:
        recs = _manifest(m["_dir"])
        items = _done_items(recs)
        for it in items:
            tot[it["kind"]] += 1
        first = (m["_dir"] / "summary.md")
        headline = m.get("headline", "")
        if first.exists():
            ln = first.read_text(encoding="utf-8").strip().splitlines()
            if ln:
                headline = ln[0].lstrip("# ").strip() or headline
        try:
            tm = datetime.datetime.fromisoformat(m.get("started", "")).strftime("%H:%M")
        except Exception:
            tm = "—"
        run_blocks.append({
            "id": m["id"], "time": tm, "dry_run": m.get("dry_run"),
            "status": m.get("status", "—"), "headline": headline,
            "tokens": m.get("tokens"), "five_delta": m.get("five_delta"),
            "items": items,
        })
        proposals.extend(_proposals(m))

    parts = []
    for k in ("trashed", "moved", "created", "edited", "perms"):
        if tot[k]:
            parts.append(f"{tot[k]} {k}")
    # dedup proposals across runs (same reclaim proposed by 3 runs → once)
    seen_p, uniq_p = set(), []
    for p in proposals:
        sig = _sig((p.get("title", "") + " " + " ".join(p.get("commands", []))))
        if sig in seen_p:
            continue
        seen_p.add(sig)
        uniq_p.append(p)
    proposals = uniq_p

    summary = (f"{len(night)} runs · " + ", ".join(parts)) if parts else f"{len(night)} runs (audit only)"

    # categorized prose, consolidated across runs
    is_sec = lambda t: any(k in t for k in ("security", "secret", "cve", "perm", "vulnerab"))
    is_idea = lambda t: any(k in t for k in ("recommend", "idea", "quick win", "software"))
    audit_kw = ("estate audit", "home", "~/code", "repo", "git", "vault", "obsidian",
                "windows", "/mnt/c", "pi box", "pis", "live site", "dependency", "disk")
    is_audit = lambda t: (any(k in t for k in audit_kw)
                          and not is_sec(t) and not is_idea(t) and "proposal" not in t)

    total_tokens = sum(int(m.get("tokens") or 0) for m in night)
    total_min = sum(float(m.get("duration_min") or 0) for m in night)

    # ---- TICKABLE "to do" list: structured todos (authoritative) + prose fallback ----
    # Each item is a uniform dict the panel renders and the apply agent receives. Bucketed
    # into security / ideas / audit. Deduped by signature across BOTH sources and all runs,
    # so nothing double-lists and already-done work never appears (structured todos only
    # carry outstanding items; the prose scrape filters done-narration via _is_actionable).
    buckets = {"security": [], "ideas": [], "audit": []}
    seen = {}

    def _add(item, bucket):
        sig = _sig(item["text"])
        if len(sig) < 10:
            return
        if sig in seen:
            for tm in item.get("runs", []):
                if tm not in seen[sig]["runs"]:
                    seen[sig]["runs"].append(tm)
            return
        seen[sig] = item
        buckets[bucket].append(item)

    # 1) structured todos (preferred) — from every run that registered any
    runs_with_todos = set()
    for m in night:
        ts = _todos(m)
        if not ts:
            continue
        runs_with_todos.add(m["id"])
        try:
            tm = datetime.datetime.fromisoformat(m.get("started", "")).strftime("%H:%M")
        except Exception:
            tm = "—"
        for t in ts:
            bucket = _CAT_BUCKET.get(t.get("category", "idea"), "ideas")
            _add({
                "text": _todo_text(t), "task": _todo_task(t), "runs": [tm],
                "structured": True, "needs_sudo": bool(t.get("needs_sudo")),
                "needs_push": bool(t.get("needs_push")), "repo": t.get("repo", ""),
                "risk": t.get("risk", "low"), "commands": t.get("commands", []),
            }, bucket)

    # 2) prose fallback — ONLY for runs that did NOT emit structured todos (older runs),
    #    so a mixed night still surfaces the old runs' findings without re-introducing noise.
    legacy = [m for m in night if m["id"] not in runs_with_todos]
    if legacy:
        _ps, _po = {}, []
        prose_sec = _collect_items(legacy, is_sec, _ps, _po)
        prose_idea = _collect_items(legacy, is_idea, _ps, _po)
        prose_audit = _collect_items(legacy, is_audit, _ps, _po)
        for it in prose_sec:
            _add({"text": it["text"], "task": it["text"], "runs": it["runs"],
                  "structured": False}, "security")
        for it in prose_idea:
            _add({"text": it["text"], "task": it["text"], "runs": it["runs"],
                  "structured": False}, "ideas")
        for it in prose_audit:
            _add({"text": it["text"], "task": it["text"], "runs": it["runs"],
                  "structured": False}, "audit")

    return {
        "date": date, "summary": summary, "totals": dict(tot),
        "run_count": len(night), "total_tokens": total_tokens,
        "total_min": round(total_min),
        "changes": tot.get("trashed", 0) + tot.get("moved", 0) + tot.get("created", 0),
        "runs": run_blocks, "proposals": proposals,
        "structured_runs": len(runs_with_todos),
        "security": buckets["security"],
        "ideas": buckets["ideas"],
        "audit": buckets["audit"],
    }


def prior_brief(date=None, cap=40):
    """A compact 'already covered tonight — do NOT repeat' block for the next run's
    mission, built from the night's prior runs. Empty if this is the first run."""
    d = build_night(date)
    if not d["runs"]:
        return ""
    lines = []
    t = d.get("totals", {})
    done = ", ".join(f"{t[k]} {k}" for k in ("trashed", "moved", "created", "edited", "perms") if t.get(k))
    if done:
        lines.append(f"ALREADY DONE tonight (do NOT redo): {done}.")
    found = ([f["text"] for f in d.get("security", [])]
             + [f["text"] for f in d.get("ideas", [])]
             + [f["text"] for f in d.get("audit", [])])
    if found:
        lines.append("ALREADY FOUND & REPORTED tonight (do NOT re-audit or re-report these — "
                     "find genuinely NEW things, or DO the proposed work instead):")
        for f in found[:cap]:
            lines.append("  - " + re.sub(r'\s+', ' ', f)[:140])
    props = [p["title"] for p in d.get("proposals", [])]
    if props:
        lines.append("ALREADY PROPOSED tonight (do NOT re-propose): " + "; ".join(props[:20]))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "brief":
        print(prior_brief())
    else:
        print(json.dumps(build_night(sys.argv[1] if len(sys.argv) > 1 else None),
                         indent=2, default=str))
