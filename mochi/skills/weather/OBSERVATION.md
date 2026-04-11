---
name: weather
interval: 60
enabled: true
requires_config: [WEATHER_CITY]
skill_name: weather
---

Provides current weather data for the user's location via wttr.in (no API key required).

## Fields
| Field | Type | Description |
|-------|------|-------------|
| temperature_c | number | Temperature in Celsius |
| feels_like_c | number | Feels-like temperature in Celsius |
| condition | string | sunny / cloudy / rainy / snowy / ... |
| description | string | Description from API, e.g. "Light rain" |
| humidity | number | Humidity percentage (0-100) |
| wind_kph | number | Wind speed in km/h |
| summary | string | Human-readable one-liner, e.g. "22°C, Partly cloudy" |

## Config
| Env Var | Required | Description |
|---------|----------|-------------|
| WEATHER_CITY | yes | City name (e.g. Tokyo, New York, Shanghai) |
