---
name: workspace
description: "日记读写 — 写日记、查日记"
type: tool
locked: true
---

# Workspace Skill

日记读写。

## Tools

### write_diary (resident)
自由修订今天或明天的日记正文；日期、来源和状态区由系统管理；正文结构与内容由 Main 决定。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| content | string | yes | 修订后的完整“今日日記”正文，不包含当天日期页头 |
| day | string (enum: today, tomorrow) | no | 写今天或明天，默认 today |

### read_diary (on_demand, adaptive)
读取今天或指定日期的日记归档，为回顾当天经历提供原始记录。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| date | string | no | YYYY-MM-DD 格式。不填 = 今天 |

## Capability Context

- `write_diary` 只写日记正文，不修改 habit、todo、meal 的结构化记录。
