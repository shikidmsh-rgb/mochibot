"""Weather Observer — current conditions via OpenWeatherMap free tier.

Requires:
  OPENWEATHER_API_KEY  — free account at openweathermap.org
  WEATHER_LAT          — latitude  (e.g. 31.2304)
  WEATHER_LON          — longitude (e.g. 121.4737)
"""

import logging
import os

import httpx

from mochi.observers.base import Observer

log = logging.getLogger(__name__)

_OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

# Map OWM condition codes -> simple label
_CONDITION_MAP: dict[str, str] = {
    "Clear": "sunny",
    "Clouds": "cloudy",
    "Rain": "rainy",
    "Drizzle": "drizzle",
    "Thunderstorm": "stormy",
    "Snow": "snowy",
    "Mist": "misty",
    "Fog": "foggy",
    "Haze": "hazy",
    "Dust": "dusty",
    "Sand": "dusty",
    "Ash": "ashy",
    "Squall": "windy",
    "Tornado": "tornado",
}


class WeatherObserver(Observer):
    """Fetches current weather from OpenWeatherMap every 30 minutes."""

    async def observe(self) -> dict:
        api_key = os.getenv("OPENWEATHER_API_KEY", "")
        lat = os.getenv("WEATHER_LAT", "")
        lon = os.getenv("WEATHER_LON", "")

        if not all([api_key, lat, lon]):
            log.warning("WeatherObserver: missing config (should have been auto-disabled)")
            return {}

        params = {
            "lat": lat,
            "lon": lon,
            "appid": api_key,
            "units": "metric",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_OWM_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        main = data.get("main", {})
        wind = data.get("wind", {})
        weather_list = data.get("weather", [{}])
        weather_info = weather_list[0] if weather_list else {}

        owm_main = weather_info.get("main", "")
        condition = _CONDITION_MAP.get(owm_main, owm_main.lower() if owm_main else "unknown")
        description = weather_info.get("description", "")
        temp_c = round(main.get("temp", 0), 1)
        feels_like = round(main.get("feels_like", 0), 1)
        humidity = main.get("humidity", 0)
        wind_ms = wind.get("speed", 0)
        wind_kph = round(wind_ms * 3.6, 1)

        summary = f"{temp_c}°C, {description}" if description else f"{temp_c}°C, {condition}"

        return {
            "temperature_c": temp_c,
            "feels_like_c": feels_like,
            "condition": condition,
            "description": description,
            "humidity": humidity,
            "wind_kph": wind_kph,
            "summary": summary,
        }
