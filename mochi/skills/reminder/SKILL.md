---
name: reminder
description: "定时提醒 — 到了某个时间点提醒一下，纯时间触发，不需要追踪进度或催促完成。支持重复（每天、工作日、每周等），重复提醒也是 reminder 不是 habit（如'每天提醒我吃药'）。"
type: tool
expose_as_tool: true
diary_status_order: 30
---

## Tools

### manage_reminder (L1)
Create, list, or delete reminders for the user.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: create, list, delete) | yes | What to do |
| message | string | no | Reminder message (required for create) |
| remind_at | string | no | ISO 8601 datetime for the reminder (required for create) |
| reminder_id | integer | no | Reminder ID (required for delete) |

## Usage Rules
- When user says "remind me to X at Y", immediately create a reminder — don't ask for confirmation
- Parse relative times: "in 1 hour" / "tomorrow morning" / "next Monday" into ISO 8601
- `list` returns upcoming (unfired) reminders only
