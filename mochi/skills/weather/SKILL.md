---
name: weather
description: "天气查询 — 通过 Open-Meteo 获取当前天气数据"
type: hybrid
requires:
  env: [WEATHER_CITY]
sense:
  interval: 60
config:
  WEATHER_CITY:
    label: "天气城市"
    type: str
    default: ""
    description: "City name (e.g. Tokyo, New York, Shanghai)"
---

## Tools

### get_weather (routed)
读取配置城市的当前天气实况，可为当下温度、降水和穿衣判断提供数据。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| force_refresh | boolean | no | 设为 true 可绕过缓存，从 Open-Meteo 拉取最新数据 |

## Capability Context

- 这个能力只返回当前实况，不提供明天、后天或更远的预报。当前数据不能作为未来天气已经确定的证据。
- `force_refresh` 会绕过本地缓存并访问 Open-Meteo；普通读取可能使用缓存。
