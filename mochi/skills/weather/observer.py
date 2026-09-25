"""Weather Observer — current conditions via Open-Meteo (no API key required).

Requires:
  WEATHER_CITY — city name (e.g. "Tokyo", "New York", "Shanghai")
"""

import logging

import httpx

from mochi.observers.base import Observer

log = logging.getLogger(__name__)

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_CURRENT_FIELDS = (
    "temperature_2m", "apparent_temperature", "relative_humidity_2m",
    "weather_code", "wind_speed_10m",
)
_WEATHER_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _compact_number(value: object) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _select_location(city: str, results: list[dict]) -> dict:
    if not results:
        raise ValueError(f"Weather location not found: {city}")
    expected_name = city.split(",", 1)[0].strip().casefold()
    exact_matches = [
        result for result in results
        if str(result.get("name", "")).casefold() == expected_name
    ]
    return max(
        exact_matches or results, key=lambda result: result.get("population") or 0,
    )


class WeatherObserver(Observer):
    """Fetches current weather from Open-Meteo every 60 minutes."""

    _VIEW_FIELDS = (
        "temperature_c",
        "feels_like_c",
        "condition",
        "description",
        "humidity",
        "wind_kph",
    )

    def overview_view(self, data: dict) -> dict:
        return self.select_view(data, scalar_fields=self._VIEW_FIELDS)

    def detail_view(self, data: dict) -> dict:
        return self.select_view(data, scalar_fields=self._VIEW_FIELDS)

    def has_delta(self, prev: dict, curr: dict) -> bool:
        """Weather does not schedule autonomous Main work."""
        return False

    async def observe(self) -> dict:
        from mochi.skills import get_skill
        from mochi.skill_config_resolver import resolve_skill_config

        skill = get_skill("weather")
        values = resolve_skill_config("weather", skill._config_schema_typed) if skill else {}
        city = str(values.get("WEATHER_CITY", ""))
        if not city:
            log.warning("WeatherObserver: missing WEATHER_CITY (should have been auto-disabled)")
            return {}

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                _GEOCODING_URL,
                params={"name": city, "count": 10, "language": "en", "format": "json"},
            )
            response.raise_for_status()
            location = _select_location(city, response.json().get("results", []))
            response = await client.get(
                _FORECAST_URL,
                params={
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                    "current": ",".join(_CURRENT_FIELDS),
                    "timezone": "auto",
                },
            )
            response.raise_for_status()
            current = response.json().get("current")

        if not isinstance(current, dict):
            raise ValueError("Open-Meteo response is missing current weather")
        temp_c = _compact_number(current["temperature_2m"])
        feels_like = _compact_number(current["apparent_temperature"])
        humidity = _compact_number(current["relative_humidity_2m"])
        wind_kph = _compact_number(current["wind_speed_10m"])
        weather_code = int(current["weather_code"])
        description = _WEATHER_DESCRIPTIONS.get(
            weather_code, f"Unknown conditions ({weather_code})",
        )
        condition = description.lower()

        summary = f"{temp_c}°C, {description}"

        return {
            "city": city,
            "temperature_c": temp_c,
            "feels_like_c": feels_like,
            "condition": condition,
            "description": description,
            "humidity": humidity,
            "wind_kph": wind_kph,
            "summary": f"{city}: {summary}",
        }
