---
name: reminder
description: "定时提醒 — 到点通知一下，不追踪完成情况。"
type: tool
expose_as_tool: true
diary_status_order: 30
sense:
  interval: 5
---

## Tools

### manage_reminder (L1)
创建、列出或删除提醒。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: create, list, delete) | yes | 操作类型 |
| message | string | no | 提醒内容（create 必填） |
| remind_at | string | no | ISO 8601 格式的提醒时间（create 必填） |
| reminder_id | integer | no | 提醒 ID（delete 必填） |

## Usage Rules
- **"过一会儿" / "待会儿"** → `delay_minutes: 30`

**不适合用 reminder 的场景：**
- 事件触发型（"遛狗后再做XX"）→ 用 `note`
- 修改已有 habit/todo 的执行条件 → 用 `note`
