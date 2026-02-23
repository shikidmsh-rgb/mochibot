"""Oura Ring API client — fetches sleep, activity, readiness, and stress data.

Uses OAuth2 with auto-refreshing tokens. Token refresh happens transparently
when the access token expires (401 response).

Setup:
  1. Create an Oura app at https://cloud.ouraring.com/v2/docs
  2. Run `python oura_auth.py` to get your refresh token
  3. Set OURA_CLIENT_ID, OURA_CLIENT_SECRET, OURA_REFRESH_TOKEN in .env
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from mochi.config import (
    OURA_CLIENT_ID,
    OURA_CLIENT_SECRET,
    OURA_REFRESH_TOKEN,
    TIMEZONE_OFFSET_HOURS,
)

log = logging.getLogger(__name__)

# Oura API base URL
API_BASE = "https://api.ouraring.com/v2/usercollection"
TOKEN_URL = "https://api.ouraring.com/oauth/token"

# User timezone
TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# --- Persistent token cache ---
# Access token lasts ~24h. Persist to .env so restarts don't burn
# a one-time-use refresh token every time.
_access_token: str = os.getenv("OURA_ACCESS_TOKEN", "")
_refresh_token: str = OURA_REFRESH_TOKEN or ""
_token_expires_at: float = float(os.getenv("OURA_TOKEN_EXPIRES_AT", "0"))

if _access_token and time.time() < _token_expires_at:
    _remaining = int(_token_expires_at - time.time())
    logging.getLogger(__name__).info(
        "Oura: loaded cached access token from .env (valid for %dd %dh)",
        _remaining // 86400, (_remaining % 86400) // 3600,
    )
else:
    _access_token = ""  # expired or missing, will refresh on first API call

# Data cache (avoid hammering API)
_cache: dict = {}
_CACHE_TTL = 600  # 10 minutes


def is_configured() -> bool:
    """Check if Oura OAuth2 credentials are configured."""
    return bool(OURA_CLIENT_ID and OURA_CLIENT_SECRET and _refresh_token)


def _refresh_access_token() -> str:
    """Exchange refresh token for a new access token (OAuth2 mode)."""
    global _access_token, _refresh_token, _token_expires_at

    if not _refresh_token:
        log.warning("No Oura refresh token configured")
        return ""

    data = urlencode({
        "grant_type": "refresh_token",
        "refresh_token": _refresh_token,
        "client_id": OURA_CLIENT_ID,
        "client_secret": OURA_CLIENT_SECRET,
    })

    try:
        resp = httpx.post(
            TOKEN_URL,
            content=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()

        _access_token = result["access_token"]
        new_refresh = result.get("refresh_token", _refresh_token)
        expires_in = result.get("expires_in", 86400)
        _token_expires_at = time.time() + expires_in - 60

        if new_refresh != _refresh_token:
            _refresh_token = new_refresh
        _persist_tokens(new_refresh, _access_token, _token_expires_at)

        log.info("Oura token refreshed, expires in %ds", expires_in)
        return _access_token

    except httpx.HTTPStatusError as e:
        log.error("Oura token refresh failed: %s %s", e.response.status_code, e.response.text[:200])
        return ""
    except Exception as e:
        log.error("Oura token refresh error: %s", e)
        return ""


def _persist_tokens(refresh: str, access: str, expires_at: float):
    """Persist all Oura tokens to .env so service restarts reuse them.

    Critical because Oura refresh tokens are ONE-TIME-USE.
    Without persisting, every restart burns a refresh token.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text().splitlines(keepends=True)
        updates = {
            "OURA_REFRESH_TOKEN": refresh,
            "OURA_ACCESS_TOKEN": access,
            "OURA_TOKEN_EXPIRES_AT": str(int(expires_at)),
        }
        found = set()
        for i, line in enumerate(lines):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                lines[i] = f"{key}={updates[key]}\n"
                found.add(key)
        for key in updates:
            if key not in found:
                lines.append(f"{key}={updates[key]}\n")
        env_path.write_text("".join(lines))
        log.info("Persisted Oura tokens to .env")
    except Exception as e:
        log.warning("Could not persist tokens to .env: %s", e)


def _get_token() -> str:
    """Get a valid access token, refreshing if needed."""
    global _access_token
    if _access_token and time.time() < _token_expires_at:
        return _access_token
    if _access_token:
        log.info("Oura: access token expired, refreshing…")
    else:
        log.info("Oura: no cached access token, refreshing…")
    return _refresh_access_token()


def _api_get(endpoint: str, params: dict | None = None) -> dict | None:
    """Make an authenticated GET request to Oura API."""
    token = _get_token()
    if not token:
        return None

    url = f"{API_BASE}/{endpoint}"

    try:
        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            # Token expired, try refreshing once
            log.info("Oura 401, refreshing token...")
            token = _refresh_access_token()
            if not token:
                return None
            try:
                resp = httpx.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e2:
                log.error("Oura API retry failed: %s", e2)
                return None
        else:
            log.error("Oura API error: %s %s", e.response.status_code, e.response.text[:200])
            return None
    except Exception as e:
        log.error("Oura API request error: %s", e)
        return None


def _cached_get(key: str, endpoint: str, params: dict | None = None) -> dict | None:
    """Cached API GET — avoids hammering the API."""
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < _CACHE_TTL:
        return _cache[key]["data"]
    data = _api_get(endpoint, params)
    if data is not None:
        _cache[key] = {"data": data, "ts": now}
    return data


# ── Date Helpers ─────────────────────────────────────────────────────────


def _today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


def _next_day(date_str: str) -> str:
    """Oura API v2 end_date is EXCLUSIVE, so to include date D we pass D+1."""
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _prev_day(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def _get_daily_record(endpoint: str, target_date: str,
                      fallback_yesterday: bool = True) -> dict | None:
    """Fetch a single Oura daily record for the target date.

    Oura API date filtering uses UTC boundaries, but returns data tagged
    with the user's local timezone. We widen the query range to avoid
    off-by-one issues for non-UTC timezones.
    """
    wide_start = _prev_day(target_date)
    wide_end = _next_day(_next_day(target_date))

    cache_key = f"{endpoint}_{target_date}"
    result = _cached_get(cache_key, endpoint,
                         {"start_date": wide_start, "end_date": wide_end})

    if result and result.get("data"):
        for item in result["data"]:
            if item.get("day") == target_date:
                return item

    # Fallback to yesterday if querying today and no data yet
    if fallback_yesterday and target_date == _today_str():
        yesterday = _yesterday_str()
        cache_key_y = f"{endpoint}_{yesterday}"
        result_y = _cached_get(cache_key_y, endpoint,
                               {"start_date": _prev_day(yesterday),
                                "end_date": _next_day(_next_day(yesterday))})
        if result_y and result_y.get("data"):
            for item in result_y["data"]:
                if item.get("day") == yesterday:
                    return item

    return None


# ── Public Data Functions ────────────────────────────────────────────────


def get_sleep_data(date: str | None = None) -> dict | None:
    """Get detailed sleep data for a specific date.

    Returns the longest sleep period (main sleep) for the given date.
    """
    if not is_configured():
        return None

    d = date or _today_str()
    wide_start = _prev_day(d)
    wide_end = _next_day(_next_day(d))
    result = _cached_get(f"sleep_{d}", "sleep",
                         {"start_date": wide_start, "end_date": wide_end})

    if result and result.get("data"):
        day_periods = [p for p in result["data"] if p.get("day") == d]
        if day_periods:
            return max(day_periods, key=lambda p: p.get("total_sleep_duration", 0))

    # Fallback to yesterday
    if d == _today_str():
        y = _yesterday_str()
        result_y = _cached_get(f"sleep_{y}", "sleep",
                               {"start_date": _prev_day(y), "end_date": _next_day(_next_day(y))})
        if result_y and result_y.get("data"):
            day_periods = [p for p in result_y["data"] if p.get("day") == y]
            if day_periods:
                return max(day_periods, key=lambda p: p.get("total_sleep_duration", 0))

    return None


def get_daily_sleep_score(date: str | None = None) -> dict | None:
    """Get daily sleep score and contributors."""
    if not is_configured():
        return None
    return _get_daily_record("daily_sleep", date or _today_str())


def get_daily_activity(date: str | None = None) -> dict | None:
    """Get daily activity data — steps, calories, score."""
    if not is_configured():
        return None
    return _get_daily_record("daily_activity", date or _today_str())


def get_daily_readiness(date: str | None = None) -> dict | None:
    """Get daily readiness score and contributors."""
    if not is_configured():
        return None
    return _get_daily_record("daily_readiness", date or _today_str())


def get_daily_stress(date: str | None = None) -> dict | None:
    """Get daily stress summary."""
    if not is_configured():
        return None
    return _get_daily_record("daily_stress", date or _today_str())


def get_daily_summary(date: str | None = None) -> dict | None:
    """Get a comprehensive daily health summary.

    Returns a dict with "raw" data and "data_date" for the observer and skill.
    """
    if not is_configured():
        return None

    sleep = get_sleep_data(date)
    sleep_score_data = get_daily_sleep_score(date)
    activity = get_daily_activity(date)
    readiness = get_daily_readiness(date)
    stress = get_daily_stress(date)

    if not any([sleep, sleep_score_data, activity, readiness]):
        return None

    summary: dict = {"raw": {}}

    if sleep:
        summary["raw"]["sleep"] = {
            "total": sleep.get("total_sleep_duration", 0),
            "deep": sleep.get("deep_sleep_duration", 0),
            "rem": sleep.get("rem_sleep_duration", 0),
            "light": sleep.get("light_sleep_duration", 0),
            "efficiency": sleep.get("efficiency", 0),
            "avg_hr": sleep.get("average_heart_rate"),
            "avg_hrv": sleep.get("average_hrv"),
            "lowest_hr": sleep.get("lowest_heart_rate"),
            "bedtime_start": sleep.get("bedtime_start", ""),
            "bedtime_end": sleep.get("bedtime_end", ""),
        }

    if sleep_score_data:
        summary["raw"]["sleep_score"] = sleep_score_data.get("score")

    if activity:
        summary["raw"]["activity"] = {
            "score": activity.get("score"),
            "steps": activity.get("steps", 0),
            "active_calories": activity.get("active_calories", 0),
            "total_calories": activity.get("total_calories", 0),
        }

    if readiness:
        summary["raw"]["readiness"] = {
            "score": readiness.get("score"),
            "temperature_deviation": readiness.get("temperature_deviation"),
        }

    if stress:
        summary["raw"]["stress"] = {
            "stress_high": (stress.get("stress_high") or 0) // 60,
            "recovery_high": (stress.get("recovery_high") or 0) // 60,
            "day_summary": stress.get("day_summary", ""),
        }

    summary["data_date"] = sleep.get("day") if sleep else None
    return summary
