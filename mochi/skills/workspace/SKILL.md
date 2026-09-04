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
向今天的日记追加一段经历或感受，例如心情起伏、争执和状态变化。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| entry | string | yes | 日记内容 |

### read_diary (on_demand)
读取今天或指定日期的日记归档，为回顾当天经历提供原始记录。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| date | string | no | YYYY-MM-DD 格式。不填 = 今天 |

## Capability Context

- `write_diary` 追加今日日记；habit、todo 和 meal 的结构化状态由各自技能维护，重复写入日记会留下两份事实。
- `read_diary` 不带日期时读取今天，带 `YYYY-MM-DD` 时读取对应归档。
