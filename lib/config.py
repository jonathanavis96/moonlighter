"""config.py — load and resolve Moonlighter config (stdlib + pyyaml).

Two layers: the documented, comment-rich template `config.yaml` (shipped defaults), and a
machine-written overlay `~/.moonlighter/config.local.yaml` that the first-run setup wizard
owns. `load()` deep-merges the overlay OVER the template, so the wizard never has to rewrite
(and strip the comments from) the template. Anything the wizard sets — budget, work folders,
vault, devices, notifications, PIN, setup_complete — lives in the overlay.
"""
import os
import pathlib
import yaml

PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
STATE_DIR = pathlib.Path.home() / ".moonlighter"
LOCAL_PATH = STATE_DIR / "config.local.yaml"

# Paths that are ALWAYS off-limits regardless of config (secrets/credentials).
ALWAYS_OFF_LIMITS = [
    "~/.claude/.credentials.json",
    "~/.claude",            # session tokens live here
    "~/code/secrets",       # DPAPI-encrypted personal keys
    "~/.ssh",
    "~/.aws",
    "~/.config/gh",
    "~/.gnupg",
]


def _expand(p):
    return pathlib.Path(os.path.expanduser(str(p))).resolve(strict=False)


def _deep_merge(base, over):
    """Recursively merge `over` into `base` (dicts merge, everything else replaces)."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    """Return the parsed config dict (template ← overlay) with key paths pre-expanded."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if LOCAL_PATH.exists():
        try:
            local = yaml.safe_load(LOCAL_PATH.read_text(encoding="utf-8")) or {}
            cfg = _deep_merge(cfg, local)
        except Exception:
            pass  # a corrupt overlay must never break the panel
    cfg["_project_dir"] = PROJECT_DIR
    cfg["_state_dir"] = STATE_DIR
    cfg["report_dir_path"] = _expand(cfg.get("report_dir", "~/.moonlighter/reports"))
    cfg["kill_switch_path"] = _expand(cfg.get("kill_switch", "~/.moonlighter/pause"))

    # Work roots — the folders Moonlighter may ACT in (reversibly). Default to home.
    roots = [r for r in (cfg.get("work_roots") or ["~"]) if r and str(r).strip()]
    roots = roots or ["~"]
    cfg["work_roots"] = roots
    cfg["work_roots_resolved"] = [str(_expand(p)) for p in roots]

    # Optional vault / notes path for the maintenance pass ("" / null = no vault step).
    vp = (cfg.get("vault_path") or "").strip() if isinstance(cfg.get("vault_path"), str) else ""
    cfg["vault_path"] = vp
    cfg["vault_path_resolved"] = str(_expand(vp)) if vp else ""

    # Devices: backup target + read-only audit hosts. Absent/empty = those steps are skipped.
    dev = cfg.get("devices") or {}
    cfg["devices"] = {
        "backup": dev.get("backup") or {"kind": "none"},
        "audit": dev.get("audit") or [],
    }

    cfg["setup_complete"] = bool(cfg.get("setup_complete"))

    # Resolve off-limits (config + always) to absolute path strings.
    off = list(cfg.get("off_limits") or [])
    off += ALWAYS_OFF_LIMITS
    cfg["off_limits_resolved"] = [str(_expand(p)) for p in off]
    return cfg


def save_local(updates):
    """Deep-merge `updates` into the overlay file and write it. Returns the new overlay dict."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    current = {}
    if LOCAL_PATH.exists():
        try:
            current = yaml.safe_load(LOCAL_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            current = {}
    merged = _deep_merge(current, updates)
    tmp = LOCAL_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    os.replace(tmp, LOCAL_PATH)
    return merged


def is_off_limits(path, cfg=None):
    """True if `path` is inside any off-limits root."""
    cfg = cfg or load()
    target = str(_expand(path))
    for root in cfg["off_limits_resolved"]:
        if target == root or target.startswith(root + os.sep):
            return True
    return False


if __name__ == "__main__":
    import json
    c = load()
    print(json.dumps({k: str(v) for k, v in c.items() if not k.startswith("_")},
                     indent=2, default=str))
