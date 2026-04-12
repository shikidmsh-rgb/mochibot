---
name: weather
description: "天气查询 — 通过 wttr.in 获取当前天气数据"
type: hybrid
expose_as_tool: true
requires:
  env: [WEATHER_CITY]
sense:
  interval: 60
config:
  WEATHER_CITY:
    type: str
    default: ""
    description: "City name (e.g. Tokyo, New York, Shanghai)"
---

## Tools

### get_weather (L0)
Get current weather conditions for the user's configured city. Returns temperature, feels-like, condition, humidity, wind speed.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| force_refresh | boolean | no | Set true to bypass cache and fetch fresh data from wttr.in |

## Usage Rules
- Call this when the user asks about weather, temperature, or outdoor conditions.
- Only current conditions are available — no forecasts.
- Default behavior returns cached data from the background observer (updated every 60 min). Use `force_refresh: true` if the user wants the latest.
