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
日记是你的今日小本本，记录用户或者你的想法、经历、重要事项等。判断标准是：我应该今天一天都知道这事吗？我应该后续翻看知道这件事吗？

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
