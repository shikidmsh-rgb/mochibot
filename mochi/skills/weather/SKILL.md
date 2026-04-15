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
获取用户所在城市的当前天气。返回温度、体感温度、天气状况、湿度、风速。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| force_refresh | boolean | no | 设为 true 可绕过缓存，从 wttr.in 拉取最新数据 |

## Usage Rules
- 本工具只有当前实况，没有预报。如果用户问的是"明天/后天/未来"，仍然调用工具获取当前天气，回复时附带说明"目前只能查到实时天气，暂不支持预报"。
