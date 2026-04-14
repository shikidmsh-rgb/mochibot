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
- 用户问天气、气温、穿衣建议等，**一律先调用 get_weather 获取数据**，拿到结果再回复。
- 本工具只有当前实况，没有预报。如果用户问的是"明天/后天/未来"，仍然调用工具获取当前天气，回复时附带说明"目前只能查到实时天气，暂不支持预报"。
- 默认返回后台缓存数据（每 60 分钟更新）。如果用户要求最新数据，传 `force_refresh: true`。
