"""
usage_api.py — stdlib-only module for reading Moonlighter's gate data source.

Reads the Claude OAuth token fresh on every call from:
    ~/.claude/.credentials.json  (key: claudeAiOauth.accessToken)

Calls:
    GET https://api.anthropic.com/api/oauth/usage
    Headers:
        Authorization: Bearer <token>
        anthropic-beta: oauth-2025-04-20

Returns a dict. Sample response shape:
{
  "five_hour":      {"utilization": 61.0, "resets_at": "2026-06-12T15:29:59.437834+00:00"},
  "seven_day":      {"utilization":  9.0, "resets_at": "2026-06-19T03:59:59.437856+00:00"},
  "seven_day_sonnet":{"utilization": 2.0, "resets_at": "2026-06-19T04:00:00.437863+00:00"},
  "extra_usage": {
    "is_enabled": false,
    "monthly_limit": null,
    "used_credits": null,
    "utilization": null,
    "currency": null,
    "disabled_reason": null
  }
}
utilization values are percentages (0–100).
resets_at is an ISO-8601 UTC timestamp string.
"""

import json
import pathlib
import time
import urllib.request
import urllib.error

CREDENTIALS_PATH = pathlib.Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT = 15

# Cache so the panel (auto-refresh) and gate don't hammer the API (→ HTTP 429).
CACHE_TTL = 45          # serve in-memory result without refetching, seconds
STALE_GRACE = 5400      # on fetch failure (e.g. 429), serve last-good up to this old (90 min)
                        # — usage % moves slowly, so a slightly-stale number beats a blank panel.
_LAST_GOOD = pathlib.Path.home() / ".moonlighter" / "usage_last_good.json"
_mem = {"ts": 0.0, "data": None}


def _read_token() -> str:
    """Read the OAuth token fresh from disk on every call."""
    data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    token = data.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        raise ValueError(
            f"Could not find claudeAiOauth.accessToken in {CREDENTIALS_PATH}"
        )
    return token


def _fetch() -> dict:
    token = _read_token()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError(
                "Usage API returned HTTP 401: token is stale. "
                "Open Claude Code once (any prompt) to refresh the token, then retry."
            ) from exc
        raise


def _save_last_good(data):
    try:
        _LAST_GOOD.parent.mkdir(parents=True, exist_ok=True)
        _LAST_GOOD.write_text(json.dumps({"ts": time.time(), "data": data}))
    except Exception:
        pass


def _load_last_good():
    try:
        obj = json.loads(_LAST_GOOD.read_text())
        return obj.get("ts", 0), obj.get("data")
    except Exception:
        return 0, None


def get_usage(force=False) -> dict:
    """
    Current usage from the Anthropic OAuth usage API, with caching.

    - Serves an in-memory result younger than CACHE_TTL without refetching.
    - On a fetch failure (e.g. HTTP 429 / transient network), serves the last-good
      value up to STALE_GRACE old rather than flapping to "no live data".
    - Raises only when there is no usable cached value at all.
    Raises RuntimeError on HTTP 401 (token stale — user must open Claude Code once).
    """
    now = time.time()
    if not force and _mem["data"] is not None and (now - _mem["ts"]) < CACHE_TTL:
        return _mem["data"]
    try:
        data = _fetch()
        _mem["ts"] = now
        _mem["data"] = data
        _save_last_good(data)
        return data
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise
        # 429 / 5xx — fall back to last-good if recent enough
        if _mem["data"] is not None and (now - _mem["ts"]) < STALE_GRACE:
            return _mem["data"]
        ts, data = _load_last_good()
        if data is not None and (now - ts) < STALE_GRACE:
            _mem["ts"], _mem["data"] = ts, data
            return data
        raise
    except Exception:
        if _mem["data"] is not None and (now - _mem["ts"]) < STALE_GRACE:
            return _mem["data"]
        ts, data = _load_last_good()
        if data is not None and (now - ts) < STALE_GRACE:
            _mem["ts"], _mem["data"] = ts, data
            return data
        raise