"""Weather skill — get_weather tool + co-located observer.

Tool: get_weather — returns current weather from wttr.in (via observer cache).
Observer: WeatherObserver — collects weather data every 60 minutes.
"""

import json

from mochi.skills.base import Skill, SkillContext, SkillResult


class WeatherSkill(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        from mochi.observers import get_observer

        obs = get_observer("weather")
        if obs is None:
            return SkillResult(output="Weather not configured.", success=False)

        if context.args.get("force_refresh"):
            data = await obs.observe()
        else:
            data = await obs.safe_observe()

        if not data:
            return SkillResult(output="Weather data unavailable.", success=False)

        return SkillResult(output=json.dumps(data, ensure_ascii=False))
