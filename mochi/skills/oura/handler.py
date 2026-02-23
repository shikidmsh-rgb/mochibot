"""Oura Ring skill — structured health data for Chat layer.

Provides get_oura_data tool for querying sleep, activity, readiness, and stress.
Reuses oura_client's existing 10-min cache — no extra API calls.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.config import TIMEZONE_OFFSET_HOURS

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


def _sensor_response(data: dict | None, cached_at: str | None,
                     stale: bool = False, error: str | None = None) -> dict:
    """Unified sensor tool return protocol."""
    return {
        "available": data is not None,
        "data": data or {},
        "cached_at": cached_at,
        "stale": stale,
        "error": error,
    }


class OuraSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        """Handle get_oura_data tool call."""
        if context.tool_name != "get_oura_data":
            result = _sensor_response(None, None, error=f"unknown tool: {context.tool_name}")
            return SkillResult(output=json.dumps(result), success=False)

        from mochi import oura_client

        if not oura_client.is_configured():
            result = _sensor_response(None, None, error="oura_not_configured")
            return SkillResult(output=json.dumps(result), success=False)

        category = context.args.get("category", "all")
        date = context.args.get("date")  # None = today

        try:
            result = self._fetch(category, date)
            return SkillResult(output=json.dumps(result, ensure_ascii=False), success=True)
        except Exception as e:
            log.error("get_oura_data error: %s", e, exc_info=True)
            result = _sensor_response(None, None, error=str(e))
            return SkillResult(output=json.dumps(result), success=False)

    def _fetch(self, category: str, date: str | None) -> dict:
        """Fetch Oura data by category."""
        cached_at = datetime.now(TZ).isoformat()

        if category == "sleep":
            data = self._build_sleep(date)
        elif category == "activity":
            data = self._build_activity(date)
        elif category == "readiness":
            data = self._build_readiness(date)
        elif category == "stress":
            data = self._build_stress(date)
        elif category == "all":
            data = self._build_all(date)
        else:
            return _sensor_response(None, cached_at, error=f"unknown category: {category}")

        if data is None:
            return _sensor_response(None, cached_at, error="no_data")
        return _sensor_response(data, cached_at)

    def _build_sleep(self, date: str | None) -> dict | None:
        from mochi import oura_client

        sleep = oura_client.get_sleep_data(date)
        score_data = oura_client.get_daily_sleep_score(date)

        if not sleep and not score_data:
            return None

        result: dict = {}
        if score_data and score_data.get("score") is not None:
            result["score"] = score_data["score"]

        if sleep:
            result["total_sleep_sec"] = sleep.get("total_sleep_duration", 0)
            result["deep_sleep_sec"] = sleep.get("deep_sleep_duration", 0)
            result["rem_sleep_sec"] = sleep.get("rem_sleep_duration", 0)
            result["light_sleep_sec"] = sleep.get("light_sleep_duration", 0)
            result["efficiency"] = sleep.get("efficiency", 0)
            result["avg_hr"] = sleep.get("average_heart_rate")
            result["avg_hrv"] = sleep.get("average_hrv")
            result["lowest_hr"] = sleep.get("lowest_heart_rate")
            result["bedtime_start"] = sleep.get("bedtime_start", "")
            result["bedtime_end"] = sleep.get("bedtime_end", "")
            result["day"] = sleep.get("day", "")

        return result

    def _build_activity(self, date: str | None) -> dict | None:
        from mochi import oura_client

        activity = oura_client.get_daily_activity(date)
        if not activity:
            return None

        return {
            "score": activity.get("score"),
            "steps": activity.get("steps", 0),
            "active_calories": activity.get("active_calories", 0),
            "total_calories": activity.get("total_calories", 0),
            "day": activity.get("day", ""),
        }

    def _build_readiness(self, date: str | None) -> dict | None:
        from mochi import oura_client

        readiness = oura_client.get_daily_readiness(date)
        if not readiness:
            return None

        return {
            "score": readiness.get("score"),
            "temperature_deviation": readiness.get("temperature_deviation"),
            "day": readiness.get("day", ""),
        }

    def _build_stress(self, date: str | None) -> dict | None:
        from mochi import oura_client

        stress = oura_client.get_daily_stress(date)
        if not stress:
            return None

        return {
            "stress_high_min": stress.get("stress_high", 0),
            "recovery_high_min": stress.get("recovery_high", 0),
            "day_summary": stress.get("day_summary", ""),
            "day": stress.get("day", ""),
        }

    def _build_all(self, date: str | None) -> dict | None:
        """Compact summary of all categories."""
        sleep = self._build_sleep(date)
        activity = self._build_activity(date)
        readiness = self._build_readiness(date)
        stress = self._build_stress(date)

        if not any([sleep, activity, readiness, stress]):
            return None

        result: dict = {}
        if sleep:
            result["sleep"] = sleep
        if activity:
            result["activity"] = activity
        if readiness:
            result["readiness"] = readiness
        if stress:
            result["stress"] = stress
        return result
