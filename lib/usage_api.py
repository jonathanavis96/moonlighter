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

import datetime
import json
import pathlib
import time
import urllib.request
import urllib.error

CREDENTIALS_PATH = pathlib.Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT = 15

# Cache so the panel (auto-refresh) and gate don't hammer the API (→ HTTP 429).
CACHE_TTL = 45          # serve cached result without refetching, seconds
STALE_GRACE = 5400      # on fetch failure (e.g. 429), serve last-good up to this old (90 min)
                        # — usage % moves slowly, so a slightly-stale number beats a blank panel.
FORCE_MIN_INTERVAL = 60 # even force=True will not refetch more often than this. force means
                        # "ignore the TTL", not "hammer the API": a poll loop that 429s itself
                        # gets NO data, which is worse than a 60s-old reading.
_LAST_GOOD = pathlib.Path.home() / ".moonlighter" / "usage_last_good.json"
_mem = {"ts": 0.0, "data": None}

# Staleness of the value most recently returned by get_usage(), for callers that must not
# present a cached number as if it were live. See last_serve_info().
_serve = {"fetched_at": 0.0, "stale": True}

# Backoff state, shared across processes via a file next to the cache. This throttles on the
# last fetch ATTEMPT, not on the age of the last good value: once the cached value is older
# than the floor, an age-based check lets every caller retry — which is precisely the 429
# state we are trying to escape. A 429 also tells us when to come back; honour Retry-After.
_ATTEMPT_FILE = pathlib.Path.home() / ".moonlighter" / "usage_backoff.json"
MIN_ATTEMPT_INTERVAL = 30   # never attempt the API more than once per 30s across all processes


def _last_attempt():
    try:
        o = json.loads(_ATTEMPT_FILE.read_text())
        return float(o.get("ts", 0)), float(o.get("retry_after", 0))
    except Exception:
        return 0.0, 0.0


def _record_attempt(retry_after=0.0):
    try:
        _ATTEMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ATTEMPT_FILE.write_text(json.dumps({"ts": time.time(), "retry_after": retry_after}))
    except Exception:
        pass


def _may_attempt(now, window_reset=False):
    """True if enough time has passed since the last attempt to try the API again.

    Normally honours whichever is longer: the anti-stampede floor or a server-set
    Retry-After. But `window_reset=True` means the cached usage window has already
    rolled over, so the cached utilization is known-stale — a long Retry-After must
    not outlive the very window it was throttling. In that case only the short floor
    applies, so one throttled re-fetch can pick up the fresh (reset) reading.
    """
    ts, retry_after = _last_attempt()
    if not ts:
        return True
    floor = MIN_ATTEMPT_INTERVAL if window_reset else max(MIN_ATTEMPT_INTERVAL, retry_after)
    return (now - ts) >= floor


def _five_hour_reset_passed(data, now):
    """True if the cached five-hour window's reset time is at or before `now`.

    A rolled window means the cached utilization no longer describes the current
    window and must not be served across the boundary. Any missing/unparseable
    timestamp returns False — an unreadable reset is never a licence to break the
    backoff and re-open a 429 storm.
    """
    try:
        stamp = (data or {}).get("five_hour", {}).get("resets_at")
        if not stamp:
            return False
        return datetime.datetime.fromisoformat(stamp).timestamp() <= now
    except Exception:
        return False


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


def last_known():
    """
    (ts, data) of the last good reading on disk, of ANY age — for DISPLAY only.

    get_usage() deliberately goes to None once the reading is older than STALE_GRACE so a
    stale number can never drive a gate launch. The panel, though, should keep showing the
    last reading (dated) rather than going blank when the API is down for a long spell.
    Returns (0.0, None) when there is no cached reading at all. NEVER use this for a decision.
    """
    return _load_last_good()


def last_serve_info() -> dict:
    """
    Staleness of the value most recently returned by get_usage(), so callers (the panel)
    can show "as of HH:MM" instead of presenting a cached number as a live one.

    Returns {"fetched_at": epoch, "age": seconds, "stale": bool}. `stale` means the value
    did NOT come from a fresh fetch on that call — it is older than CACHE_TTL.
    """
    age = time.time() - _serve["fetched_at"] if _serve["fetched_at"] else float("inf")
    return {"fetched_at": _serve["fetched_at"], "age": age, "stale": _serve["stale"] or age > CACHE_TTL}


def _serve_cached(ts, data, now):
    """Adopt a cached value into memory and mark how stale it is."""
    _mem["ts"], _mem["data"] = ts, data
    _serve["fetched_at"] = ts
    _serve["stale"] = (now - ts) >= CACHE_TTL
    return data


def get_usage(force=False) -> dict:
    """
    Current usage from the Anthropic OAuth usage API, with caching.

    - Serves a cached result younger than CACHE_TTL without refetching. The cache is
      CROSS-PROCESS (via the last-good file): the gate, the CLI and each one-shot
      `python3 -c` all start with a cold in-memory cache, so without this every
      invocation was a real API call and they stampeded each other into HTTP 429.
    - force=True ignores the TTL but still honours FORCE_MIN_INTERVAL — force must not
      mean "hammer the API", or a tight poll loop 429s itself into having no data at all.
    - On a fetch failure (e.g. HTTP 429 / transient network), serves the last-good
      value up to STALE_GRACE old rather than flapping to "no live data".
    - Raises only when there is no usable cached value at all.
    Raises RuntimeError on HTTP 401 (token stale — user must open Claude Code once).

    Callers that display the value MUST consult last_serve_info() — a stale value is
    returned in the same shape as a fresh one and is otherwise indistinguishable.
    """
    now = time.time()
    floor = FORCE_MIN_INTERVAL if force else CACHE_TTL
    if _mem["data"] is not None and (now - _mem["ts"]) < floor:
        _serve["fetched_at"] = _mem["ts"]
        _serve["stale"] = (now - _mem["ts"]) >= CACHE_TTL
        return _mem["data"]
    # Cold in-memory cache (new process): reuse a recent on-disk value before hitting the API.
    ts, data = _load_last_good()
    if data is not None and (now - ts) < floor:
        return _serve_cached(ts, data, now)
    # Backoff gate: if we attempted too recently (or the server set Retry-After), do NOT
    # attempt again — serve the stale value instead. Without this, a stale cache means every
    # caller retries on every call, which is how the 429 storm sustains itself.
    # Exception: once the cached five-hour window has reset, the cached utilization is
    # definitively wrong, so a long Retry-After must not keep serving a pre-reset reading
    # across the boundary (which would wrongly hold/miss a scheduled run for up to an hour).
    window_reset = _five_hour_reset_passed(data, now) or _five_hour_reset_passed(_mem["data"], now)
    if not _may_attempt(now, window_reset=window_reset):
        if data is not None and (now - ts) < STALE_GRACE:
            return _serve_cached(ts, data, now)
        if _mem["data"] is not None and (now - _mem["ts"]) < STALE_GRACE:
            return _serve_cached(_mem["ts"], _mem["data"], now)
        # No cached value to serve does NOT license an attempt: falling
        # through here would mean every caller retries during the backoff /
        # Retry-After window (exactly the no-cache-yet case of the 429
        # storm, e.g. a fresh install whose first request got 429). Honour
        # the window; callers already handle "no usable value" per the
        # docstring contract.
        raise RuntimeError("usage API is in its backoff window and no cached value is available yet")
    try:
        _record_attempt()
        data = _fetch()
        _mem["ts"] = now
        _mem["data"] = data
        _serve["fetched_at"] = now
        _serve["stale"] = False
        _save_last_good(data)
        return data
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise
        if exc.code == 429:
            # The server told us when to come back; respect it across processes.
            try:
                ra = float(exc.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                ra = 0.0
            _record_attempt(retry_after=ra)
        # 429 / 5xx — fall back to last-good if recent enough
        if _mem["data"] is not None and (now - _mem["ts"]) < STALE_GRACE:
            return _serve_cached(_mem["ts"], _mem["data"], now)
        ts, data = _load_last_good()
        if data is not None and (now - ts) < STALE_GRACE:
            return _serve_cached(ts, data, now)
        raise
    except Exception:
        if _mem["data"] is not None and (now - _mem["ts"]) < STALE_GRACE:
            return _serve_cached(_mem["ts"], _mem["data"], now)
        ts, data = _load_last_good()
        if data is not None and (now - ts) < STALE_GRACE:
            return _serve_cached(ts, data, now)
        raise