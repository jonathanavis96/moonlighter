"""notify.py — outbound report notifications (LOCAL only: toast, ntfy push, vault).

These are the ONLY outward-ish calls Moonlighter makes, and they are explicitly
user-approved report channels — never used by the night session itself.
"""
import datetime
import os
import pathlib
import platform
import shutil
import subprocess
import urllib.request


def _is_wsl():
    try:
        return "microsoft" in pathlib.Path("/proc/version").read_text().lower()
    except Exception:
        return False


def desktop_notify(title, body):
    """Cross-platform desktop notification. Windows (incl. WSL→Windows) toast via
    PowerShell, macOS via osascript, Linux via notify-send. Best-effort: returns
    True if a mechanism fired. Never raises."""
    body = (body or "").replace('"', "'")
    title = (title or "Moonlighter").replace('"', "'")
    try:
        if os.name == "nt" or _is_wsl():
            ps = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
            # BurntToast-free toast via the WinRT notification API.
            script = (
                '[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;'
                '$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('
                '[Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
                '$x=$t.GetElementsByTagName("text");'
                f'$x.Item(0).AppendChild($t.CreateTextNode("{title}"))>$null;'
                f'$x.Item(1).AppendChild($t.CreateTextNode("{body}"))>$null;'
                '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Moonlighter")'
                '.Show([Windows.UI.Notifications.ToastNotification]::new($t));'
            )
            subprocess.run([ps, "-NoProfile", "-Command", script], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{body}" with title "{title}"'],
                           timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:
        return False
    return False


# Backwards-compatible alias (older callers / config wording).
def windows_toast(title, body):
    return desktop_notify(title, body)


def ntfy_push(cfg, message, title="Moonlighter"):
    nt = (cfg.get("ntfy") or {})
    base = nt.get("base_url", "https://ntfy.sh").rstrip("/")
    topic = nt.get("notify_topic")
    if not topic:
        return False
    try:
        req = urllib.request.Request(
            f"{base}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": "crescent_moon"},
        )
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception:
        return False


def vault_append(cfg, line):
    nc = (cfg.get("notify") or {})
    if not nc.get("vault_log"):
        return False
    path = nc.get("vault_log_path")
    if not path:
        return False
    p = pathlib.Path(os.path.expanduser(path))
    try:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                "# Moonlighter Changelog\n\n"
                "Append-only log of nightly runs and skips.\n\n",
                encoding="utf-8",
            )
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
        return True
    except Exception:
        return False


def report_ready(cfg, headline, report_path=None, spend_line=""):
    """Fire all enabled channels that a report exists. Returns which fired."""
    nc = (cfg.get("notify") or {})
    fired = []
    date = datetime.datetime.now().strftime("%a %d %b")
    body = headline + (f"\n{spend_line}" if spend_line else "")
    if nc.get("windows_toast") and desktop_notify("Moonlighter", body):
        fired.append("toast")
    if nc.get("ntfy") and ntfy_push(cfg, body):
        fired.append("ntfy")
    if nc.get("vault_log"):
        vline = f"- **{date}** — {headline}" + (f" ({spend_line})" if spend_line else "")
        if vault_append(cfg, vline):
            fired.append("vault")
    return fired
