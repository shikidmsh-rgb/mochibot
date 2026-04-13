"""Weather Observer — current conditions via wttr.in (no API key required).

Requires:
  WEATHER_CITY — city name (e.g. "Tokyo", "New York", "Shanghai")
"""

import logging
import os
from urllib.parse import quote

import httpx

from mochi.observers.base import Observer

log = logging.getLogger(__name__)

_WTTR_URL = "https://wttr.in"


class WeatherObserver(Observer):
    """Fetches current weather from wttr.in every 60 minutes."""

    def has_delta(self, prev: dict, curr: dict) -> bool:
        """Weather changes alone don't justify a Think call."""
        return False

    async def observe(self) -> dict:
        # DB config (admin portal) takes priority over .env
        from mochi.db import get_skill_config
        db_cfg = get_skill_config("weather")
        city = db_cfg.get("WEATHER_CITY") or os.getenv("WEATHER_CITY", "")
        if not city:
            log.warning("WeatherObserver: missing WEATHER_CITY (should have been auto-disabled)")
            return {}

        url = f"{_WTTR_URL}/{quote(city)}?format=j1"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current_condition", [{}])
        if not current:
            log.warning("WeatherObserver: empty current_condition from wttr.in")
            return {}
        cc = current[0]

        temp_c = int(cc.get("temp_C", 0))
        feels_like = int(cc.get("FeelsLikeC", 0))
        humidity = int(cc.get("humidity", 0))
        wind_kph = int(cc.get("windspeedKmph", 0))

        weather_desc_list = cc.get("weatherDesc", [{}])
        description = weather_desc_list[0].get("value", "unknown") if weather_desc_list else "unknown"
        condition = description.lower()

        summary = f"{temp_c}°C, {description}"

        return {
            "temperature_c": temp_c,
            "feels_like_c": feels_like,
            "condition": condition,
            "description": description,
            "humidity": humidity,
            "wind_kph": wind_kph,
            "summary": summary,
        }
