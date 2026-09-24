---
name: perception
description: "感知最近环境状态 — 只读取 Observer 已有缓存的安全投影，不触发实时采集"
type: tool
locked: true
triggers: [tool_call]
---

## Tools

### look_around (resident)
读取 Observer 最近一次内存缓存的安全投影。省略 sources 或传空数组时返回所有已发现来源的概览；指定 1–3 个来源时按请求顺序返回详情。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| sources | array (items: string) | no | 可选来源名，最多 3 个且不可重复；空数组表示所有来源概览 |

## Capability Context

- `look_around` 不是摄像头、截图、GPS 或实时设备查看，也不会触发采集。
- 它只提供结构化事实，不会自动写入 Core、Memory Items、Diary、Todo 或 Reminder。
