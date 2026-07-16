"""server.py — Moonlighter local control-panel web server.

Stdlib only. Bind 127.0.0.1 only. PIN-gate all state changes.

Usage:
    python3 panel/server.py
"""
import datetime
import difflib
import http.server
import socketserver
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import traceback
import urllib.parse

# ---------------------------------------------------------------------------
# Boot — resolve lib dir and import modules
# ---------------------------------------------------------------------------
HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
LIB = PROJECT / "lib"
sys.path.insert(0, str(LIB))

import config as cfgmod      # noqa: E402
import gate as gatemod       # noqa: E402
import state                 # noqa: E402
import revert as revertmod   # noqa: E402
import digest as digestmod   # noqa: E402

DEVNULL = subprocess.DEVNULL
TMUX = "moonlighter"


# ---------------------------------------------------------------------------
# HTML builder helpers
# ---------------------------------------------------------------------------

def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _verdict_class(verdict: str) -> str:
    return {"OK": "ok", "GO": "ok", "FAIL": "fail", "SKIP": "fail",
            "HOLD": "hold"}.get(verdict, "hold")


def _pwr_toggle_html(paused: bool, active_run: bool) -> str:
    """The ON/OFF power toggle button, server-rendered with its initial state.

    Shared across every panel page (main, run, night). State is a single boolean,
    `paused`, derived from kill-switch file existence (never string-matched from a
    check name).
    """
    cls = "pwrtoggle off" if paused else "pwrtoggle on"
    label = "OFF" if paused else "ON"
    return (
        f'<button class="{cls}" id="pwr-toggle" '
        f'data-paused="{"1" if paused else "0"}" '
        f'data-active-run="{"1" if active_run else "0"}">'
        f'<span class="pwrdot"></span>{label}</button>'
    )


def _pwr_toggle_script() -> str:
    """Standalone power-toggle wiring for pages that don't share the main dashboard's
    big script (run page, night page): click handler + a 20s /api/status poll so the
    toggle stays state-reflecting even if `paused` changed elsewhere (CLI, ntfy bridge,
    another tab) while this page was open."""
    return """
<script>
function pwrRender(paused, activeRun) {
  const btn = document.getElementById('pwr-toggle');
  if (!btn) return;
  btn.dataset.paused = paused ? '1' : '0';
  btn.dataset.activeRun = activeRun ? '1' : '0';
  btn.className = 'pwrtoggle ' + (paused ? 'off' : 'on');
  btn.innerHTML = '<span class="pwrdot"></span>' + (paused ? 'OFF' : 'ON');
}
async function pwrPost(url, body) {
  try {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                 body: JSON.stringify(body || {})});
    const data = await r.json();
    if (!r.ok || !data.ok) { alert('Error: ' + (data.error || r.status)); return false; }
    return true;
  } catch (e) { alert('Request failed: ' + e); return false; }
}
async function pwrToggleClick() {
  const btn = document.getElementById('pwr-toggle');
  if (!btn) return;
  if (btn.dataset.paused === '1') {
    const pin = prompt('Enter 6-digit PIN:');
    if (!pin) return;
    if (await pwrPost('/api/resume', {pin})) pwrRender(false, btn.dataset.activeRun === '1');
  } else {
    const msg = btn.dataset.activeRun === '1'
      ? 'Switch Moonlighter off? This will stop the run in progress.'
      : 'Switch Moonlighter off?';
    if (!confirm(msg)) return;
    if (await pwrPost('/api/pause', {})) pwrRender(true, false);
  }
}
async function pwrPoll() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    // A degraded response must never read as "running": absent `paused` is unknown,
    // not false. Keep the last known state rather than flipping the toggle to ON.
    if (typeof s.paused !== 'boolean') return;
    pwrRender(s.paused, !!s.active_run);
  } catch (e) {}
}
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('pwr-toggle');
  if (btn) btn.addEventListener('click', pwrToggleClick);
  setInterval(pwrPoll, 20000);
});
</script>
"""


def _human_tokens(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.0f}k"
    return str(n)


def _localize_fonts(html: str) -> str:
    """Replace the Google Fonts CDN links with the self-hosted /fonts.css
    (doc requirement: no CDN, works offline)."""
    html = html.replace(
        '<link rel="preconnect" href="https://fonts.googleapis.com">', "", 1)
    html = re.sub(r'<link href="https://fonts\.googleapis\.com[^"]*" rel="stylesheet">',
                  '<link href="/fonts.css" rel="stylesheet">', html)
    return html


def _replace_div_block(html: str, open_literal: str, new_block: str) -> str:
    """Replace a <div ...>...</div> block (with NESTED divs) by depth-counting,
    so we get the matching close tag, not the first nested one."""
    start = html.find(open_literal)
    if start == -1:
        return html
    i = start + len(open_literal)
    depth = 1
    while i < len(html) and depth > 0:
        nxt_open = html.find("<div", i)
        nxt_close = html.find("</div>", i)
        if nxt_close == -1:
            return html  # malformed; leave untouched
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
        else:
            depth -= 1
            i = nxt_close + 6
    return html[:start] + new_block + html[i:]


def _build_panel_html(status: dict, cfg: dict) -> str:
    """Take template.html, inject ids + live data, append JS. CSS untouched."""
    tmpl_path = HERE / "template.html"
    html = tmpl_path.read_text(encoding="utf-8")

    # --- gate console rows ---
    clog_rows = []
    for c in status.get("gate", {}).get("checks", []):
        ts = _html_escape(c.get("ts", ""))
        name = _html_escape(c.get("name", ""))
        verdict = c.get("verdict", "")
        why = _html_escape(c.get("why", ""))
        vc = _verdict_class(verdict)
        dot_pad = max(2, 26 - len(name))
        dots = "." * dot_pad
        why_span = f' <span class="why">( {why} )</span>' if why else ""
        clog_rows.append(
            f'<div><span class="t">{ts}</span>'
            f'{name} <span class="dots">{dots}</span> '
            f'<span class="{vc}">{_html_escape(verdict)}</span>'
            f'{why_span}</div>'
        )
    clog_html = "\n".join(clog_rows) if clog_rows else '<div class="dim">no checks yet</div>'

    # --- heatmap cells ---
    heat_rows = status.get("heatmap", [])
    heat_raw = status.get("heatmap_raw") or heat_rows
    window_hours = set(status.get("window", []))
    now_cell = status.get("heatmap_now") or [-1, -1]
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    level_words = {0: "quiet", 1: "light", 2: "active", 3: "busy"}
    heat_cells = []
    for dow, row in enumerate(heat_rows):
        for hour_idx, val in enumerate(row):
            cnt = heat_raw[dow][hour_idx] if dow < len(heat_raw) and hour_idx < len(heat_raw[dow]) else 0
            cls_list = []
            if val == 1:
                cls_list.append("a1")
            elif val == 2:
                cls_list.append("a2")
            elif val == 3:
                cls_list.append("a3")
            if hour_idx in window_hours:
                cls_list.append("winh")
            if dow == now_cell[0] and hour_idx == now_cell[1]:
                cls_list.append("now")
            cls_attr = f' class="{" ".join(cls_list)}"' if cls_list else ""
            inwin = " · work window" if hour_idx in window_hours else ""
            title = f"{day_names[dow]} {hour_idx:02d}:00 · {level_words[val]} ({cnt}){inwin}"
            heat_cells.append(
                f'<div{cls_attr} data-dow="{dow}" data-hour="{hour_idx}" '
                f'data-count="{cnt}" data-level="{val}" title="{title}"></div>')
    heat_html = "".join(heat_cells)

    # --- runs table rows ---
    runs = status.get("runs", [])
    if runs:
        run_rows = []
        for r in runs:
            rid = _html_escape(str(r.get("id", "")))
            date = _html_escape(str(r.get("date", "")))
            headline = _html_escape(str(r.get("headline", "—")))
            spend_pct = r.get("spend_pct")
            tokens = r.get("tokens")
            run_status = _html_escape(str(r.get("status", "—")))
            five_d = r.get("five_delta")
            five_part = f'<span class="pct">{five_d}%</span> 5h · ' if five_d else ""
            spend_str = (
                f'{five_part}{_human_tokens(tokens)} tok'
                if (spend_pct is not None or tokens) else "—"
            )
            tag_cls = "tag clean" if run_status.lower() in ("clean", "done") else "tag"
            view_color = "var(--gold)" if rid else "var(--dim)"
            row_cls = ' class="applyrow"' if r.get("apply") else ""
            rtime = _html_escape(str(r.get("time", "")))
            status_tags = ('<span class="tag applied">✅ applied</span>' if r.get("apply")
                           else f'<span class="{tag_cls}">{run_status}</span>')
            run_rows.append(
                f"<tr{row_cls}>"
                f'<td class="date">{date}<span class="rtime">{rtime}</span></td>'
                f"<td>{headline}</td>"
                f"<td>{spend_str}</td>"
                f'<td><span class="tags">{status_tags}</span></td>'
                f'<td style="color:{view_color}">'
                f'{"<a href=" + chr(34) + "/run/" + rid + chr(34) + " style=" + chr(34) + "color:var(--gold);text-decoration:none" + chr(34) + ">view →</a>" if rid else "log →"}'
                f"</td>"
                f"</tr>"
            )
        runs_tbody = "\n".join(run_rows)
    else:
        runs_tbody = '<tr><td colspan="5" style="color:var(--dim);text-align:center">no runs yet</td></tr>'

    # --- summary and gate info ---
    gate = status.get("gate", {})
    summary_text = _html_escape(gate.get("summary", ""))
    next_check = _html_escape(str(gate.get("next_check", "")))

    # --- usage stats ---
    usage = status.get("usage", {})
    five = usage.get("five_hour", {})
    week = usage.get("seven_day", {})
    five_util = five.get("utilization", 0) or 0
    week_util = week.get("utilization", 0) or 0
    five_max_pct = usage.get("five_hour_max_pct", 20) or 20
    weekly_reserve_pct = usage.get("weekly_reserve_pct", 10) or 10
    five_resets_in = _html_escape(str(five.get("resets_in", "")))
    week_resets_in = _html_escape(str(week.get("resets_in", "")))

    mode_text = _html_escape(str(status.get("mode", "")))
    night_text = _html_escape(str(status.get("night", "")))
    is_live = status.get("live", False)

    # Whether any dry run has been recorded (for approve button state)
    runs_raw = state.list_runs(50)
    has_dry_run = any(r.get("dry_run") for r in runs_raw)
    approved = status.get("approved", False)
    approve_disabled = "" if has_dry_run else " disabled"

    # JSON for the wiring script to reuse on refresh
    status_json = json.dumps(status, default=str)

    script = f"""
<script>
// ---- Live data (server-rendered snapshot) ----
const INITIAL_STATUS = {status_json};
const HAS_DRY_RUN = {"true" if has_dry_run else "false"};
const APPROVED = {"true" if approved else "false"};
let LAST_STATUS = INITIAL_STATUS;

// ---- DOM helpers ----
function el(id) {{ return document.getElementById(id); }}
function esc(s) {{
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}
function humTok(n) {{
  n = parseInt(n||0,10);
  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return Math.round(n/1000) + 'k';
  return String(n);
}}

// ---- Verdict → CSS class ----
function verdictClass(v) {{
  const m = {{OK:'ok',GO:'ok',FAIL:'fail',SKIP:'fail',HOLD:'hold'}};
  return m[v] || 'hold';
}}

// ---- Power toggle (header) ----
function pwrRender(paused, activeRun) {{
  const btn = el('pwr-toggle');
  if (!btn) return;
  btn.dataset.paused = paused ? '1' : '0';
  btn.dataset.activeRun = activeRun ? '1' : '0';
  btn.className = 'pwrtoggle ' + (paused ? 'off' : 'on');
  btn.innerHTML = '<span class="pwrdot"></span>' + (paused ? 'OFF' : 'ON');
}}

// ---- Live run-in-progress card ----
let ML_RUN_STARTED = null;
let ML_RUN_ACTIVE = false;
function fmtElapsed(sec) {{
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  if (h>0) return h + ':' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
  return m + ':' + String(s).padStart(2,'0');
}}
function renderFeed(activity) {{
  const feed = el('run-feed');
  if (!feed) return;
  if (!activity || !activity.length) {{ feed.innerHTML = '<span class="think">starting…</span>'; return; }}
  feed.innerHTML = activity.map((line, i) => {{
    const isSpin = (i === activity.length-1) && (line.indexOf('…') >= 0 || line.indexOf('tokens)') >= 0);
    return '<div class="' + (isSpin ? 'spin' : 'think') + '">' + esc(line) + '</div>';
  }}).join('');
  feed.scrollTop = feed.scrollHeight;
}}
function showRunCard(ar) {{
  const card = el('run-card');
  if (!card) return;
  const active = ar && ar.active !== false && (ar.id || ar.started || (ar.activity && ar.activity.length));
  if (active) {{
    card.style.display = '';
    ML_RUN_ACTIVE = true;
    if (ar.started) ML_RUN_STARTED = new Date(ar.started).getTime();
    const mode = el('run-mode'); if (mode) mode.textContent = ar.dry_run ? '· dry run' : '· full-auto';
    renderFeed(ar.activity);
    const askb = el('ask-box');
    if (askb) {{
      if (ar.ask && ar.ask.question) {{
        askb.style.display = ''; askb.dataset.run = ar.id || '';
        el('ask-q').innerHTML = '🟡 <b>The agent is asking:</b> ' + esc(ar.ask.question);
      }} else {{ askb.style.display = 'none'; }}
    }}
  }} else {{
    if (ML_RUN_ACTIVE) {{ ML_RUN_ACTIVE = false; refreshStatus(); }}  // just ended → reload ledger
    card.style.display = 'none';
    ML_RUN_STARTED = null;
  }}
}}

// ---- Render functions ----
function renderStatus(s) {{
  if (!s) return;
  LAST_STATUS = s;
  // The power toggle reflects the kill-switch file, independent of the usage API —
  // update it even when there's no live usage data to show. But absent `paused` is
  // unknown, not false: keep the last known state rather than reading as ON.
  if (typeof s.paused === 'boolean') pwrRender(s.paused, !!s.active_run);
  if (s.live === false) {{
    el('mode-line').textContent = '● no live data';
    ['five-huge','week-huge'].forEach(id => el(id).textContent = '--');
    return;
  }}

  // Mode / night
  const modeEl = el('mode-line');
  modeEl.innerHTML = esc(s.mode) + ' · ' + esc(s.night) + '<br><b>next window&nbsp; ' +
    esc((s.gate && s.gate.next_check) ? 'next check ' + s.gate.next_check : '') + '</b>';

  // Five-hour
  const u = s.usage || {{}};
  const five = u.five_hour || {{}};
  const week = u.seven_day || {{}};
  const fiveUtil = five.utilization != null ? five.utilization : 0;
  const weekUtil = week.utilization != null ? week.utilization : 0;
  const fiveMax = u.five_hour_max_pct || 20;
  const weekReserve = u.weekly_reserve_pct || 10;

  el('five-huge').textContent = fiveUtil;
  el('five-track-i').style.width = fiveUtil + '%';
  el('five-track-u').style.left = fiveMax + '%';
  el('five-resets').innerHTML = 'resets in <b>' + esc(five.resets_in || '') + '</b> · start threshold <b>' + fiveMax + '%</b>';

  el('week-huge').textContent = weekUtil;
  el('week-track-i').style.width = weekUtil + '%';
  el('week-track-u').style.left = (100 - weekReserve) + '%';
  el('week-resets').innerHTML = 'resets in <b>' + esc(week.resets_in || '') + '</b> · reserve <b>' + weekReserve + '%</b>';

  // Gate console
  const gate = s.gate || {{}};
  const checks = gate.checks || [];
  const clog = el('clog');
  if (clog) {{
    clog.innerHTML = checks.map(c => {{
      const dots = '.'.repeat(Math.max(2, 26 - (c.name||'').length));
      const vc = verdictClass(c.verdict);
      const why = c.why ? ' <span class="why">( ' + esc(c.why) + ' )</span>' : '';
      return '<div><span class="t">' + esc(c.ts||'') + '</span>' +
        esc(c.name||'') + ' <span class="dots">' + dots + '</span> ' +
        '<span class="' + vc + '">' + esc(c.verdict||'') + '</span>' + why + '</div>';
    }}).join('');
  }}

  const summaryEl = el('gate-summary');
  if (summaryEl) summaryEl.textContent = gate.summary || '';

  const nextEl = el('gate-next');
  if (nextEl) nextEl.innerHTML = 'next check <b>' + esc(gate.next_check||'') + '</b>';

  // Heatmap
  const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const LEVELW = ['quiet','light','active','busy'];
  const heatEl = el('heat-grid');
  if (heatEl && s.heatmap) {{
    const window_hours = new Set(s.window || []);
    const nowc = s.heatmap_now || [-1,-1];
    const raw = s.heatmap_raw || s.heatmap;
    let cells = '';
    s.heatmap.forEach((row, dow) => {{
      row.forEach((val, hour) => {{
        let cls = [];
        if (val === 1) cls.push('a1');
        else if (val === 2) cls.push('a2');
        else if (val === 3) cls.push('a3');
        if (window_hours.has(hour)) cls.push('winh');
        if (dow === nowc[0] && hour === nowc[1]) cls.push('now');
        const cnt = (raw[dow] && raw[dow][hour] != null) ? raw[dow][hour] : 0;
        const inwin = window_hours.has(hour) ? ' · work window' : '';
        const title = DAYS[dow] + ' ' + String(hour).padStart(2,'0') + ':00 · ' + LEVELW[val] + ' (' + cnt + ')' + inwin;
        cells += '<div' + (cls.length ? ' class="' + cls.join(' ') + '"' : '') +
          ' data-dow="' + dow + '" data-hour="' + hour + '" data-count="' + cnt +
          '" data-level="' + val + '" title="' + title + '"></div>';
      }});
    }});
    heatEl.innerHTML = cells;
  }}

  // Week graph (data-driven) + caption
  if (s.graph) {{
    const gw = el('graph-wrap');
    if (gw && s.graph.svg) gw.innerHTML = s.graph.svg;
    const gc = el('graph-cap');
    if (gc) gc.innerHTML = s.graph.caption || '';
  }}

  // Live run card
  showRunCard(s.active_run);

  // Runs table
  const tbody = el('runs-tbody');
  if (tbody) {{
    const runs = s.runs || [];
    if (runs.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--dim);text-align:center">no runs yet</td></tr>';
    }} else {{
      tbody.innerHTML = runs.map(r => {{
        const rid = esc(r.id||'');
        const fivePart = (r.five_delta) ? '<span class="pct">' + r.five_delta + '%</span> 5h · ' : '';
        const spend = (r.spend_pct != null || r.tokens)
          ? fivePart + humTok(r.tokens) + ' tok'
          : '—';
        const stcls = (r.status||'').toLowerCase() === 'clean' ? 'tag clean' : 'tag';
        const statusTags = r.apply ? '<span class="tag applied">✅ applied</span>'
                                   : '<span class="' + stcls + '">' + esc(r.status||'—') + '</span>';
        const viewLink = rid
          ? '<a href="/run/' + rid + '" style="color:var(--gold);text-decoration:none">view →</a>'
          : '<span style="color:var(--dim)">log →</span>';
        const rtime = r.time ? '<span class="rtime">' + esc(r.time) + '</span>' : '';
        return '<tr' + (r.apply?' class="applyrow"':'') + '>' +
          '<td class="date">' + esc(r.date||'') + rtime + '</td>' +
          '<td>' + esc(r.headline||'—') + '</td>' +
          '<td>' + spend + '</td>' +
          '<td><span class="tags">' + statusTags + '</span></td>' +
          '<td>' + viewLink + '</td>' +
          '</tr>';
      }}).join('');
    }}
  }}

  // Mode toggle buttons reflect current mode
  if (s.mode) setModeButtons(s.mode);

  // Prefill spare-capacity settings (don't clobber a field being edited)
  const b = s.gate && s.gate.budget;
  if (b) {{
    const fl = el('set-five-leave');
    if (fl && document.activeElement !== fl) fl.value = Math.round(100 - (b.five_target||80));
    const wr = el('set-weekly-reserve');
    if (wr && document.activeElement !== wr) wr.value = Math.round(b.reserve_pct||10);
  }}
}}

async function doSaveSettings() {{
  const fl = parseFloat((el('set-five-leave')||{{}}).value);
  const wr = parseFloat((el('set-weekly-reserve')||{{}}).value);
  if (isNaN(fl) || isNaN(wr)) {{ alert('Enter both numbers.'); return; }}
  const pin = pinPrompt();
  if (!pin) return;
  const ok = await postAction('/api/settings', {{five_leave: fl, weekly_reserve: wr, pin}});
  if (ok) {{ alert('Saved — fill 5h to ' + (100-fl) + '%, always leave ' + wr + '% weekly.'); refreshStatus(); }}
}}

// ---- Button actions ----
function pinPrompt() {{
  return prompt('Enter 6-digit PIN:');
}}

async function postAction(url, body) {{
  try {{
    const r = await fetch(url, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(body)
    }});
    const data = await r.json();
    if (!r.ok || !data.ok) {{
      alert('Error: ' + (data.error || r.status));
      return false;
    }}
    return true;
  }} catch(e) {{
    alert('Request failed: ' + e);
    return false;
  }}
}}

async function doStart() {{
  const pin = pinPrompt();
  if (!pin) return;
  const hours = parseFloat(prompt('Away for how many hours?', '5') || '5') || 5;
  const ok = await postAction('/api/start', {{pin, hours}});
  if (ok) {{ alert('Run started!'); refreshStatus(); }}
}}

async function doPause() {{
  // No PIN — off is the fail-safe direction (design doc "Accepted risk"). Warn first
  // if it will stop a run in progress; otherwise just confirm the switch.
  const activeRun = LAST_STATUS && !!LAST_STATUS.active_run;
  const msg = activeRun
    ? 'Switch Moonlighter off? This will stop the run in progress.'
    : 'Switch Moonlighter off?';
  if (!confirm(msg)) return;
  const ok = await postAction('/api/pause', {{}});
  if (ok) {{ refreshStatus(); }}
}}

async function doResume() {{
  const pin = pinPrompt();
  if (!pin) return;
  const ok = await postAction('/api/resume', {{pin}});
  if (ok) {{ alert('Resumed.'); refreshStatus(); }}
}}

async function doToggleMode() {{
  const btn = el('btn-mode') || el('btn-mode-m');
  const cur = btn ? btn.dataset.mode : 'observe';
  const target = cur === 'full-auto' ? 'observe' : 'full-auto';
  const msg = target === 'full-auto'
    ? 'Switch to FULL-AUTO? Runs will actually make changes (all reversible).'
    : 'Switch to REVIEW? Runs will only propose, touching nothing.';
  if (!confirm(msg)) return;
  const pin = pinPrompt();
  if (!pin) return;
  const ok = await postAction('/api/mode', {{mode: target, pin}});
  if (ok) {{ refreshStatus(); }}
}}
function setModeButtons(mode) {{
  const lab = mode === 'full-auto' ? 'Switch to Review' : 'Switch to Full-auto';
  const b = el('btn-mode'); if (b) {{ b.textContent = lab; b.dataset.mode = mode; }}
  const m = el('btn-mode-m'); if (m) {{ m.textContent = mode === 'full-auto' ? 'Review' : 'Auto'; m.dataset.mode = mode; }}
}}

// ---- Auto-refresh ----
async function refreshStatus() {{
  try {{
    const r = await fetch('/api/status');
    const s = await r.json();
    renderStatus(s);
  }} catch(e) {{
    const modeEl = el('mode-line');
    if (modeEl) modeEl.textContent = '● no live data';
  }}
}}

// ---- Wire buttons ----
document.addEventListener('DOMContentLoaded', () => {{
  renderStatus(INITIAL_STATUS);

  // Desktop start buttons
  document.querySelectorAll('.btn-start').forEach(b => b.addEventListener('click', doStart));
  // Power toggle (header, works for desktop + mobile — one control, always visible)
  const pwrBtn = el('pwr-toggle');
  if (pwrBtn) pwrBtn.addEventListener('click', () => {{
    if (pwrBtn.dataset.paused === '1') doResume(); else doPause();
  }});
  // Mode toggle buttons (desktop + mobile)
  const modeBtn = el('btn-mode');
  if (modeBtn) modeBtn.addEventListener('click', doToggleMode);
  const modeBtnM = el('btn-mode-m');
  if (modeBtnM) modeBtnM.addEventListener('click', doToggleMode);

  // Save settings
  const saveBtn = el('btn-save-settings');
  if (saveBtn) saveBtn.addEventListener('click', doSaveSettings);

  // Answer the agent's clarifying question
  const askSend = el('ask-send');
  if (askSend) askSend.addEventListener('click', async () => {{
    const askb = el('ask-box'), inp = el('ask-input');
    const answer = (inp.value || '').trim();
    if (!answer) {{ inp.focus(); return; }}
    try {{
      await fetch('/api/answer', {{method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{run: askb.dataset.run, answer}})}});
      inp.value = ''; askb.style.display = 'none';
      el('run-feed') && (el('run-feed').innerHTML += '<div class="think">↳ you answered — continuing…</div>');
    }} catch(e) {{ alert('Send failed: ' + e); }}
  }});
  // Mobile start
  document.querySelectorAll('.btn-mobile-start').forEach(b => b.addEventListener('click', doStart));

  // Heatmap tap/click → readout (delegated, survives refresh re-renders)
  const grid = el('heat-grid');
  if (grid) grid.addEventListener('click', (e) => {{
    const cell = e.target.closest('div[data-dow]');
    if (!cell) return;
    grid.querySelectorAll('.sel').forEach(c => c.classList.remove('sel'));
    cell.classList.add('sel');
    const DAYS = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
    const LEVELW = ['quiet','light activity','active','busy'];
    const dow = +cell.dataset.dow, hour = +cell.dataset.hour;
    const cnt = +cell.dataset.count, lvl = +cell.dataset.level;
    const h2 = String((hour+1)%24).padStart(2,'0');
    const ro = el('heat-readout');
    if (ro) ro.innerHTML = '<b>' + DAYS[dow] + ' ' + String(hour).padStart(2,'0') + ':00–' + h2 + ':00</b> · ' +
      LEVELW[lvl] + ' — active ' + cnt + ' time' + (cnt===1?'':'s') + ' over the last 4 weeks' +
      (cell.classList.contains('winh') ? ' · <b>Moonlighter window</b>' : '');
  }});

  // Auto-refresh every 20s
  setInterval(refreshStatus, 20000);

  // Run timer ticks every second from the run's start time
  setInterval(() => {{
    if (ML_RUN_STARTED) {{
      const t = el('run-timer');
      if (t) t.textContent = fmtElapsed((Date.now() - ML_RUN_STARTED) / 1000);
    }}
  }}, 1000);

  // Fast activity poll (no usage API) — live thoughts while a run is active
  setInterval(async () => {{
    try {{
      const r = await fetch('/api/run-activity');
      showRunCard(await r.json());
    }} catch(e) {{}}
  }}, 2500);
}});
</script>
"""

    # --- Inject IDs and replace demo content ---
    # We surgically inject id= attributes and replace demo content.
    # We do NOT touch any CSS or structural elements.

    # 1. Mode line div
    old_mode = '<div class="mode">Observe mode · night 0<br><b>next window&nbsp; 02:00 – 05:30</b></div>'
    new_mode = (
        f'<div class="mode" id="mode-line">'
        f'{mode_text} · {night_text}<br>'
        f'<b>next check&nbsp; {next_check}</b></div>'
    )
    html = html.replace(old_mode, new_mode, 1)

    # 1b. Power toggle (header, every page) — reflects the kill-switch file directly
    html = html.replace(
        '<button class="pwrtoggle on" id="pwr-toggle" data-paused="0" data-active-run="0">'
        '<span class="pwrdot"></span>ON</button>',
        _pwr_toggle_html(
            # If status is degraded and carries no `paused`, read the kill-switch file
            # rather than defaulting to False — an unknown state must not render as ON.
            status["paused"] if isinstance(status.get("paused"), bool)
            else cfg["kill_switch_path"].exists(),
            status.get("active_run") is not None,
        ),
        1)

    # 2. Five-hour huge number
    old_five_huge = '<div class="huge">71<small>%</small></div>'
    new_five_huge = f'<div class="huge"><span id="five-huge">{five_util:.0f}</span><small>%</small></div>'
    html = html.replace(old_five_huge, new_five_huge, 1)

    # 3. Five-hour track
    old_five_track = '<div class="track"><i style="width:71%"></i><u style="left:20%"></u></div>'
    new_five_track = (
        f'<div class="track">'
        f'<i id="five-track-i" style="width:{five_util:.0f}%"></i>'
        f'<u id="five-track-u" style="left:{five_max_pct:.0f}%"></u>'
        f'</div>'
    )
    html = html.replace(old_five_track, new_five_track, 1)

    # 4. Five-hour resets
    old_five_resets = '<p class="resets">resets in <b>3 h 14 m</b> · start threshold <b>20%</b></p>'
    new_five_resets = (
        f'<p class="resets" id="five-resets">'
        f'resets in <b>{five_resets_in}</b> · start threshold <b>{five_max_pct:.0f}%</b>'
        f'</p>'
    )
    html = html.replace(old_five_resets, new_five_resets, 1)

    # 5. Week huge number
    old_week_huge = '<div class="huge">10<small>%</small></div>'
    new_week_huge = f'<div class="huge"><span id="week-huge">{week_util:.0f}</span><small>%</small></div>'
    html = html.replace(old_week_huge, new_week_huge, 1)

    # 6. Week track
    old_week_track = '<div class="track"><i style="width:10%"></i><u style="left:90%"></u></div>'
    new_week_track = (
        f'<div class="track">'
        f'<i id="week-track-i" style="width:{week_util:.0f}%"></i>'
        f'<u id="week-track-u" style="left:{100 - weekly_reserve_pct:.0f}%"></u>'
        f'</div>'
    )
    html = html.replace(old_week_track, new_week_track, 1)

    # 7. Week resets
    old_week_resets = '<p class="resets">resets <b>Friday 06:00</b> · reserve <b>10%</b></p>'
    new_week_resets = (
        f'<p class="resets" id="week-resets">'
        f'resets in <b>{week_resets_in}</b> · reserve <b>{weekly_reserve_pct:.0f}%</b>'
        f'</p>'
    )
    html = html.replace(old_week_resets, new_week_resets, 1)

    # 8. Gate console bar next
    old_next = '<div class="next">next check <b>14:30</b></div>'
    new_next = f'<div class="next" id="gate-next">next check <b>{next_check}</b></div>'
    html = html.replace(old_next, new_next, 1)

    # 9. clog div (replace whole nested block, depth-aware)
    new_clog_block = f'<div class="clog" id="clog">\n{clog_html}\n</div>'
    html = _replace_div_block(html, '<div class="clog">', new_clog_block)

    # 10. Summary
    old_summary = '<div class="summary">Holding — your window is hot and you\'re still around. Tonight at <b>02:00</b> looks clear.</div>'
    new_summary = f'<div class="summary" id="gate-summary">{summary_text}</div>'
    html = html.replace(old_summary, new_summary, 1)

    # 11. Actions buttons
    old_actions = '''  <div class="actions">
    <button class="btn primary">Start now</button>
    <div class="hours">away for <span class="pill">5 h</span></div>
    <button class="btn ghost">Pause</button>
    <button class="btn" disabled>Approve full-auto</button>
  </div>'''
    cur_mode = status.get("mode", "observe")
    mode_label = "Switch to Review" if cur_mode == "full-auto" else "Switch to Full-auto"
    # Pause used to live here as a dead-end button (wired to doPause, no doResume anywhere).
    # It's replaced by the header power toggle (on/off, state-reflecting, every page).
    new_actions = (
        f'  <div class="actions">\n'
        f'    <button class="btn primary btn-start">Start now</button>\n'
        f'    <div class="hours">away for <span class="pill">5 h</span></div>\n'
        f'    <button class="btn" id="btn-mode" data-mode="{cur_mode}">{mode_label}</button>\n'
        f'  </div>'
    )
    html = html.replace(old_actions, new_actions, 1)

    # 12. Heatmap grid — replace whole nested block, depth-aware
    new_heat_block = f'<div class="heat" id="heat-grid">{heat_html}</div>'
    html = _replace_div_block(html, '<div class="heat">', new_heat_block)

    # 12b. Tappable readout line under the heatmap label
    html = html.replace(
        'works the quiet hours <span style="color:var(--gold)">▣</span></div>',
        'works the quiet hours <span style="color:var(--gold)">▣</span></div>'
        '\n<div class="heat-readout" id="heat-readout">Tap a block for its time &amp; activity.</div>',
        1)

    # 12c. Big "view last night's report" button above the ledger
    html = html.replace(
        '<div class="label">The ledger of nights</div>',
        '<div class="label">The ledger of nights</div>\n'
        '<a href="/night" class="btn primary" style="display:block;text-align:center;'
        'margin:4px 0 20px;padding:16px;text-decoration:none">📋 &nbsp;View last night\'s report →</a>',
        1)

    # 13. Runs table — add id to tbody, replace demo rows
    old_thead = '<thead><tr><th>Night</th><th>What was done</th><th>Spend</th><th>Status</th><th></th></tr></thead>'
    new_thead = f'<thead><tr><th>Night</th><th>What was done</th><th>Spend</th><th>Status</th><th></th></tr></thead>\n<tbody id="runs-tbody">'
    html = html.replace(old_thead, new_thead, 1)

    # Replace the demo rows (from first <tr> after thead to </table>)
    # Find old demo rows and closing </table>
    old_runs_demo = '''      <tr>
        <td class="date">June 11th</td>
        <td>Sorted 49 stray files out of ~/code · filed 7 inbox ideas · cleared two untitled canvases</td>
        <td><span class="pct">5.8%</span> · 412k tok</td>
        <td><span class="tag clean">clean</span></td>
        <td style="color:var(--gold)">view →</td>
      </tr>
      <tr>
        <td class="date">June 10th</td>
        <td>Skipped — you were active at 02:40</td>
        <td>—</td>
        <td><span class="tag">skipped</span></td>
        <td style="color:var(--dim)">log →</td>
      </tr>
      <tr>
        <td class="date">June 9th</td>
        <td>Dry run — proposed 14 actions, touched nothing</td>
        <td><span class="pct">0.4%</span> · 31k tok</td>
        <td><span class="tag">observed</span></td>
        <td style="color:var(--gold)">view →</td>
      </tr>
    </table>'''
    new_runs_content = f'{runs_tbody}\n    </tbody>\n    </table>'
    html = html.replace(old_runs_demo, new_runs_content, 1)

    # 14. Mobile bar buttons
    old_mobile = '''<div class="mobilebar">
  <button class="btn primary">▶ Start · away 5h</button>
  <button class="btn ghost">Pause</button>
</div>'''
    # Mobile Pause button dropped too — the header power toggle is visible at every
    # width (only the .mode text is hidden on mobile, not the toggle) so one control
    # now serves desktop and mobile.
    new_mobile = (
        '<div class="mobilebar">\n'
        '  <button class="btn primary btn-mobile-start">▶ Start · away 5h</button>\n'
        f'  <button class="btn ghost" id="btn-mode-m" data-mode="{cur_mode}">{("Review" if cur_mode=="full-auto" else "Auto")}</button>\n'
        '</div>'
    )
    html = html.replace(old_mobile, new_mobile, 1)

    # 14b. Week graph — replace the static demo SVG with the data-driven one + caption
    g = status.get("graph") or {}
    gsvg = g.get("svg") or ""
    gcap = g.get("caption") or ""
    if gsvg:
        s_start = html.find("<svg")
        s_end = html.find("</svg>", s_start)
        if s_start != -1 and s_end != -1:
            html = html[:s_start] + f'<div id="graph-wrap">{gsvg}</div>' + html[s_end + 6:]
    # caption after the legend block
    leg_start = html.find('<div class="legend">')
    if leg_start != -1:
        leg_end = html.find("</div>", leg_start)
        if leg_end != -1:
            cap = f'\n<div class="gcap" id="graph-cap">{gcap}</div>'
            html = html[:leg_end + 6] + cap + html[leg_end + 6:]

    # 14c. Live run-in-progress card (hidden until a run is active) — placed at the
    # TOP, right under the header, so it's the first thing you see while watching.
    run_card = (
        '\n  <section class="runcard" id="run-card" style="display:none">\n'
        '  <div class="bar">\n'
        '    <div class="label" id="run-card-label">● Run in progress <span id="run-mode" style="color:var(--blue)"></span></div>\n'
        '    <div class="next">elapsed <b id="run-timer">0:00</b></div>\n'
        '  </div>\n'
        '  <div class="runfeed" id="run-feed"></div>\n'
        '  <div class="askbox" id="ask-box" style="display:none">\n'
        '    <div class="askq" id="ask-q"></div>\n'
        '    <textarea id="ask-input" rows="2" placeholder="Type your answer…"></textarea>\n'
        '    <button class="btn primary" id="ask-send">Send answer</button>\n'
        '  </div>\n'
        '  </section>\n'
    )
    html = html.replace('</header>', '</header>' + run_card, 1)

    # graph legend — actual (solid) · expected-from-activity (dashed) · reserve line
    html = html.replace(
        '<span><i style="background:rgba(143,168,216,.5)"></i>your forecast</span>\n'
        '        <span><i style="background:#d4b478"></i>with moonlighter</span>',
        '<span><i style="background:#9db5e2"></i>expected</span>'
        '<span><i style="background:#d4b478"></i>reserve line</span>', 1)
    html = html.replace('<i style="background:#e8e2d4"></i>so far',
                        '<i style="background:#e8e2d4"></i>actual', 1)

    # 14d. Spare-capacity settings card (adjustable knobs) — before the heatmap panel.
    settings_card = (
        '<section class="panel settingscard">\n'
        '  <div class="label">Spare-capacity settings</div>\n'
        '  <div class="setgrid">\n'
        '    <div class="setitem"><span class="setlbl">Leave free in the 5-hour window</span>'
        '<div class="setval"><input id="set-five-leave" type="number" min="0" max="95" step="5"> %</div></div>\n'
        '    <div class="setitem"><span class="setlbl">Always leave weekly for you</span>'
        '<div class="setval"><input id="set-weekly-reserve" type="number" min="0" max="90" step="5"> %</div></div>\n'
        '    <button class="btn ghost" id="btn-save-settings">Save</button>\n'
        '  </div>\n'
        '  <a href="/setup" class="reconf">⚙ Reconfigure folders, devices &amp; PIN →</a>\n'
        '</section>\n'
    )
    # inject before the heatmap section
    idx_heat = html.find('<section class="panel">')
    if idx_heat != -1:
        html = html[:idx_heat] + settings_card + "\n  " + html[idx_heat:]

    # extra CSS (now-marker + caption) appended into the main style block.
    # The trailing @media rule overrides the template's swipeable heatmap (whose
    # hour labels sat OUTSIDE the scroll container and desynced on swipe): instead
    # the heatmap fits the mobile width so all 24 hours + labels line up, no scroll.
    extra_css = (
        ".heat .now{box-shadow:0 0 0 2px var(--moon);position:relative;z-index:3}"
        ".reconf{display:inline-block;margin-top:12px;color:var(--gold);text-decoration:none;font-size:13px;opacity:.85}"
        ".reconf:hover{opacity:1;text-decoration:underline}"
        ".gcap{margin-top:12px;font-size:11.5px;color:#b7c0d4;line-height:1.55}"
        ".gcap b{color:var(--gold);font-weight:400}"
        ".runcard{margin:0 0 34px;border:1px solid rgba(168,212,154,.45);background:rgba(8,12,8,.6)}"
        ".runcard .bar{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line)}"
        ".runcard .bar .label{margin:0;color:var(--ok);animation:mlpulse 2s ease-in-out infinite}"
        ".runcard .next b{color:var(--moon);font-variant-numeric:tabular-nums}"
        "@keyframes mlpulse{0%,100%{opacity:.55}50%{opacity:1}}"
        ".runfeed{padding:14px 22px;font-size:12.5px;line-height:1.85;color:#cdd6e6;max-height:260px;overflow-y:auto}"
        ".runfeed .think{color:#d3dbe9;padding:1px 0}"
        ".runfeed .spin{color:var(--gold);padding-top:4px}"
        ".askbox{padding:14px 20px;border-top:1px solid var(--gold);background:rgba(212,180,120,.07)}"
        ".askq{color:var(--gold);font-size:13.5px;margin-bottom:9px;line-height:1.5}"
        ".askbox textarea{width:100%;box-sizing:border-box;background:rgba(0,0,0,.35);border:1px solid var(--line);color:var(--moon);font-family:'IBM Plex Mono',monospace;font-size:13px;padding:9px;margin-bottom:9px;resize:vertical}"
        "#ask-send{padding:11px 20px}"
        ".tag.applied{color:var(--ok);border-color:rgba(168,212,154,.55)}"
        ".applyrow td{background:rgba(168,212,154,.07);border-bottom-color:rgba(168,212,154,.25)}"
        ".tags{display:flex;flex-direction:column;gap:5px;align-items:flex-start}"
        ".tags .tag{display:inline-block}"
        "td.date .rtime{display:block;font-size:11px;color:var(--dim);margin-top:3px;font-family:'IBM Plex Mono',monospace}"
        ".settingscard{margin-top:28px}"
        ".setgrid{display:flex;gap:24px;align-items:flex-end;flex-wrap:wrap;margin-top:10px}"
        ".setitem{display:flex;flex-direction:column;gap:9px}"
        ".setlbl{font-size:11px;color:var(--dim);letter-spacing:.05em}"
        ".setval{color:var(--moon);font-size:13px}"
        ".setval input{width:64px;background:rgba(0,0,0,.3);border:1px solid var(--line);color:var(--gold);"
        "font-family:'IBM Plex Mono',monospace;font-size:16px;padding:6px 8px;text-align:center;margin-right:4px}"
        "#btn-save-settings{padding:11px 24px}"
        ".heat-readout{font-size:11.5px;color:var(--dim);margin:2px 0 12px;min-height:15px}"
        ".heat-readout b{color:var(--gold);font-weight:400}"
        ".heat div{cursor:pointer}"
        ".heat .sel{box-shadow:0 0 0 2px var(--blue);position:relative;z-index:4}"
        "@media (max-width:720px){"
        ".heatwrap{overflow-x:visible;margin:0;padding:0}"
        ".heatwrap .heat{min-width:0}"
        ".heat{gap:2px}"
        "}"
    )
    html = html.replace("</style>", extra_css + "</style>", 1)

    # 15. Append script before </body>
    html = html.replace('</body>', script + '\n</body>', 1)

    # 17. Make the Moonlighter logo a home link
    html = html.replace(
        '<div class="brand">',
        '<a href="/" class="brand" style="text-decoration:none;color:inherit">', 1)
    html = html.replace(
        '<h1>Moon<em>lighter</em></h1>\n    </div>',
        '<h1>Moon<em>lighter</em></h1>\n    </a>', 1)

    # 16. Self-host fonts (no CDN)
    html = _localize_fonts(html)

    return html


# ---------------------------------------------------------------------------
# Per-run page builder
# ---------------------------------------------------------------------------

def _basic_md_to_html(md: str) -> str:
    """Minimal markdown → HTML (headings, bold, code, bullets, code blocks, paragraphs)."""
    import re

    def _inline(s):
        s = _html_escape(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
        s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
        return s

    lines = md.splitlines()
    out = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                out.append('<pre style="background:rgba(0,0,0,.4);padding:12px 16px;overflow-x:auto;font-size:12px">')
                in_code = True
            continue
        if in_code:
            out.append(_html_escape(line))
            continue
        if line.startswith("### "):
            out.append(f'<h3 style="color:var(--gold);margin:18px 0 8px;font-family:Fraunces,serif;font-weight:300">{_html_escape(line[4:])}</h3>')
        elif line.startswith("## "):
            out.append(f'<h2 style="color:var(--moon);margin:22px 0 10px;font-family:Fraunces,serif;font-weight:300">{_html_escape(line[3:])}</h2>')
        elif line.startswith("# "):
            out.append(f'<h1 style="color:var(--moon);margin:24px 0 12px;font-family:Fraunces,serif;font-weight:300">{_html_escape(line[2:])}</h1>')
        elif line.lstrip().startswith(("- ", "* ")):
            out.append(f'<li style="margin:3px 0">{_inline(line.lstrip()[2:])}</li>')
        elif line.strip() == "":
            out.append("<br>")
        else:
            out.append(f"<p style='margin:4px 0'>{_inline(line)}</p>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def _is_wsl() -> bool:
    try:
        return "microsoft" in pathlib.Path("/proc/version").read_text().lower()
    except Exception:
        return False


def _wsl_path(abs_path: str, distro: str) -> str:
    """A clickable file:// link to a local path. On WSL, rewrite to the \\\\wsl$ UNC
    so Windows Explorer can open it; elsewhere a plain file:// URL works."""
    if _is_wsl():
        win_path = abs_path.lstrip("/").replace("/", "\\")
        return f"file://///wsl$/{distro}/{win_path}"
    return "file://" + abs_path


def _build_run_html(run_id: str, cfg: dict) -> str:
    """Build the per-run page."""
    # Header power toggle — kill-switch file existence is the source of truth (cheap;
    # avoids a full compute_status()/usage-API round trip just to render a page).
    toggle_html = _pwr_toggle_html(cfg["kill_switch_path"].exists(), gatemod.run_in_flight())
    distro = (cfg.get("wsl") or {}).get("distro", "Ubuntu")
    runs_dir = state.RUNS_DIR
    run_dir = runs_dir / run_id

    if not run_dir.exists():
        return _error_page(f"Run not found: {run_id}")

    # Read metadata
    meta = {}
    meta_f = run_dir / "run.json"
    if meta_f.exists():
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Read summary
    summary_md = ""
    summary_f = run_dir / "summary.md"
    if summary_f.exists():
        summary_md = summary_f.read_text(encoding="utf-8")

    # Read manifest
    manifest = []
    mf = run_dir / "manifest.jsonl"
    if mf.exists():
        for line in mf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    manifest.append(json.loads(line))
                except Exception:
                    pass

    # Build per-op sections
    ops_html = _build_manifest_ops(manifest, run_dir, distro)

    # Read the <style> block from template.html
    tmpl = (HERE / "template.html").read_text(encoding="utf-8")
    style_start = tmpl.find("<style>")
    style_end = tmpl.find("</style>") + 8
    style_block = tmpl[style_start:style_end] if style_start != -1 else ""

    # Header fields
    run_date = _html_escape(str(meta.get("date_human", meta.get("id", run_id))))
    is_apply = bool(meta.get("apply"))
    mode_str = ("✅ applied (your approval)" if is_apply
                else ("dry-run" if meta.get("dry_run") else "full-auto (scheduled)"))
    # time window: started → finished (HH:MM)

    def _hm(iso):
        try:
            return datetime.datetime.fromisoformat(iso).strftime("%H:%M")
        except Exception:
            return ""
    t_start, t_fin = _hm(meta.get("started", "")), _hm(meta.get("finished", ""))
    run_time = _html_escape(f"{t_start} → {t_fin}" if t_fin else t_start)
    run_status = "applied" if is_apply else _html_escape(str(meta.get("status", "—")))
    stop_reason = _html_escape(str(meta.get("stop_reason", "—")))
    duration = _html_escape(str(meta.get("duration_min", "—")))
    delta = meta.get("util_delta", 0) or 0           # weekly %
    five_delta = meta.get("five_delta") or 0         # 5-hour %
    tok_h = _human_tokens(meta.get("tokens", 0))
    cost_parts = []
    if five_delta: cost_parts.append(f"{five_delta}% of 5-hour")
    if delta: cost_parts.append(f"{delta}% of weekly")
    cost_parts.append(f"{tok_h} tokens")
    run_cost = _html_escape(" · ".join(cost_parts))
    rid_esc = _html_escape(run_id)

    def _summary_cards(md):
        md = (md or "").strip()
        if not md:
            return "<p style='color:var(--dim)'>No summary.</p>"
        m = re.search(r'^#{1,4}\s', md, re.M)
        pre = (md[:m.start()] if m else md).strip()
        out = []
        if pre:
            out.append(f'<div class="sumcard lead">{_basic_md_to_html(pre)}</div>')
        for title, body in digestmod._sections(md):
            out.append(f'<div class="sumcard"><div class="sumh">{_html_escape(title)}</div>'
                       f'<div class="sumbody">{_basic_md_to_html(body)}</div></div>')
        return "".join(out) or _basic_md_to_html(md)
    summary_html = _summary_cards(summary_md)

    revert_script = f"""
<script>
async function doRevert() {{
  if (!confirm('Revert run {rid_esc}? This will undo all file changes.')) return;
  const pin = prompt('Enter 6-digit PIN:');
  if (!pin) return;
  const r = await fetch('/api/revert', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{id: '{rid_esc}', pin}})
  }});
  const data = await r.json();
  if (data.ok) {{
    alert('Revert complete!');
  }} else {{
    alert('Revert failed: ' + (data.error || 'unknown error'));
  }}
}}

function copyPath(path) {{
  navigator.clipboard.writeText(path).then(() => {{
    alert('Path copied!');
  }}).catch(() => {{
    prompt('Copy this path:', path);
  }});
}}

// Live run card — only if THIS run is the active one
const THIS_RUN = {json.dumps(run_id)};
let RP_STARTED = null;
function rpEsc(s){{const d=document.createElement('div');d.textContent=s;return d.innerHTML;}}
function rpFmt(sec){{sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;return h>0?(h+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')):(m+':'+String(s).padStart(2,'0'));}}
function rpShow(ar){{
  const card=document.getElementById('run-card');if(!card)return;
  if(ar && ar.active!==false && ar.id===THIS_RUN){{
    card.style.display='';
    if(ar.started)RP_STARTED=new Date(ar.started).getTime();
    const mode=document.getElementById('run-mode');if(mode)mode.textContent=ar.dry_run?'· dry run':'· full-auto';
    const feed=document.getElementById('run-feed');
    if(feed){{const a=ar.activity||[];feed.innerHTML=a.length?a.map((l,i)=>{{const sp=(i===a.length-1)&&(l.indexOf('…')>=0||l.indexOf('tokens)')>=0);return '<div class="'+(sp?'spin':'think')+'">'+rpEsc(l)+'</div>';}}).join(''):'<span class="think">starting…</span>';feed.scrollTop=feed.scrollHeight;}}
  }} else {{ card.style.display='none'; RP_STARTED=null; if(ar&&ar.active===false&&document.getElementById('run-card').dataset.was==='1'){{location.reload();}} }}
  if(ar&&ar.id===THIS_RUN)document.getElementById('run-card').dataset.was='1';
}}
setInterval(()=>{{if(RP_STARTED){{const t=document.getElementById('run-timer');if(t)t.textContent=rpFmt((Date.now()-RP_STARTED)/1000);}}}},1000);
setInterval(async()=>{{try{{const r=await fetch('/api/run-activity');rpShow(await r.json());}}catch(e){{}}}},2500);
fetch('/api/run-activity').then(r=>r.json()).then(rpShow).catch(()=>{{}});
</script>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Moonlighter · Run {rid_esc}</title>
<link href="/fonts.css" rel="stylesheet">
{style_block}
<style>
.applybanner {{background:rgba(168,212,154,.12);border:1px solid rgba(168,212,154,.5);
  color:var(--ok);padding:12px 16px;margin-bottom:18px;font-size:12px;letter-spacing:.14em;text-transform:uppercase}}
.sumcard {{background:rgba(11,16,29,.5);border:1px solid var(--line);border-left:3px solid rgba(220,190,133,.45);padding:14px 18px;margin:0 0 14px}}
.sumcard.lead {{border-left-color:var(--ok);font-family:'Fraunces',serif;font-size:16px;color:#e6ebf5;line-height:1.6}}
.sumh {{color:var(--gold);font-size:14px;letter-spacing:.05em;margin-bottom:8px;font-weight:500}}
.sumbody {{font-size:13.5px;line-height:1.65;color:#cdd6e6;overflow-wrap:anywhere}}
.sumbody b {{color:#fff}} .sumbody code {{color:var(--gold);background:rgba(0,0,0,.3);padding:1px 5px;word-break:break-all}}
.sumbody li {{margin-left:18px;margin-bottom:4px}} .sumbody pre {{white-space:pre-wrap;word-break:break-all;background:rgba(0,0,0,.4);padding:8px 12px;font-size:12px}}
.run-header {{border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:32px}}
.run-meta {{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:16px;margin-top:18px}}
.run-meta-item {{}}
.run-meta-item .label {{margin-bottom:6px}}
.run-meta-item .val {{color:var(--moon);font-size:14px}}
.op-block {{border:1px solid var(--line);margin-bottom:16px;padding:16px 18px;background:rgba(11,16,29,.4)}}
.op-block .path-line {{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}}
.op-block .path-link {{color:var(--blue);text-decoration:none;font-size:12px;word-break:break-all}}
.op-block .path-link:hover {{color:var(--moon)}}
.copy-btn {{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.15em;padding:3px 8px;
  background:transparent;border:1px solid var(--line);color:var(--dim);cursor:pointer}}
.copy-btn:hover {{border-color:var(--dim);color:var(--moon)}}
.diff-wrap {{margin-top:10px}}
.diff-wrap pre {{font-size:11px;line-height:1.6;overflow-x:auto;padding:10px 14px;
  background:rgba(0,0,0,.5);max-height:400px;overflow-y:auto;white-space:pre}}
.diff-add {{color:#a8d49a}} .diff-remove {{color:#e8866c}} .diff-meta {{color:#646f88}}
.runcard {{margin:0 0 30px;border:1px solid rgba(168,212,154,.45);background:rgba(8,12,8,.6)}}
.runcard .bar {{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line)}}
.runcard .bar .label {{margin:0;font-size:10.5px;letter-spacing:.28em;text-transform:uppercase;color:var(--ok);animation:mlpulse 2s ease-in-out infinite}}
.runcard .next {{font-size:11px;color:var(--dim)}} .runcard .next b {{color:var(--moon);font-variant-numeric:tabular-nums}}
@keyframes mlpulse {{0%,100%{{opacity:.55}}50%{{opacity:1}}}}
.runfeed {{padding:14px 22px;font-size:12.5px;line-height:1.85;color:#cdd6e6;max-height:260px;overflow-y:auto}}
.runfeed .think {{color:#d3dbe9;padding:1px 0}} .runfeed .spin {{color:var(--gold);padding-top:4px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <a href="/" class="brand" style="text-decoration:none;color:inherit">
      <div class="crescent"></div>
      <h1>Moon<em>lighter</em></h1>
    </a>
    <div class="hdrright">
      {toggle_html}
      <div class="mode"><a href="/" style="color:var(--dim);text-decoration:none">← back</a></div>
    </div>
  </header>

  <section class="runcard" id="run-card" style="display:none">
    <div class="bar">
      <div class="label">● Run in progress <span id="run-mode" style="color:var(--blue)"></span></div>
      <div class="next">elapsed <b id="run-timer">0:00</b></div>
    </div>
    <div class="runfeed" id="run-feed"></div>
  </section>

  {'<div class="applybanner">✅ APPROVED-APPLY RUN — it did the items you ticked</div>' if is_apply else ''}
  <div class="run-header">
    <div class="label">Run {rid_esc}</div>
    <div class="run-meta">
      <div class="run-meta-item"><div class="label">Date</div><div class="val">{run_date}</div></div>
      <div class="run-meta-item"><div class="label">Time</div><div class="val">{run_time}</div></div>
      <div class="run-meta-item"><div class="label">Type</div><div class="val">{mode_str}</div></div>
      <div class="run-meta-item"><div class="label">Status</div><div class="val">{run_status}</div></div>
      <div class="run-meta-item"><div class="label">Stop reason</div><div class="val">{stop_reason}</div></div>
      <div class="run-meta-item"><div class="label">Duration</div><div class="val">{duration} min</div></div>
      <div class="run-meta-item" style="grid-column:span 2"><div class="label">This run cost</div><div class="val">{run_cost}</div></div>
    </div>
  </div>

  <div class="label" style="margin-bottom:14px">What happened</div>
  <div style="margin-bottom:36px;line-height:1.9;color:#d6dce8">{summary_html}</div>

  <div class="label" style="margin-bottom:14px">Changes ({len(manifest)} ops)</div>
  {ops_html if ops_html else '<p style="color:var(--dim)">No file operations recorded.</p>'}

  <div style="margin-top:36px">
    <button class="btn" style="border-color:var(--gold);color:var(--gold)" onclick="doRevert()">
      ⟲ Revert this run
    </button>
  </div>

  <footer>moonlighter · works while you sleep · everything revertible</footer>
</div>
{revert_script}
{_pwr_toggle_script()}
</body>
</html>"""


def _build_manifest_ops(manifest: list, run_dir: pathlib.Path, distro: str) -> str:
    if not manifest:
        return ""

    def file_link(path: str) -> str:
        unc = _wsl_path(path, distro)
        esc_path = _html_escape(path)
        esc_unc = _html_escape(unc)
        return (
            f'<a class="path-link" href="{esc_unc}" title="{esc_unc}">{esc_path}</a>'
            f'<button class="copy-btn" onclick="copyPath({json.dumps(path)})">copy</button>'
        )

    def inline_diff(abs_path: str, snap_path: pathlib.Path) -> str:
        """Show before (snap) vs after (current) diff, capped at 80 lines."""
        before_lines = []
        after_lines = []
        if snap_path.exists():
            try:
                before_lines = snap_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except Exception:
                pass
        cur = pathlib.Path(abs_path)
        if cur.exists():
            try:
                after_lines = cur.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except Exception:
                pass
        if not before_lines and not after_lines:
            return ""
        diff = list(difflib.unified_diff(
            before_lines, after_lines,
            fromfile="before", tofile="after", lineterm=""
        ))
        if not diff:
            return '<p style="color:var(--dim);font-size:11px;margin-top:6px">No textual changes.</p>'
        if len(diff) > 80:
            diff = diff[:80]
            diff.append("… (diff truncated at 80 lines)")

        def color_line(line: str) -> str:
            if line.startswith("+") and not line.startswith("+++"):
                return f'<span class="diff-add">{_html_escape(line)}</span>'
            if line.startswith("-") and not line.startswith("---"):
                return f'<span class="diff-remove">{_html_escape(line)}</span>'
            if line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
                return f'<span class="diff-meta">{_html_escape(line)}</span>'
            return _html_escape(line)

        colored = "\n".join(color_line(l) for l in diff)
        return f'<div class="diff-wrap"><pre>{colored}</pre></div>'

    blocks = []
    for rec in manifest:
        op = rec.get("op", "?")
        block_lines = [f'<div class="op-block">']
        block_lines.append(f'<div style="font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--dim);margin-bottom:8px">{_html_escape(op)}</div>')

        if op in ("snapshot", "write-begin"):
            path = rec.get("path", "")
            snap_rel = str(pathlib.Path(path)).lstrip("/")
            snap_path = run_dir / "snapshot" / snap_rel
            block_lines.append(f'<div class="path-line">{file_link(path)}</div>')
            diff_html = inline_diff(path, snap_path)
            if diff_html:
                block_lines.append(diff_html)

        elif op == "move":
            src = rec.get("src", "")
            dst = rec.get("dst", "")
            block_lines.append(f'<div class="path-line"><span style="color:var(--dim);font-size:11px">from</span> {file_link(src)}</div>')
            block_lines.append(f'<div class="path-line"><span style="color:var(--dim);font-size:11px">to</span>   {file_link(dst)}</div>')

        elif op == "trash":
            path = rec.get("path", "")
            trash = rec.get("trash", "")
            block_lines.append(f'<div class="path-line">{file_link(path)}</div>')
            if trash:
                block_lines.append(f'<div style="font-size:11px;color:var(--dim)">trashed to {_html_escape(trash)}</div>')

        elif op == "created":
            path = rec.get("path", "")
            block_lines.append(f'<div class="path-line">{file_link(path)}</div>')

        elif op == "note":
            note = rec.get("note", "")
            block_lines.append(f'<p style="color:#d6dce8;font-size:13px">{_html_escape(note)}</p>')

        else:
            block_lines.append(f'<pre>{_html_escape(json.dumps(rec, indent=2))}</pre>')

        block_lines.append("</div>")
        blocks.append("\n".join(block_lines))

    return "\n".join(blocks)


def _build_night_html(cfg: dict) -> str:
    # Header power toggle — kill-switch file existence is the source of truth (cheap;
    # avoids a full compute_status()/usage-API round trip just to render a page).
    toggle_html = _pwr_toggle_html(cfg["kill_switch_path"].exists(), gatemod.run_in_flight())
    d = digestmod.build_night()
    tmpl = (HERE / "template.html").read_text(encoding="utf-8")
    s, e = tmpl.find("<style>"), tmpl.find("</style>") + 8
    style = tmpl[s:e] if s != -1 else ""

    # shared registry: each approvable item gets ONE id, so a Top card and the
    # category/proposal card for the same thing carry the same id and sync together.
    registry, ridx = [], {}

    def _item_id(key, payload):
        if key not in ridx:
            ridx[key] = len(registry)
            registry.append(payload)
        return ridx[key]

    # proposals checklist
    prop_html = []
    for i, p in enumerate(d["proposals"]):
        cmds = "\n".join(p.get("commands", []))
        iid = _item_id("p:%d" % i, {"kind": "proposal", "i": i})
        prop_html.append(
            f'<div class="row"><label><input type="checkbox" class="do" data-id="{iid}"> '
            f'<b>{_html_escape(p["title"])}</b></label>'
            f'<pre class="cmd">{_html_escape(cmds)}</pre></div>')
    prop_block = "\n".join(prop_html) if prop_html else '<p class="dimp">No structured proposals (see per-run reports).</p>'

    # done / revert checklist, grouped by run
    run_html = []
    for r in d["runs"]:
        if not r["items"]:
            continue
        rows = []
        for it in r["items"]:
            rows.append(
                f'<div class="row"><label><input type="checkbox" class="rev" '
                f'data-run="{_html_escape(r["id"])}" data-idx="{it["idx"]}"> '
                f'<span class="k k-{it["kind"]}">{it["kind"]}</span> '
                f'{_html_escape(it["desc"])}</label></div>')
        fd = r.get("five_delta")
        spend = (f'{fd}% 5h · ' if fd else '') + f'{_human_tokens(r.get("tokens", 0))} tok'
        run_html.append(
            f'<div class="runblock"><div class="rb-head">'
            f'<label><input type="checkbox" class="rev-all" data-run="{_html_escape(r["id"])}"> '
            f'<b>{r["time"]} · {_html_escape(r["headline"][:80])}</b></label>'
            f'<span class="rb-view">{spend} · <a href="/run/{_html_escape(r["id"])}" '
            f'style="color:var(--gold);text-decoration:none">report →</a></span></div>'
            + "\n".join(rows) + "</div>")
    runs_block = "\n".join(run_html) if run_html else '<p class="dimp">Nothing was changed (audit-only runs).</p>'

    def _md_inline(s):
        s = _html_escape(s)
        s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
        s = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', s)
        return s

    def _badges(item):
        """Capability badges for a structured todo: sudo / push / risk."""
        if not isinstance(item, dict):
            return ""
        b = []
        if item.get("needs_sudo"):
            b.append('<span class="bdg bdg-sudo">sudo</span>')
        if item.get("needs_push"):
            repo = _html_escape(item.get("repo") or "remote")
            b.append(f'<span class="bdg bdg-push">push→{repo}</span>')
        if item.get("risk") and item["risk"] != "low":
            b.append(f'<span class="bdg bdg-risk bdg-{_html_escape(item["risk"])}">{_html_escape(item["risk"])} risk</span>')
        return ("<span class=\"bdgs\">" + "".join(b) + "</span>") if b else ""

    def _finding_card(item, tickable=False):
        """One finding/todo as a card: bold lead-in heading + dimmer detail + badges.
        `item` is the digest dict (text/task/runs/flags). tickable=True makes it an
        approvable task — a checkbox tied to the shared registry id, so the Top card and
        the category card for the same finding stay in sync. The registry carries the
        item's FULL `task` (text + capability hints), never a truncated display string."""
        if isinstance(item, dict):
            text = item.get("text", "")
            task = item.get("task") or text
            runs = len(item.get("runs", []))
        else:
            text, task, runs = item, item, 0
        seen = f'<span class="seen">{runs}× seen</span>' if runs > 1 else ""
        badges = _badges(item)
        mm = re.match(r'^(.{6,80}?)(\s[—–-]\s|:\s)(.*)$', text, re.S)
        if mm:
            body = (f'<span class="fh">{_md_inline(mm.group(1))}</span>'
                    f'{_html_escape(mm.group(2))}{_md_inline(mm.group(3))}')
        else:
            body = f'<span class="fh">{_md_inline(text)}</span>'
        if tickable:
            iid = _item_id("f:" + text, {"kind": "finding", "task": task})
            return (f'<label class="finding tick"><input type="checkbox" class="do" data-id="{iid}">'
                    f'<span class="fc">{body}{seen}{badges}</span></label>')
        return f'<div class="finding"><span class="fc">{body}{seen}{badges}</span></div>'

    # collapsible categories — DEDUPED findings, each an approvable (tickable) card
    def _cat(label, items, open_=False):
        if not items:
            return ""
        inner = "".join(_finding_card(it, tickable=True) for it in items)
        allbox = (f'<label class="catall"><input type="checkbox" class="sel-all"> '
                  f'Select all {len(items)} to apply</label>')
        op = " open" if open_ else ""
        return (f'<details class="cat"{op}><summary>{label}'
                f'<span class="catn">{len(items)}</span></summary>'
                f'<div class="catwrap">{allbox}{inner}</div></details>')

    cats = (_cat("🔒 Security", d.get("security", []), True)
            + _cat("💡 Ideas &amp; recommendations", d.get("ideas", []), False)
            + _cat("🔍 Estate audit", d.get("audit", []), False))

    n_prop, n_rev = len(d["proposals"]), sum(len(r["items"]) for r in d["runs"])
    prop_allbox = (f'<label class="catall"><input type="checkbox" class="sel-all"> '
                   f'Select all {n_prop} to apply</label>') if n_prop else ""
    prop_det = (f'<details class="cat" open><summary>▶ Proposals — tick to DO'
                f'<span class="catn">{n_prop}</span></summary>'
                f'<div class="catwrap">{prop_allbox}{prop_block}</div></details>')
    keepnote = ('<div class="keepnote">Everything here is already applied and <b>kept</b> by default — '
                'tick only what you want UNDONE. Leaving an item unticked keeps it.</div>')
    runs_det = (f'<details class="cat" open><summary>✅ What was done — tick to REVERT'
                f'<span class="catn">{n_rev}</span></summary><div class="catwrap">{keepnote}{runs_block}</div></details>')

    # holistic overview
    def stat(n, l):
        return f'<div class="ovcell"><div class="ovn">{n}</div><div class="ovl">{l}</div></div>'
    overview = (
        '<div class="ovgrid">'
        + stat(d.get("run_count", 0), "runs")
        + stat(f'{d.get("total_min",0)}m', "active")
        + stat(_human_tokens(d.get("total_tokens", 0)), "tokens")
        + stat(d.get("changes", 0), "changes")
        + stat(len(d.get("security", [])), "security")
        + stat(len(d.get("ideas", [])), "ideas")
        + stat(len(d.get("audit", [])), "audit")
        + stat(n_prop, "proposals")
        + '</div>')
    # top highlights — tickable cards that SHARE ids with the category/proposal cards
    def _top_proposal(i, title):
        iid = _item_id("p:%d" % i, {"kind": "proposal", "i": i})
        return (f'<label class="finding tick"><input type="checkbox" class="do" data-id="{iid}">'
                f'<span class="fc"><span class="fh">{_md_inline(title)}</span></span></label>')

    def _topwrap(label, cards):
        cards = [c for c in cards if c][:3]
        if not cards:
            return ""
        return f'<div class="tophl"><div class="toph">{label}</div>{"".join(cards)}</div>'
    tops = (_topwrap("Top security", [_finding_card(s, tickable=True) for s in d.get("security", [])])
            + _topwrap("Top ideas", [_finding_card(i, tickable=True) for i in d.get("ideas", [])])
            + _topwrap("Top proposals", [_top_proposal(i, p["title"]) for i, p in enumerate(d["proposals"])]))

    registry_json = json.dumps(registry)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Moonlighter · Last night</title>
<link href="/fonts.css" rel="stylesheet">
{style}
<style>
.dimp{{color:var(--dim)}}
.nsum{{font-family:'Fraunces',serif;font-style:italic;font-size:18px;color:#e2e7f2;margin:8px 0 18px}}
.ovgrid{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin:6px 0 16px}}
.ovcell{{background:var(--ink);padding:14px 10px;text-align:center}}
.ovn{{font-family:'Archivo Black',sans-serif;font-size:24px;color:var(--gold);line-height:1}}
.ovl{{font-size:9.5px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);margin-top:6px}}
.tophl{{margin:14px 0}}
.toph{{color:var(--gold);font-size:15px;letter-spacing:.14em;text-transform:uppercase;margin-bottom:8px}}
.tophl ul{{margin:0 0 6px 20px}} .tophl li{{margin:8px 0;line-height:1.55;font-size:15px;color:#e6ebf5}}
.tophl code{{color:var(--gold);background:rgba(0,0,0,.3);padding:1px 5px;font-size:14px}}
.tophl b{{color:#fff}}
.row{{border-bottom:1px solid var(--line);padding:10px 2px}}
.row label{{display:flex;gap:11px;align-items:flex-start;cursor:pointer;font-size:14.5px;color:#e6ebf5;line-height:1.55}}
.row input{{margin-top:3px;accent-color:var(--gold);width:17px;height:17px;flex:none}}
.cmd{{margin:8px 0 4px 28px;font-size:11.5px;background:rgba(0,0,0,.45);padding:8px 12px;overflow-x:auto;color:#cdd6e6}}
.k{{font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;padding:2px 7px;border:1px solid var(--line);flex:none}}
.k-trashed{{color:var(--fail)}} .k-moved{{color:var(--blue)}} .k-created{{color:var(--ok)}}
.k-edited{{color:var(--gold)}} .k-perms{{color:var(--gold)}}
.catwrap{{padding:4px 18px 14px}}
.runblock{{border:1px solid var(--line);margin:12px 0;padding:4px 14px 10px}}
.rb-head{{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}}
.rb-head label{{font-size:13.5px;color:var(--moon)}} .rb-view{{color:var(--dim);font-size:11px;white-space:nowrap}}
/* collapsible category headers — LARGE + readable */
.cat{{border:1px solid var(--line);margin:14px 0;background:rgba(11,16,29,.4)}}
.cat>summary{{cursor:pointer;padding:16px 18px;font-family:'Fraunces',serif;font-size:18px;
  color:var(--moon);list-style:none;display:flex;justify-content:space-between;align-items:center}}
.cat>summary::-webkit-details-marker{{display:none}}
.cat>summary::after{{content:"▸";color:var(--dim);font-family:monospace;font-size:14px}}
.cat[open]>summary::after{{content:"▾"}}
.cat .catn{{color:var(--gold);font-family:'IBM Plex Mono',monospace;font-size:14px;margin-left:auto;margin-right:14px}}
/* deduped findings — each a distinct card: bold lead-in heading + detail */
.finding{{background:rgba(11,16,29,.55);border:1px solid var(--line);border-left:3px solid rgba(220,190,133,.45);
  padding:13px 16px;margin:0 0 12px;font-size:14px;line-height:1.65;color:#aeb8cb}}
.finding .fh{{color:#fff;font-weight:500;font-size:15px}}
.finding code{{color:var(--gold);background:rgba(0,0,0,.35);padding:1px 5px;font-size:13.5px}}
.finding b{{color:#eef2f8}}
.seen{{display:inline-block;margin-left:8px;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--hold);border:1px solid rgba(220,190,133,.4);padding:1px 7px;vertical-align:middle}}
.keepnote{{font-size:13px;color:#aeb8cb;margin:2px 0 10px;font-style:italic}}
.bdgs{{display:inline-flex;gap:6px;flex-wrap:wrap;margin-left:8px;vertical-align:middle}}
.bdg{{display:inline-block;font-size:10px;letter-spacing:.06em;text-transform:uppercase;
  padding:1px 7px;border-radius:3px;font-family:'IBM Plex Mono',monospace}}
.bdg-sudo{{color:#ffd9a0;border:1px solid rgba(255,180,90,.5);background:rgba(255,150,60,.08)}}
.bdg-push{{color:#a9d0ff;border:1px solid rgba(120,170,240,.5);background:rgba(90,140,240,.08)}}
.bdg-risk{{color:#e8e2d4;border:1px solid rgba(220,190,133,.4)}}
.bdg-medium{{color:#ffcf8a;border-color:rgba(255,180,90,.5)}}
.bdg-high{{color:#ff9a9a;border-color:rgba(240,120,120,.6);background:rgba(240,90,90,.08)}}
/* ALWAYS-visible action bar (fixed to viewport bottom, not buried at page end) */
.applybar{{position:fixed;left:0;right:0;bottom:0;z-index:40;background:rgba(7,10,18,.97);
  backdrop-filter:blur(10px);border-top:1px solid var(--line);box-sizing:border-box;
  padding:10px 12px calc(10px + env(safe-area-inset-bottom));display:flex;gap:10px;align-items:center}}
.applybar .btn{{flex:1 1 50%;min-width:0;max-width:none;padding:14px 4px;text-align:center;
  font-size:11px;letter-spacing:.06em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.wrap{{padding-bottom:104px}}
/* kill horizontal overflow — long paths/commands must wrap, not widen the page
   (page wider than the screen was breaking the fixed bar's alignment on mobile) */
html,body{{overflow-x:hidden;max-width:100%}}
.wrap{{overflow-x:hidden;box-sizing:border-box}}
.finding,.row label,.catbody,.fc,.tophl li,.rb-head{{overflow-wrap:anywhere;word-break:break-word}}
code{{word-break:break-all}}
.cmd,.catbody pre{{white-space:pre-wrap;word-break:break-all;max-width:100%;box-sizing:border-box}}
.finding.tick{{display:flex;gap:12px;align-items:flex-start;cursor:pointer}}
.finding.tick input{{margin-top:3px;accent-color:var(--gold);width:18px;height:18px;flex:none}}
.finding .fc{{flex:1}}
.finding.tick:has(.do:checked){{border-left-color:var(--gold);background:rgba(212,180,120,.08)}}
.catall{{display:flex;gap:10px;align-items:center;padding:11px 14px;margin-bottom:10px;
  font-size:12.5px;color:var(--gold);letter-spacing:.05em;border-bottom:1px solid var(--line);cursor:pointer}}
.catall input{{accent-color:var(--gold);width:17px;height:17px}}
#btn-revapply{{border-color:var(--fail);color:var(--fail)}}
#btn-revapply:hover{{background:rgba(232,134,108,.12)}}
.btn[disabled]{{opacity:.3;pointer-events:none}}
</style></head><body><div class="wrap">
<header><a href="/" class="brand" style="text-decoration:none;color:inherit"><div class="crescent"></div><h1>Moon<em>lighter</em></h1></a>
<div class="hdrright">{toggle_html}<div class="mode"><a href="/" style="color:var(--dim);text-decoration:none">← dashboard</a></div></div></header>

<div class="label">Last night — {d['date']}</div>
<div class="nsum">{_html_escape(d['summary'])}</div>

{overview}
{tops}

{cats}
{prop_det}
{runs_det}

<div class="applybar">
  <button class="btn primary" id="btn-doapply" disabled>Apply 0 proposals</button>
  <button class="btn" id="btn-revapply" disabled>Revert 0 selected</button>
</div>
</div>
<script>
function revSel() {{
  const rev = [...document.querySelectorAll('.rev:checked')].map(c=>({{run:c.dataset.run, idx:+c.dataset.idx}}));
  document.querySelectorAll('.rev-all:checked').forEach(c=>{{
    document.querySelectorAll('.rev[data-run="'+c.dataset.run+'"]').forEach(x=>{{
      if(!rev.find(r=>r.run===x.dataset.run&&r.idx===+x.dataset.idx)) rev.push({{run:x.dataset.run, idx:+x.dataset.idx}});
    }});
  }});
  return rev;
}}
const REG = {registry_json};
function doSel() {{
  const ids = new Set([...document.querySelectorAll('.do:checked')].map(c=>+c.dataset.id));
  return [...ids].map(id=>REG[id]).filter(Boolean);
}}
function upd() {{
  const nr=revSel().length, nd=doSel().length;
  const db=document.getElementById('btn-doapply'), rb=document.getElementById('btn-revapply');
  db.textContent='Apply '+nd+' item'+(nd===1?'':'s'); db.disabled=!nd;
  rb.textContent='Revert '+nr+' selected'; rb.disabled=!nr;
}}
document.addEventListener('change', e=>{{
  if(e.target.classList.contains('rev-all')){{
    document.querySelectorAll('.rev[data-run="'+e.target.dataset.run+'"]').forEach(x=>x.checked=e.target.checked);
  }}
  if(e.target.classList.contains('do')){{   // sync the same item wherever it appears (top ↔ category)
    document.querySelectorAll('.do[data-id="'+e.target.dataset.id+'"]').forEach(x=>{{x.checked=e.target.checked;}});
  }}
  if(e.target.classList.contains('sel-all')){{   // tick/untick a whole category
    const cat=e.target.closest('.cat');
    cat.querySelectorAll('.do').forEach(x=>{{
      x.checked=e.target.checked;
      document.querySelectorAll('.do[data-id="'+x.dataset.id+'"]').forEach(y=>y.checked=e.target.checked);
    }});
  }}
  upd();
}});
async function post(payload){{
  const pin=prompt('Enter 6-digit PIN:'); if(!pin) return;
  payload.pin=pin;
  const r=await fetch('/api/apply',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
  const data=await r.json();
  if(data.ok){{alert('Done.\\n'+(data.message||'')); location.reload();}}
  else alert('Error: '+(data.error||'unknown'));
}}
document.getElementById('btn-doapply').addEventListener('click', async ()=>{{
  const dop=doSel(); if(!dop.length) return;
  if(!confirm('Apply '+dop.length+' approved item(s)?\\n\\nA Moonlighter agent will open and DO them (reversibly, logged). It takes a few minutes — watch the live tile on the dashboard.')) return;
  const pin=prompt('Enter 6-digit PIN:'); if(!pin) return;
  const r=await fetch('/api/apply',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{revert:[], do:dop, pin}})}});
  const data=await r.json();
  if(data.ok){{ alert((data.agent_started?'✅ Agent started — doing your approved items now. ':'')+(data.message||'')); }}
  else alert('Error: '+(data.error||'unknown'));
}});
document.getElementById('btn-revapply').addEventListener('click', ()=>{{
  const rev=revSel(); if(!rev.length) return;
  if(confirm('Revert '+rev.length+' change(s)? Restores them byte-for-byte.')) post({{revert:rev, do:[]}});
}});
upd();
</script>
{_pwr_toggle_script()}
</body></html>"""


def _error_page(msg: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Error</title></head>
<body style="background:#070a12;color:#eee9dd;font-family:monospace;padding:40px">
<h2 style="color:#e8866c">Error</h2><p>{_html_escape(msg)}</p>
<a href="/" style="color:#9db5e2">← back</a>
</body></html>"""


_SETUP_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Moonlighter · Setup</title>
<link href="/fonts.css" rel="stylesheet">
<style>
:root{--bg:#070a12;--card:#0c1322;--line:#1d2940;--gold:#d4b478;--ink:#e8e2d4;--dim:#94a0b8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:'IBM Plex Sans',system-ui,sans-serif;
 line-height:1.55;-webkit-text-size-adjust:100%}
.wrap{max-width:760px;margin:0 auto;padding:26px 18px 130px}
h1{font-family:'IBM Plex Mono',monospace;font-weight:500;letter-spacing:.04em;font-size:26px;margin:0}
h1 .moon{color:var(--gold)}
.sub{color:var(--dim);margin:4px 0 22px;font-size:14px}
.dots{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:20px}
.dot{height:6px;flex:1;min-width:24px;border-radius:3px;background:var(--line);transition:.2s}
.dot.on{background:var(--gold)}
.dot.done{background:rgba(212,180,120,.5)}
.step{display:none;animation:fade .25s ease}
.step.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 20px;margin:0 0 16px}
.h2{font-size:20px;font-weight:600;margin:0 0 6px}
.lead{color:var(--dim);font-size:14.5px;margin:0 0 16px}
label.fld{display:block;margin:16px 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--gold)}
input[type=text],input[type=number],input[type=password]{width:100%;background:#0a1120;border:1px solid var(--line);
 color:var(--ink);border-radius:8px;padding:11px 12px;font-size:15px;font-family:inherit}
input:focus{outline:none;border-color:var(--gold)}
.range{display:flex;align-items:center;gap:14px}
.range input[type=range]{flex:1;accent-color:var(--gold)}
.rv{font-family:'IBM Plex Mono',monospace;color:#fff;min-width:64px;text-align:right;font-size:17px}
.eff{font-size:13px;color:var(--dim);margin-top:6px}
.opt{display:flex;gap:12px;align-items:flex-start;background:#0a1120;border:1px solid var(--line);
 border-radius:9px;padding:12px 14px;margin:8px 0;cursor:pointer}
.opt.sel{border-color:var(--gold);background:rgba(212,180,120,.07)}
.opt input{margin-top:3px;accent-color:var(--gold);width:18px;height:18px;flex:none}
.opt .t{font-weight:600}.opt .d{font-size:13px;color:var(--dim)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.chip{display:inline-flex;align-items:center;gap:8px;background:#0a1120;border:1px solid var(--line);
 border-radius:20px;padding:6px 12px;font-size:13.5px;font-family:'IBM Plex Mono',monospace}
.chip.off{border-color:rgba(240,120,120,.5);color:#ffb4b4}
.chip.work{border-color:rgba(120,200,140,.5);color:#bfe9c8}
.chip b{cursor:pointer;color:var(--dim);font-size:16px;line-height:1}
.chip b:hover{color:#fff}
.btn{background:#101a2e;border:1px solid var(--line);color:var(--ink);border-radius:8px;
 padding:10px 16px;font-size:14px;font-family:inherit;cursor:pointer;transition:.15s}
.btn:hover{border-color:var(--gold)}
.btn.primary{background:var(--gold);color:#1a1407;border-color:var(--gold);font-weight:600}
.btn.ghost{background:transparent}
.btn:disabled{opacity:.45;cursor:not-allowed}
.small{font-size:12.5px;color:var(--dim)}
/* folder browser */
.browser{border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden;display:none}
.browser.open{display:block}
.bcrumb{display:flex;align-items:center;gap:8px;padding:10px 12px;background:#0a1120;border-bottom:1px solid var(--line);
 font-family:'IBM Plex Mono',monospace;font-size:12.5px;color:var(--dim);overflow-wrap:anywhere}
.blist{max-height:300px;overflow:auto}
.brow{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid rgba(29,41,64,.5)}
.brow:last-child{border-bottom:none}
.brow .nav{flex:1;cursor:pointer;font-family:'IBM Plex Mono',monospace;font-size:14px;color:var(--ink);overflow-wrap:anywhere}
.brow .nav:hover{color:var(--gold)}
.brow .nav .ic{color:var(--gold);margin-right:7px}
.mini{background:#101a2e;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:4px 9px;
 font-size:12px;cursor:pointer;white-space:nowrap}
.mini:hover{border-color:var(--gold)}
.bbar{display:flex;gap:8px;padding:10px 12px;background:#0a1120;border-top:1px solid var(--line);align-items:center}
.audit-row{display:flex;gap:8px;margin:8px 0}
.audit-row input{flex:1}
.navbar{position:fixed;left:0;right:0;bottom:0;background:rgba(7,10,18,.97);backdrop-filter:blur(10px);
 border-top:1px solid var(--line);padding:14px 18px;display:flex;gap:10px;justify-content:space-between;
 max-width:760px;margin:0 auto}
.navbar .btn{flex:1}
.rev-line{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-bottom:1px solid rgba(29,41,64,.5);font-size:14px}
.rev-line .k{color:var(--dim)}.rev-line .v{text-align:right;font-family:'IBM Plex Mono',monospace}
.toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:#101a2e;border:1px solid var(--gold);
 color:var(--ink);padding:12px 18px;border-radius:9px;font-size:14px;display:none;z-index:60;max-width:90%}
.unlock{position:fixed;inset:0;background:rgba(7,10,18,.96);z-index:80;display:none;align-items:center;justify-content:center;padding:20px}
.unlock.open{display:flex}
.unlock .box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;max-width:340px;width:100%}
</style></head><body>
<div class="unlock" id="unlock"><div class="box">
  <div class="h2">Reconfigure Moonlighter</div>
  <p class="small">Enter your current PIN to change the setup.</p>
  <input type="password" id="authpin" inputmode="numeric" placeholder="PIN" autocomplete="off">
  <button class="btn primary" style="width:100%;margin-top:12px" onclick="doUnlock()">Unlock</button>
  <p class="small" id="unlockerr" style="color:#ffb4b4;display:none">Wrong PIN.</p>
</div></div>

<div class="wrap">
  <h1><span class="moon">◖</span> Moonlighter</h1>
  <div class="sub" id="subt">First-run setup — about 2 minutes.</div>
  <div class="dots" id="dots"></div>

  <!-- STEP 0: intro -->
  <div class="step" data-step="0"><div class="card">
    <div class="h2">What Moonlighter does</div>
    <p class="lead">Moonlighter is Claude working for you in the hours you're <b>not</b> using it —
    overnight, while you sleep. It tidies and audits your machine, fixes safe things, and leaves a
    morning report of everything it did and everything it suggests.</p>
    <p class="lead">It spends only the subscription capacity you'd otherwise <b>waste</b> — your
    rate-limit window refills every few hours whether you use it or not, so Moonlighter fills the
    idle window and stops well before it could touch what you've reserved for yourself.</p>
    <p class="lead">Everything it changes is <b>reversible</b> (one-click undo), it never pushes,
    deploys, or messages anyone, and it never touches your secrets. You approve anything bigger
    from a simple checklist. This wizard sets up <b>your</b> budget, folders, and devices.</p>
  </div></div>

  <!-- STEP 1: budget -->
  <div class="step" data-step="1"><div class="card">
    <div class="h2">Budget &amp; mode</div>
    <p class="lead">How much of your spare capacity may Moonlighter use, and may it act on its own?</p>
    <div class="opt" id="opt-auto" onclick="setMode('full-auto')"><input type="radio" name="mode" id="m-auto">
      <div><div class="t">Full-auto</div><div class="d">Acts on safe things itself (all reversible); asks approval for bigger items. Recommended.</div></div></div>
    <div class="opt" id="opt-obs" onclick="setMode('observe')"><input type="radio" name="mode" id="m-obs">
      <div><div class="t">Review only</div><div class="d">Only investigates and proposes — changes nothing until you approve each item.</div></div></div>
    <label class="fld">Fill each idle 5-hour window up to</label>
    <div class="range"><input type="range" id="five" min="10" max="100" step="5"><span class="rv" id="fivev"></span></div>
    <div class="eff" id="fivee"></div>
    <label class="fld">Always keep this much of your weekly limit for yourself</label>
    <div class="range"><input type="range" id="res" min="0" max="90" step="5"><span class="rv" id="resv"></span></div>
    <div class="eff" id="rese"></div>
    <label class="fld">Maximum length of a single overnight run (minutes)</label>
    <input type="number" id="wall" min="30" max="720" step="30">
  </div></div>

  <!-- STEP 2: folders -->
  <div class="step" data-step="2"><div class="card">
    <div class="h2">Folders to work in</div>
    <p class="lead">Pick the folders Moonlighter may tidy and act in (reversibly). Mark anything
    sensitive as <b>off-limits</b> — it'll never be read or touched. Your secrets/keys are always
    off-limits regardless.</p>
    <label class="fld">Work here</label>
    <div class="chips" id="workchips"></div>
    <button class="btn" onclick="openBrowser('work')">+ Add a work folder</button>
    <label class="fld" style="margin-top:18px">Never touch (off-limits)</label>
    <div class="chips" id="offchips"></div>
    <button class="btn" onclick="openBrowser('off')">+ Add an off-limits folder</button>
    <div class="browser" id="browser"></div>
  </div></div>

  <!-- STEP 3: vault -->
  <div class="step" data-step="3"><div class="card">
    <div class="h2">Notes vault <span class="small">(optional)</span></div>
    <p class="lead">If you keep an Obsidian-style notes vault, Moonlighter can do upkeep on it
    (fix broken links, stale statuses, orphan notes). Skip if you don't have one.</p>
    <div class="chips" id="vaultchips"></div>
    <button class="btn" onclick="openBrowser('vault')">Pick vault folder</button>
    <button class="btn ghost" onclick="clearVault()">Skip / none</button>
    <div class="browser" id="browser3"></div>
  </div></div>

  <!-- STEP 4: devices -->
  <div class="step" data-step="4"><div class="card">
    <div class="h2">Devices</div>
    <p class="lead">Not everyone has a home server. Tell Moonlighter what you've got — all optional.</p>
    <label class="fld">Off-box backup destination</label>
    <p class="small">Where at-risk work (unpushed git, local-only repos) can be backed up.</p>
    <div class="opt" id="bk-none" onclick="setBackup('none')"><input type="radio" name="bk"><div><div class="t">None</div><div class="d">Don't propose off-box backups.</div></div></div>
    <div class="opt" id="bk-ssh" onclick="setBackup('ssh')"><input type="radio" name="bk"><div><div class="t">A remote box over SSH</div><div class="d">e.g. a Raspberry Pi or another machine you can <code>ssh</code> to.</div></div></div>
    <div class="opt" id="bk-mount" onclick="setBackup('mount')"><input type="radio" name="bk"><div><div class="t">A mounted drive / folder</div><div class="d">An external disk or network share path.</div></div></div>
    <div id="bk-ssh-fields" style="display:none">
      <label class="fld">SSH host</label><input type="text" id="bkhost" placeholder="e.g. myserver or user@10.0.0.5">
      <label class="fld">Destination path on that host</label><input type="text" id="bkdest" placeholder="backups/git-bundles">
    </div>
    <div id="bk-mount-fields" style="display:none">
      <label class="fld">Destination path</label><input type="text" id="bkmount" placeholder="/mnt/d/backups">
    </div>
    <label class="fld" style="margin-top:18px">Other devices to audit (read-only)</label>
    <p class="small">Machines Moonlighter should check the health of over SSH (disk, services, errors) — never change. Leave empty if none.</p>
    <div id="auditrows"></div>
    <button class="btn" onclick="addAudit()">+ Add a device</button>
  </div></div>

  <!-- STEP 5: notifications -->
  <div class="step" data-step="5"><div class="card">
    <div class="h2">Notifications</div>
    <p class="lead">The morning report always appears in this panel. Optionally also:</p>
    <label class="opt"><input type="checkbox" id="n-toast"><div><div class="t">Desktop notification</div><div class="d">A popup when a run finishes — Windows toast, macOS notification, or Linux <code>notify-send</code>.</div></div></label>
    <label class="opt"><input type="checkbox" id="n-vault"><div><div class="t">Append to my notes vault</div><div class="d">Log each run to a changelog note in your vault.</div></div></label>
    <label class="opt"><input type="checkbox" id="n-ntfy"><div><div class="t">ntfy push to phone</div><div class="d">Push the report to the ntfy app (configure topics in config later).</div></div></label>
  </div></div>

  <!-- STEP 6: pin -->
  <div class="step" data-step="6"><div class="card">
    <div class="h2">Set your PIN</div>
    <p class="lead">A 4–8 digit PIN protects every action (start, approve, reconfigure) — so only you
    can drive Moonlighter, even though the panel is reachable on your private network.</p>
    <label class="fld">PIN</label><input type="password" id="pin1" inputmode="numeric" placeholder="4–8 digits" autocomplete="off">
    <label class="fld">Confirm PIN</label><input type="password" id="pin2" inputmode="numeric" placeholder="repeat" autocomplete="off">
    <div class="eff" id="pinerr" style="color:#ffb4b4"></div>
  </div></div>

  <!-- STEP 7: review -->
  <div class="step" data-step="7"><div class="card">
    <div class="h2">Review &amp; finish</div>
    <p class="lead">Last look — you can change any of this later from the panel.</p>
    <div id="review"></div>
  </div></div>
</div>

<div class="navbar">
  <button class="btn ghost" id="back" onclick="go(-1)">← Back</button>
  <button class="btn primary" id="next" onclick="go(1)">Next →</button>
</div>
<div class="toast" id="toast"></div>

<script>
const PRE = /*__PREFILL__*/;
const TITLES = ["Welcome","Budget","Folders","Vault","Devices","Alerts","PIN","Review"];
let step = 0;
let AUTHPIN = "";
let S = {
  mode: PRE.mode || "full-auto",
  five: +PRE.five_hour_target_pct || 80,
  reserve: +PRE.weekly_reserve_pct || 20,
  wall: +PRE.max_wallclock_min || 360,
  work: (PRE.work_roots||[]).slice(),
  off: (PRE.off_limits||[]).slice(),
  vault: PRE.vault_path || "",
  backupKind: ((PRE.devices||{}).backup||{}).kind || "none",
  backupHost: ((PRE.devices||{}).backup||{}).ssh_host || "",
  backupDest: ((PRE.devices||{}).backup||{}).dest_path || "",
  backupMount: ((PRE.devices||{}).backup||{}).kind==="mount" ? ((PRE.devices||{}).backup||{}).dest_path||"" : "",
  audit: (((PRE.devices||{}).audit)||[]).slice(),
  notify: Object.assign({windows_toast:false,ntfy:false,vault_log:false}, PRE.notify||{}),
  pin: ""
};

function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';
  clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',3200);}

function renderDots(){const d=document.getElementById('dots');d.innerHTML='';
  TITLES.forEach((_,i)=>{const e=document.createElement('div');
    e.className='dot'+(i===step?' on':(i<step?' done':''));d.appendChild(e);});}
function showStep(){document.querySelectorAll('.step').forEach(s=>s.classList.toggle('active',+s.dataset.step===step));
  document.getElementById('subt').textContent=(PRE.setup_complete?'Reconfigure — ':'First-run setup — ')+TITLES[step];
  document.getElementById('back').style.visibility=step===0?'hidden':'visible';
  const n=document.getElementById('next');n.textContent=step===7?'✓ Finish setup':'Next →';
  renderDots();if(step===7)renderReview();window.scrollTo(0,0);}

function go(dir){
  if(dir>0 && !validateStep())return;
  if(step===7 && dir>0){return finish();}
  step=Math.max(0,Math.min(7,step+dir));showStep();
}
function validateStep(){
  if(step===6){
    const a=document.getElementById('pin1').value.trim(), b=document.getElementById('pin2').value.trim();
    const err=document.getElementById('pinerr');
    if(!/^\d{4,8}$/.test(a)){err.textContent='PIN must be 4–8 digits.';return false;}
    if(a!==b){err.textContent='PINs do not match.';return false;}
    err.textContent='';S.pin=a;
  }
  if(step===2 && S.work.length===0){toast('Pick at least one folder to work in.');return false;}
  return true;
}

/* ---- step 1 ---- */
function setMode(m){S.mode=m;document.getElementById('opt-auto').classList.toggle('sel',m==='full-auto');
  document.getElementById('opt-obs').classList.toggle('sel',m==='observe');
  document.getElementById('m-auto').checked=m==='full-auto';document.getElementById('m-obs').checked=m==='observe';}
function bindBudget(){
  const f=document.getElementById('five'),r=document.getElementById('res'),w=document.getElementById('wall');
  f.value=S.five;r.value=S.reserve;w.value=S.wall;
  const upd=()=>{S.five=+f.value;S.reserve=+r.value;
    document.getElementById('fivev').textContent=S.five+'%';
    document.getElementById('resv').textContent=S.reserve+'%';
    document.getElementById('fivee').textContent='Moonlighter keeps working until your 5-hour window is '+S.five+'% used, then stops.';
    document.getElementById('rese').textContent='It will never spend past '+(100-S.reserve)+'% of your weekly limit — '+S.reserve+'% stays yours.';};
  f.oninput=upd;r.oninput=upd;w.onchange=()=>S.wall=+w.value;upd();
}

/* ---- chips (folders / vault) ---- */
function renderChips(){
  document.getElementById('workchips').innerHTML=S.work.map((p,i)=>
    '<span class="chip work">'+esc(p)+'<b onclick="rm(\'work\','+i+')">×</b></span>').join('')||'<span class="small">none yet</span>';
  document.getElementById('offchips').innerHTML=S.off.map((p,i)=>
    '<span class="chip off">'+esc(p)+'<b onclick="rm(\'off\','+i+')">×</b></span>').join('')||'<span class="small">none</span>';
  document.getElementById('vaultchips').innerHTML=S.vault?
    '<span class="chip work">'+esc(S.vault)+'<b onclick="clearVault()">×</b></span>':'<span class="small">no vault</span>';
}
function rm(kind,i){S[kind].splice(i,1);renderChips();}
function clearVault(){S.vault='';renderChips();closeBrowser();}

/* ---- folder browser ---- */
let BR={mode:null,path:PRE.home,el:null};
function browserEl(){return document.getElementById(BR.mode==='vault'?'browser3':'browser');}
function openBrowser(mode){
  BR.mode=mode;BR.path=(mode==='vault'&&S.vault)?S.vault:((mode==='work'&&S.work[0])?S.work[0]:PRE.home);
  document.getElementById('browser').classList.remove('open');
  document.getElementById('browser3').classList.remove('open');
  browserEl().classList.add('open');loadDir(BR.path);
}
function closeBrowser(){document.getElementById('browser').classList.remove('open');
  const b3=document.getElementById('browser3');if(b3)b3.classList.remove('open');}
async function loadDir(path){
  const el=browserEl();el.innerHTML='<div class="bcrumb">loading…</div>';
  let url='/api/fs?path='+encodeURIComponent(path);if(AUTHPIN)url+='&pin='+encodeURIComponent(AUTHPIN);
  let d;try{const r=await fetch(url);d=await r.json();}catch(e){el.innerHTML='<div class="bcrumb">error</div>';return;}
  if(d.error&&!d.dirs){/*permission*/}
  BR.path=d.path;
  let rows=d.dirs.map(x=>'<div class="brow"><span class="nav" onclick="loadDir(\''+encB(x.path)+'\')"><span class="ic">▸</span>'+esc(x.name)+'</span>'+rowActions(x.path)+'</div>').join('');
  if(!rows)rows='<div class="brow"><span class="small" style="padding:4px">(no sub-folders)</span></div>';
  el.innerHTML=
    '<div class="bcrumb">📂 '+esc(d.path)+'</div>'+
    '<div class="blist">'+rows+'</div>'+
    '<div class="bbar">'+
      (d.parent?'<button class="mini" onclick="loadDir(\''+encB(d.parent)+'\')">↑ up</button>':'')+
      '<button class="mini" onclick="loadDir(\''+encB(PRE.home)+'\')">⌂ home</button>'+
      thisFolderBtn(d.path)+
      '<span style="flex:1"></span>'+
      '<button class="mini" onclick="closeBrowser()">close</button>'+
    '</div>';
}
function encB(s){return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'");}
function rowActions(p){
  if(BR.mode==='work')return '<button class="mini" onclick="pick(\'work\',\''+encB(p)+'\')">+ work</button>';
  if(BR.mode==='off')return '<button class="mini" onclick="pick(\'off\',\''+encB(p)+'\')">⛔ off-limits</button>';
  if(BR.mode==='vault')return '<button class="mini" onclick="pick(\'vault\',\''+encB(p)+'\')">use this</button>';
  return '';
}
function thisFolderBtn(p){
  if(BR.mode==='work')return '<button class="mini" onclick="pick(\'work\',\''+encB(p)+'\')">+ use THIS folder</button>';
  if(BR.mode==='off')return '<button class="mini" onclick="pick(\'off\',\''+encB(p)+'\')">⛔ this folder</button>';
  if(BR.mode==='vault')return '<button class="mini" onclick="pick(\'vault\',\''+encB(p)+'\')">use THIS folder</button>';
  return '';
}
function pick(kind,p){
  if(kind==='vault'){S.vault=p;closeBrowser();renderChips();toast('Vault set.');return;}
  if(!S[kind].includes(p))S[kind].push(p);
  renderChips();toast((kind==='work'?'Work folder':'Off-limits')+' added.');
}

/* ---- step 4 devices ---- */
function setBackup(k){S.backupKind=k;
  ['none','ssh','mount'].forEach(x=>document.getElementById('bk-'+x).classList.toggle('sel',x===k));
  document.querySelectorAll('input[name=bk]')[['none','ssh','mount'].indexOf(k)].checked=true;
  document.getElementById('bk-ssh-fields').style.display=k==='ssh'?'block':'none';
  document.getElementById('bk-mount-fields').style.display=k==='mount'?'block':'none';
}
function bindBackup(){
  setBackup(S.backupKind);
  document.getElementById('bkhost').value=S.backupHost;
  document.getElementById('bkdest').value=S.backupDest||'backups/git-bundles';
  document.getElementById('bkmount').value=S.backupMount;
  document.getElementById('bkhost').oninput=e=>S.backupHost=e.target.value.trim();
  document.getElementById('bkdest').oninput=e=>S.backupDest=e.target.value.trim();
  document.getElementById('bkmount').oninput=e=>S.backupMount=e.target.value.trim();
}
function renderAudit(){
  const c=document.getElementById('auditrows');c.innerHTML='';
  S.audit.forEach((h,i)=>{const row=document.createElement('div');row.className='audit-row';
    row.innerHTML='<input type="text" placeholder="name" value="'+esc(h.name||'')+'" oninput="S.audit['+i+'].name=this.value">'+
      '<input type="text" placeholder="ssh host" value="'+esc(h.ssh_host||'')+'" oninput="S.audit['+i+'].ssh_host=this.value">'+
      '<button class="mini" onclick="S.audit.splice('+i+',1);renderAudit()">×</button>';
    c.appendChild(row);});
}
function addAudit(){S.audit.push({name:'',ssh_host:''});renderAudit();}

/* ---- step 5 ---- */
function bindNotify(){
  document.getElementById('n-toast').checked=!!S.notify.windows_toast;
  document.getElementById('n-vault').checked=!!S.notify.vault_log;
  document.getElementById('n-ntfy').checked=!!S.notify.ntfy;
  document.getElementById('n-toast').onchange=e=>S.notify.windows_toast=e.target.checked;
  document.getElementById('n-vault').onchange=e=>S.notify.vault_log=e.target.checked;
  document.getElementById('n-ntfy').onchange=e=>S.notify.ntfy=e.target.checked;
}

/* ---- review ---- */
function rl(k,v){return '<div class="rev-line"><span class="k">'+k+'</span><span class="v">'+esc(v)+'</span></div>';}
function renderReview(){
  let bk=S.backupKind==='ssh'?('ssh '+S.backupHost+':'+(S.backupDest||'backups')):
         S.backupKind==='mount'?('mount '+S.backupMount):'none';
  const aud=S.audit.filter(h=>h.ssh_host).map(h=>h.name||h.ssh_host).join(', ')||'none';
  const nt=Object.keys(S.notify).filter(k=>S.notify[k]).map(k=>({windows_toast:'toast',vault_log:'vault',ntfy:'ntfy'}[k])).join(', ')||'panel only';
  document.getElementById('review').innerHTML=
    rl('Mode',S.mode)+rl('Fill 5-hour window to',S.five+'%')+rl('Keep in reserve',S.reserve+'%')+
    rl('Max run',S.wall+' min')+
    rl('Work folders',S.work.join('  •  ')||'—')+
    rl('Off-limits',S.off.join('  •  ')||'none')+
    rl('Vault',S.vault||'none')+
    rl('Backup',bk)+rl('Audit devices',aud)+rl('Alerts',nt);
}

/* ---- finish ---- */
async function finish(){
  const payload={
    pin:S.pin, auth_pin:AUTHPIN,
    mode:S.mode, five_hour_target_pct:S.five, weekly_reserve_pct:S.reserve, max_wallclock_min:S.wall,
    work_roots:S.work, off_limits:S.off, vault_path:S.vault,
    devices:{backup:
       S.backupKind==='ssh'?{kind:'ssh',ssh_host:S.backupHost,dest_path:S.backupDest||'backups/git-bundles'}:
       S.backupKind==='mount'?{kind:'mount',dest_path:S.backupMount}:{kind:'none'},
      audit:S.audit.filter(h=>h.ssh_host).map(h=>({name:h.name||h.ssh_host,ssh_host:h.ssh_host}))},
    notify:S.notify
  };
  const n=document.getElementById('next');n.disabled=true;n.textContent='Saving…';
  try{
    const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const j=await r.json();
    if(j.ok){toast('Saved! Opening Moonlighter…');setTimeout(()=>location.href='/',900);}
    else{toast(j.error==='pin'?'PIN auth failed.':('Could not save: '+(j.error||'error')));n.disabled=false;n.textContent='✓ Finish setup';}
  }catch(e){toast('Network error.');n.disabled=false;n.textContent='✓ Finish setup';}
}

/* ---- unlock (re-run only) ---- */
async function doUnlock(){
  const p=document.getElementById('authpin').value.trim();
  const r=await fetch('/api/fs?path='+encodeURIComponent(PRE.home)+'&pin='+encodeURIComponent(p));
  if(r.status===403){document.getElementById('unlockerr').style.display='block';return;}
  AUTHPIN=p;document.getElementById('unlock').classList.remove('open');
}

/* ---- init ---- */
setMode(S.mode);bindBudget();renderChips();bindBackup();renderAudit();bindNotify();showStep();
if(PRE.setup_complete && PRE.pin_set){document.getElementById('unlock').classList.add('open');}
</script>
</body></html>"""


def _validate_setup(body: dict) -> dict:
    """Validate + normalise the wizard payload into the config overlay to persist.
    Raises ValueError(human message) on bad input. Only known keys pass through."""
    def _int(v, lo, hi, name):
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number")
        if not (lo <= n <= hi):
            raise ValueError(f"{name} must be between {lo} and {hi}")
        return n

    pin = str(body.get("pin", "")).strip()
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        raise ValueError("PIN must be 4–8 digits")

    mode = body.get("mode", "full-auto")
    if mode not in ("full-auto", "observe"):
        raise ValueError("mode must be full-auto or observe")

    roots_in = body.get("work_roots") or []
    work_roots = []
    for r in roots_in:
        r = str(r).strip()
        if not r:
            continue
        p = pathlib.Path(os.path.expanduser(r))
        if not p.is_dir():
            raise ValueError(f"work folder does not exist: {r}")
        work_roots.append(str(p))
    if not work_roots:
        raise ValueError("pick at least one folder for Moonlighter to work in")

    off_limits = [str(p).strip() for p in (body.get("off_limits") or []) if str(p).strip()]

    vault = str(body.get("vault_path", "") or "").strip()
    if vault and not pathlib.Path(os.path.expanduser(vault)).is_dir():
        raise ValueError(f"vault path does not exist: {vault}")

    dev_in = body.get("devices") or {}
    bk = dev_in.get("backup") or {"kind": "none"}
    kind = bk.get("kind", "none")
    if kind == "ssh":
        host = str(bk.get("ssh_host", "")).strip()
        if not host:
            raise ValueError("backup over SSH needs a host")
        backup = {"kind": "ssh", "ssh_host": host,
                  "dest_path": str(bk.get("dest_path", "backups/git-bundles")).strip() or "backups/git-bundles"}
    elif kind == "mount":
        dest = str(bk.get("dest_path", "")).strip()
        if not dest:
            raise ValueError("backup to a mounted drive needs a destination path")
        backup = {"kind": "mount", "dest_path": dest}
    else:
        backup = {"kind": "none"}

    audit = []
    for h in (dev_in.get("audit") or []):
        host = str(h.get("ssh_host", "")).strip()
        if not host:
            continue
        audit.append({"name": str(h.get("name", "") or host).strip(), "ssh_host": host})

    nin = body.get("notify") or {}
    notify = {"windows_toast": bool(nin.get("windows_toast")),
              "ntfy": bool(nin.get("ntfy")),
              "vault_log": bool(nin.get("vault_log"))}

    return {
        "setup_complete": True,
        "pin": pin,
        "mode": mode,
        "five_hour_target_pct": _int(body.get("five_hour_target_pct", 80), 10, 100, "5-hour target"),
        "weekly_reserve_pct": _int(body.get("weekly_reserve_pct", 20), 0, 90, "weekly reserve"),
        "max_wallclock_min": _int(body.get("max_wallclock_min", 360), 30, 720, "max run length"),
        "work_roots": work_roots,
        "off_limits": off_limits,
        "vault_path": vault,
        "devices": {"backup": backup, "audit": audit},
        "notify": notify,
    }


def _build_setup_html(cfg: dict) -> str:
    """The first-run setup wizard (also reachable later to reconfigure)."""
    prefill = {
        "setup_complete": bool(cfg.get("setup_complete")),
        "mode": cfg.get("mode", "full-auto"),
        "five_hour_target_pct": cfg.get("five_hour_target_pct", 80),
        "weekly_reserve_pct": cfg.get("weekly_reserve_pct", 20),
        "max_wallclock_min": cfg.get("max_wallclock_min", 360),
        "work_roots": cfg.get("work_roots_resolved") or [],
        "off_limits": [str(p) for p in (cfg.get("off_limits") or [])],
        "vault_path": cfg.get("vault_path_resolved") or "",
        "devices": cfg.get("devices") or {"backup": {"kind": "none"}, "audit": []},
        "notify": {k: bool((cfg.get("notify") or {}).get(k))
                   for k in ("windows_toast", "ntfy", "vault_log")},
        "pin_set": bool(cfg.get("pin")),
        "home": str(pathlib.Path.home()),
    }
    return _SETUP_TEMPLATE.replace("/*__PREFILL__*/", json.dumps(prefill))


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class PanelHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress default noisy logging; print minimal info
        print(f"  {self.address_string()} {fmt % args}")

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, default=str).encode("utf-8")
        self._send(code, "application/json", body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _check_pin(self, body: dict) -> bool:
        return str(body.get("pin", "")) == str(CFG.get("pin", ""))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            # First-run: send a fresh user straight to the setup wizard.
            if not CFG.get("setup_complete"):
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.end_headers()
                return
            self._handle_root()
        elif path == "/setup":
            self._handle_setup_page()
        elif path == "/api/fs":
            self._handle_fs(urllib.parse.parse_qs(parsed.query))
        elif path == "/api/status":
            self._handle_status()
        elif path == "/api/run-activity":
            self._handle_run_activity()
        elif path == "/fonts.css":
            self._handle_font_css()
        elif path.startswith("/fonts/"):
            self._handle_font_file(path[len("/fonts/"):])
        elif path == "/night":
            self._handle_night()
        elif path.startswith("/run/"):
            run_id = path[5:].strip("/")
            self._handle_run(run_id)
        else:
            self._send(404, "text/plain", b"404 Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        routes = {
            "/api/start": self._handle_start,
            "/api/pause": self._handle_pause,
            "/api/resume": self._handle_resume,
            "/api/approve": self._handle_approve,
            "/api/mode": self._handle_mode,
            "/api/settings": self._handle_settings,
            "/api/apply": self._handle_apply,
            "/api/answer": self._handle_answer,
            "/api/revert": self._handle_revert,
            "/api/setup": self._handle_setup_save,
        }
        handler = routes.get(path)
        if handler:
            handler(body)
        else:
            self._send(404, "text/plain", b"404 Not Found")

    # ---- GET handlers ----

    def _handle_root(self):
        try:
            status = gatemod.compute_status(CFG)
        except Exception as exc:
            status = {"live": False, "error": str(exc)}
        try:
            html = _build_panel_html(status, CFG)
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        except Exception as exc:
            tb = traceback.format_exc()
            self._send(500, "text/plain", f"Panel build error:\n{tb}".encode("utf-8"))

    def _handle_status(self):
        try:
            status = gatemod.compute_status(CFG)
            body = json.dumps(status, default=str).encode("utf-8")
            self._send(200, "application/json", body)
        except Exception as exc:
            self._send_json(200, {"live": False, "error": str(exc)})

    def _handle_run_activity(self):
        # Lightweight: only the active-run state (NO usage API call), for fast polling.
        try:
            ar = gatemod.get_active_run()
            self._send_json(200, ar if ar else {"active": False})
        except Exception as exc:
            self._send_json(200, {"active": False, "error": str(exc)})

    def _handle_fs(self, qs):
        """Folder browser for the setup wizard: list immediate SUBDIRECTORIES of a path.
        Dirs only (never file contents). Available pre-setup (no PIN yet) or with a valid PIN
        afterwards. Read-only; reveals only directory names on the user's own machine."""
        if CFG.get("setup_complete") and str(qs.get("pin", [""])[0]) != str(CFG.get("pin", "")):
            self._send_json(403, {"error": "pin"})
            return
        home = str(pathlib.Path.home())
        raw = (qs.get("path", [home])[0] or home)
        try:
            base = pathlib.Path(os.path.expanduser(raw)).resolve(strict=False)
        except Exception:
            base = pathlib.Path(home)
        show_hidden = qs.get("hidden", ["0"])[0] == "1"
        dirs = []
        try:
            for e in sorted(os.scandir(base), key=lambda x: x.name.lower()):
                if not show_hidden and e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append({"name": e.name, "path": str(base / e.name)})
                except OSError:
                    continue
        except (PermissionError, FileNotFoundError, NotADirectoryError) as exc:
            self._send_json(200, {"path": str(base), "parent": str(base.parent),
                                  "dirs": [], "error": str(exc)})
            return
        self._send_json(200, {"path": str(base),
                              "parent": str(base.parent) if base != base.parent else "",
                              "home": home, "dirs": dirs})

    def _handle_setup_page(self):
        try:
            html = _build_setup_html(CFG)
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        except Exception:
            tb = traceback.format_exc()
            self._send(500, "text/plain", f"Setup build error:\n{tb}".encode("utf-8"))

    def _handle_setup_save(self, body: dict):
        """Persist the wizard's choices to the overlay and reload CFG live (no restart)."""
        global CFG
        # Once configured, re-running setup is PIN-gated (authorise with the CURRENT pin via
        # auth_pin; `pin` in the body is the NEW pin to set). First run is open (it SETS the pin).
        if CFG.get("setup_complete"):
            auth = str(body.get("auth_pin") or body.get("pin", ""))
            if auth != str(CFG.get("pin", "")):
                self._send_json(403, {"ok": False, "error": "pin"})
                return
        try:
            updates = _validate_setup(body)
        except ValueError as exc:
            self._send_json(200, {"ok": False, "error": str(exc)})
            return
        cfgmod.save_local(updates)
        CFG = cfgmod.load()
        self._send_json(200, {"ok": True, "message": "Setup saved — Moonlighter is configured."})

    def _handle_font_css(self):
        f = HERE / "fonts" / "fonts.css"
        if f.exists():
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css")
            self.send_header("Cache-Control", "max-age=31536000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, "text/plain", b"no fonts.css")

    def _handle_font_file(self, name: str):
        safe = pathlib.Path(name).name  # no traversal
        f = HERE / "fonts" / safe
        if f.exists() and f.suffix == ".woff2":
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "font/woff2")
            self.send_header("Cache-Control", "max-age=31536000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, "text/plain", b"no such font")

    def _handle_night(self):
        try:
            html = _build_night_html(CFG)
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        except Exception:
            self._send(500, "text/plain", f"Night digest error:\n{traceback.format_exc()}".encode())

    def _handle_run(self, run_id: str):
        # Sanitise run_id to prevent path traversal
        safe_id = pathlib.Path(run_id).name
        try:
            html = _build_run_html(safe_id, CFG)
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        except Exception as exc:
            tb = traceback.format_exc()
            self._send(500, "text/plain", f"Run page error:\n{tb}".encode("utf-8"))

    # ---- POST handlers ----

    def _handle_start(self, body: dict):
        if not self._check_pin(body):
            self._send_json(403, {"ok": False, "error": "bad pin"})
            return

        # Guard rails (mirroring cli.py cmd_start)
        if CFG["kill_switch_path"].exists():
            self._send_json(400, {"ok": False, "error": "paused — resume first"})
            return

        tmux_running = subprocess.run(
            ["tmux", "has-session", "-t", TMUX],
            stdout=DEVNULL, stderr=DEVNULL
        ).returncode == 0
        if tmux_running:
            self._send_json(400, {"ok": False, "error": "run already in flight"})
            return

        hours = float(body.get("hours", 5) or 5)
        try:
            s = gatemod.compute_status(CFG, manual_away_hours=hours)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"status error: {exc}"})
            return

        bud = s.get("gate", {}).get("budget")
        if bud is None:
            self._send_json(400, {"ok": False, "error": "cannot reach usage API"})
            return

        # No 5h-ceiling pre-check on a MANUAL start — the user said they're away. The
        # runner reads the 5h target + weekly cap from config and stops there.
        if not bud["ok"]:
            self._send_json(400, {"ok": False, "error":
                f"nothing to do — 5h {bud['five_now']:.0f}% (target {bud['five_target']:.0f}%), "
                f"weekly {bud['weekly_now']:.0f}% (cap {bud['weekly_cap']:.0f}%)"})
            return

        env = dict(os.environ)
        env["ML_ACTIVE_BUCKET"] = bud["active_bucket"]
        env["ML_AWAY_HOURS"] = str(hours)

        run_sh = PROJECT / "run.sh"
        subprocess.Popen(
            ["bash", str(run_sh)], env=env,
            stdout=DEVNULL, stderr=DEVNULL,
            start_new_session=True
        )
        self._send_json(200, {"ok": True})

    def _handle_pause(self, body: dict):
        # No PIN check — pausing is deliberately frictionless (see design doc "Accepted
        # risk"): off is the fail-safe direction (no spend, no filesystem mutation), so
        # it stays open even though this endpoint is shared with the ntfy bridge. Only
        # resume (which spends quota) stays PIN-gated below.
        try:
            p = CFG["kill_switch_path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(state.now_iso(), encoding="utf-8")
            self._send_json(200, {"ok": True})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _handle_resume(self, body: dict):
        if not self._check_pin(body):
            self._send_json(403, {"ok": False, "error": "bad pin"})
            return
        try:
            p = CFG["kill_switch_path"]
            if p.exists():
                p.unlink()
            self._send_json(200, {"ok": True})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _handle_settings(self, body: dict):
        if not self._check_pin(body):
            self._send_json(403, {"ok": False, "error": "bad pin"})
            return
        try:
            five_leave = float(body.get("five_leave"))      # leave this % of the 5h window free
            weekly_reserve = float(body.get("weekly_reserve"))  # always leave this % weekly
            five_target = max(5.0, min(100.0, 100.0 - five_leave))
            weekly_reserve = max(0.0, min(90.0, weekly_reserve))
            cfgpath = PROJECT / "config.yaml"
            txt = cfgpath.read_text(encoding="utf-8")
            txt = re.sub(r"^five_hour_target_pct:.*", f"five_hour_target_pct: {five_target:.0f}", txt, count=1, flags=re.M)
            txt = re.sub(r"^weekly_reserve_pct:.*", f"weekly_reserve_pct: {weekly_reserve:.0f}", txt, count=1, flags=re.M)
            cfgpath.write_text(txt, encoding="utf-8")
            CFG["five_hour_target_pct"] = five_target
            CFG["weekly_reserve_pct"] = weekly_reserve
            self._send_json(200, {"ok": True, "five_target": five_target, "weekly_reserve": weekly_reserve})
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "numbers required"})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _handle_mode(self, body: dict):
        if not self._check_pin(body):
            self._send_json(403, {"ok": False, "error": "bad pin"})
            return
        want = body.get("mode")
        want = "observe" if want in ("review", "observe") else "full-auto"
        try:
            cfgpath = PROJECT / "config.yaml"
            txt = cfgpath.read_text(encoding="utf-8")
            txt = re.sub(r"^mode:\s*\S+", f"mode: {want}", txt, count=1, flags=re.M)
            cfgpath.write_text(txt, encoding="utf-8")
            if want == "full-auto":
                state.APPROVED_FLAG.write_text(state.now_iso(), encoding="utf-8")
            CFG["mode"] = want  # reflect immediately for this process
            self._send_json(200, {"ok": True, "mode": want})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _handle_approve(self, body: dict):
        if not self._check_pin(body):
            self._send_json(403, {"ok": False, "error": "bad pin"})
            return
        runs = state.list_runs(50)
        has_dry = any(r.get("dry_run") for r in runs)
        if not has_dry:
            self._send_json(400, {
                "ok": False,
                "error": "no dry run completed yet — let an observe-mode night run first"
            })
            return
        try:
            cfgpath = PROJECT / "config.yaml"
            txt = cfgpath.read_text(encoding="utf-8")
            txt = txt.replace("mode: observe", "mode: full-auto", 1)
            cfgpath.write_text(txt, encoding="utf-8")
            state.APPROVED_FLAG.write_text(state.now_iso(), encoding="utf-8")
            self._send_json(200, {"ok": True})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _handle_apply(self, body: dict):
        if not self._check_pin(body):
            self._send_json(403, {"ok": False, "error": "bad pin"})
            return
        msgs = []
        # --- reverts (per-item, grouped by run) ---
        byrun = {}
        for r in body.get("revert", []):
            run = pathlib.Path(str(r.get("run", ""))).name
            byrun.setdefault(run, []).append(int(r.get("idx", -1)))
        for run, idxs in byrun.items():
            res = revertmod.revert_items(run, idxs)
            m = f"{run[-6:]}: {len(res['reverted'])} reverted"
            if res["errors"]:
                m += f", {len(res['errors'])} failed"
            msgs.append(m)
        # --- items to DO (proposals + findings) → spawn a focused agent run ---
        do_items = body.get("do", [])
        agent_started = False
        if do_items:
            if subprocess.run(["tmux", "has-session", "-t", TMUX],
                              stdout=DEVNULL, stderr=DEVNULL).returncode == 0:
                msgs.append("a run is already in flight — try again once it finishes")
            else:
                try:
                    d = digestmod.build_night()
                    tasks = []
                    for it in do_items:
                        if it.get("kind") == "proposal":
                            i = int(it.get("i", -1))
                            if 0 <= i < len(d["proposals"]):
                                p = d["proposals"][i]
                                cmds = "\n".join(p.get("commands", []))
                                tasks.append(f"{p.get('title','proposal')}\nCommands:\n{cmds}".strip())
                        else:  # a finding (security / idea / audit) — prose task
                            t = str(it.get("task", "")).strip()
                            if t:
                                tasks.append(t)
                    tasks = [t for t in tasks if t]
                    CAP = 200  # generous backstop only; apply ALL ticked items, warn if absurd
                    dropped = 0
                    if len(tasks) > CAP:
                        dropped = len(tasks) - CAP
                        tasks = tasks[:CAP]
                    if tasks and CFG["kill_switch_path"].exists():
                        # Switched off must refuse the launch up front, the same way
                        # /api/start does — not spawn an agent that acts until the
                        # supervisor's first tick notices and stops it.
                        msgs.append("Moonlighter is switched off — turn it on to run "
                                    "approved items. Your approvals were saved.")
                    elif tasks:
                        tf = state.STATE_DIR / "apply_tasks.txt"
                        tf.write_text("\n\x1e".join(tasks), encoding="utf-8")
                        env = dict(os.environ)
                        env["ML_APPLY_TASKS"] = str(tf)
                        subprocess.Popen(["bash", str(PROJECT / "run.sh")], env=env,
                                         stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)
                        agent_started = True
                        m = f"agent starting — will do {len(tasks)} approved item(s)"
                        if dropped:
                            m += (f" ⚠️ {dropped} more were not sent (cap {CAP} per run — "
                                  f"re-apply the rest after this finishes)")
                        msgs.append(m)
                except Exception as exc:
                    msgs.append(f"do-items failed: {exc}")
        self._send_json(200, {"ok": True, "agent_started": agent_started,
                              "message": "; ".join(msgs) or "nothing applied"})

    def _handle_answer(self, body: dict):
        # Answer the agent's clarifying question. No PIN: the run was already
        # PIN-authorised via Apply; this just feeds text to a waiting agent.
        run_id = pathlib.Path(str(body.get("run", ""))).name
        answer = str(body.get("answer", "")).strip()
        rd = state.RUNS_DIR / run_id
        if not rd.exists() or not answer:
            self._send_json(400, {"ok": False, "error": "missing run/answer"})
            return
        try:
            (rd / "answer.txt").write_text(answer, encoding="utf-8")
            self._send_json(200, {"ok": True})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _handle_revert(self, body: dict):
        if not self._check_pin(body):
            self._send_json(403, {"ok": False, "error": "bad pin"})
            return
        run_id = str(body.get("id", "")).strip()
        if not run_id:
            self._send_json(400, {"ok": False, "error": "missing run id"})
            return
        # Sanitise
        safe_id = pathlib.Path(run_id).name
        try:
            rc = revertmod.run_revert(safe_id)
            self._send_json(200, {"ok": rc == 0, "exit_code": rc})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Load config once at startup (module level so handler can reference it)
CFG = cfgmod.load()

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded so one slow request (e.g. a usage-API timeout) can't wedge the
    whole panel for every other client (phone auto-refresh + per-run pages)."""
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    port = CFG.get("ui_port", 8377)
    host = CFG.get("bind_host", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), PanelHandler)
    url = f"http://{host}:{port}/"
    print(f"Moonlighter panel: {url}")
    print(f"Bound to {host}:{port}. State-changing actions are PIN-gated. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
