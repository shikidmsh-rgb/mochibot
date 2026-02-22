---
name: reminder
description: "提醒 — 创建、查看、删除定时提醒"
type: tool
expose_as_tool: true
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
