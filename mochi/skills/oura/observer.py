"""Oura Ring Observer — sleep, activity, readiness, and stress data.

Wraps oura_client.get_daily_summary() with the existing 10-min API cache.
Interval: 30 minutes (Oura data updates every 20-30 min during the day).
"""

import logging
from datetime import datetime, timezone, timedelta

from mochi.observers.base import Observer
from mochi.config import TIMEZONE_OFFSET_HOURS

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# Baselines cache — recalculated once per day
_baselines_cache: dict | None = None
_baselines_cache_date: str | None = None


def _get_baselines() -> dict:
    """Calculate 7-day sleep / readiness score baselines. Cached per day."""
    global _baselines_cache, _baselines_cache_date

    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    if _baselines_cache is not None and _baselines_cache_date == today_str:
        return _baselines_cache

    baselines: dict = {}
    try:
        from mochi.oura_client import get_daily_sleep_score, get_daily_readiness
        now = datetime.now(TZ)
        sleep_scores, readiness_scores = [], []

        for i in range(1, 8):
            date_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            try:
                ss = get_daily_sleep_score(date_str)
                if ss and ss.get("score"):
                    sleep_scores.append(ss["score"])
            except Exception:
                pass
            try:
                rs = get_daily_readiness(date_str)
                if rs and rs.get("score"):
                    readiness_scores.append(rs["score"])
            except Exception:
                pass

        if sleep_scores:
            baselines["sleep_score_7d_avg"] = round(
                sum(sleep_scores) / len(sleep_scores), 1
            )
            if len(sleep_scores) >= 4:
                mid = len(sleep_scores) // 2
                recent = sum(sleep_scores[:mid]) / mid
                older = sum(sleep_scores[mid:]) / (len(sleep_scores) - mid)
                diff = recent - older
                baselines["sleep_score_trend"] = (
                    "improving" if diff > 3 else "declining" if diff < -3 else "stable"
                )

        if readiness_scores:
            baselines["readiness_7d_avg"] = round(
                sum(readiness_scores) / len(readiness_scores), 1
            )

    except Exception as e:
        log.debug("Oura baseline calc failed (non-critical): %s", e)

    _baselines_cache = baselines
    _baselines_cache_date = today_str
    return baselines


class OuraObserver(Observer):
    """Fetches Oura Ring data every 30 minutes.

    Auto-returns {} when Oura is not configured (no token set),
    which means it won't appear in the heartbeat observation dict.
    """

    async def observe(self) -> dict:
        from mochi.oura_client import is_configured, get_daily_summary

        if not is_configured():
            return {}

        summary = get_daily_summary()
        if not summary:
            return {}

        raw = summary.get("raw", {})
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        data_date = summary.get("data_date")
        result: dict = {"available": True, "data_date": data_date}

        # Sleep (only if synced today)
        if data_date == today_str and "sleep" in raw:
            s = raw["sleep"]
            result["sleep"] = {
                "total_hours": round(s["total"] / 3600, 1) if s.get("total") else None,
                "deep_hours": round(s["deep"] / 3600, 1) if s.get("deep") else None,
                "rem_hours": round(s["rem"] / 3600, 1) if s.get("rem") else None,
                "efficiency": s.get("efficiency"),
                "avg_hr": s.get("avg_hr"),
                "avg_hrv": s.get("avg_hrv"),
                "lowest_hr": s.get("lowest_hr"),
                "bedtime_start": s.get("bedtime_start"),
                "bedtime_end": s.get("bedtime_end"),
            }

        if data_date == today_str and "sleep_score" in raw:
            result["sleep_score"] = raw["sleep_score"]

        if data_date != today_str:
            result["sleep_not_synced"] = True

        # Activity (intraday — always current)
        if "activity" in raw:
            a = raw["activity"]
            result["activity"] = {
                "score": a.get("score"),
                "steps": a.get("steps", 0),
                "active_calories": a.get("active_calories", 0),
            }

        # Readiness (only if synced today)
        if data_date == today_str and "readiness" in raw:
            r = raw["readiness"]
            result["readiness"] = {
                "score": r.get("score"),
                "temp_deviation": r.get("temperature_deviation"),
            }

        # Stress (intraday — always current)
        if "stress" in raw:
            st = raw["stress"]
            result["stress"] = {
                "stress_high_min": st.get("stress_high", 0),
                "recovery_high_min": st.get("recovery_high", 0),
                "day_summary": st.get("day_summary"),
            }

        result["baselines"] = _get_baselines()
        return result
