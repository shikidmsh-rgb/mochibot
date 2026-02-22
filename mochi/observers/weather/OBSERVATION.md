---
name: weather
interval: 30
enabled: true
requires_config: [OPENWEATHER_API_KEY, WEATHER_LAT, WEATHER_LON]
---

Provides current weather data for the user's location via OpenWeatherMap free tier.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| temperature_c | number | Temperature in Celsius |
| feels_like_c | number | Feels-like temperature in Celsius |
| condition | string | sunny / cloudy / rainy / snowy / ... |
| description | string | Raw description from API, e.g. "light rain" |
| humidity | number | Humidity percentage (0-100) |
| wind_kph | number | Wind speed in km/h |
| summary | string | Human-readable one-liner, e.g. "22°C, partly cloudy" |

## Config
| Env Var | Required | Description |
|---------|----------|-------------|
| OPENWEATHER_API_KEY | yes | Free tier API key from openweathermap.org |
| WEATHER_LAT | yes | Latitude of the user's location |
| WEATHER_LON | yes | Longitude of the user's location |
