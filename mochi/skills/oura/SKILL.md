---
name: oura
description: "Oura 智能戒指 — 睡眠、活动、准备度、压力、心率、血氧"
type: tool
expose_as_tool: true
requires:
  env: [OURA_CLIENT_ID, OURA_CLIENT_SECRET, OURA_REFRESH_TOKEN]
sense:
  interval: 30
writes:
  diary: [journal, today_ctx]
  db: [health_log]
config:
  diary_journal:
    type: bool
    default: true
    internal: true
    description: "Write sleep/workout events to journal"
  diary_today_ctx:
    type: bool
    default: true
    internal: true
    description: "Write sleep/cycle summary to today context"
---

## Tools

### get_oura_data (L0)
查询 Oura 戒指健康数据。用于回答关于睡眠、活动、准备度、压力、血氧、心率、运动等健康问题。返回 Oura API 缓存中的结构化数据。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| category | string | no | sleep / activity / readiness / stress / spo2 / heartrate / workout / all（默认 all） |
| date | string | no | YYYY-MM-DD 格式，默认今天（今日不可用时回退到昨天） |
| force_refresh | boolean | no | 设为 true 可绕过 10 分钟 API 缓存，拉取最新数据 |

## Usage Rules
- 用户问健康、睡眠、压力、活动等相关问题时，调用本工具获取数据再回答，即使上下文中已有 Oura 数据也要重新拉取
- **过期数据**：响应中 `"stale": true` 表示今日数据尚未同步、返回的是昨天的，如实告知用户（参考 `_note` 字段）

## Response Gotchas
- `stress_high_hr` / `recovery_high_hr`：单位是**小时**
- Oura 把睡眠记录在**醒来**那天
